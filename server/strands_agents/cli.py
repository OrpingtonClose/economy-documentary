"""CLI entrypoint that wires ``--pipeline=strands`` to :func:`run_documentary`.

The legacy ADK runner lives in :mod:`server.run_pipeline`. When
``--pipeline=strands`` is passed, that script delegates here so the
Strands + DeepAgent path is reachable without restructuring the ADK
entry. This module is deliberately small: it parses the CLI args, picks
a chat model, resolves the brief, creates a run dir, and drives
:func:`run_documentary` to completion.

CI safety posture
-----------------
* ``--test-mode`` (and the absence of a live API key) routes to a
  :class:`FakeMessagesListChatModel` + :mod:`_placeholders` tool list so
  the pipeline completes without GPU/TTS/LTX/LLM costs. The strangler-fig
  policy is unchanged: this path is still optional and only used when
  ``--pipeline=strands`` is explicitly requested.
* When a live key is available and ``--test-mode`` is not set, we build
  a real chat model via :func:`init_chat_model` and use the full default
  tool surface.  Interrupts still auto-reject by default (CI has no
  operator attached).

Exit codes
----------
``0`` on success, ``2`` when the run lacks credentials and was not
invoked with ``--test-mode`` (we refuse to silently degrade to a fake
model in a non-test run — ``run_pipeline.py``'s pre-flight checks
intentionally fail loudly on missing infrastructure).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from deepagents import SubAgent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable

from . import _placeholders
from .approval import request_human_approval
from .pipeline import build_default_subagents, build_default_tools, build_orchestrator
from .run import _auto_reject_interrupt

logger = logging.getLogger(__name__)


_LIVE_MODEL_ENV = "STRANDS_MODEL"
_LIVE_MODEL_DEFAULT = "anthropic:claude-sonnet-4-5-20250929"

# Credential env vars we consult before deciding a run is "live". Order
# matches our judge-stack preference (local-first is orthogonal — this
# is about which API the orchestrator LLM uses, not the evals).
_LIVE_CREDENTIAL_ENVS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
)


def _has_live_credentials() -> bool:
    """Return ``True`` when at least one supported provider key is set."""

    return any(os.environ.get(name) for name in _LIVE_CREDENTIAL_ENVS)


class _BindingFakeChatModel(FakeMessagesListChatModel):
    """``FakeMessagesListChatModel`` with a no-op ``bind_tools``.

    DeepAgent's factory calls ``model.bind_tools(...)`` unconditionally
    when composing its middleware stack. The stock langchain fake model
    raises :class:`NotImplementedError`, which short-circuits our smoke
    run before any message is produced. Returning ``self`` from
    ``bind_tools`` is the minimum surface required to satisfy that
    contract without emulating provider-side tool dispatch — our fake
    responses don't include tool calls, so the bound-tool list is
    never exercised.
    """

    def bind_tools(
        self,
        tools: Any,  # noqa: ARG002
        *,
        tool_choice: Any = None,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> Runnable:
        return self


def _build_fake_model() -> _BindingFakeChatModel:
    """Scripted chat model used by ``--test-mode`` smoke runs.

    Returns a single AI message with no tool calls so the orchestrator
    terminates immediately. Tests that want to exercise tool dispatch
    pass their own model via :func:`run_strands_pipeline`'s ``model``
    kwarg.
    """

    return _BindingFakeChatModel(
        responses=[
            AIMessage(content="strands smoke run: orchestrator reached"),
        ],
    )


def _placeholder_tools() -> list[Any]:
    """Deterministic, credential-free tool list for smoke runs.

    Mirrors :func:`server.strands_agents.pipeline.build_default_tools`
    but uses the placeholder stubs exclusively so the smoke run stays
    hermetic (no GPU workers, no B2 writes).
    """

    return [
        _placeholders.generate_scenario,
        _placeholders.evaluate_scenario,
        _placeholders.refine_scenario,
        _placeholders.evaluate_timing,
        _placeholders.launch_audio_render,
        _placeholders.launch_visual_production,
        _placeholders.launch_assembly,
        _placeholders.launch_b2_sync,
        _placeholders.check_tasks,
        _placeholders.await_tasks,
        request_human_approval,
    ]


def _resolve_live_model() -> BaseChatModel:
    """Build the live chat model from :envvar:`STRANDS_MODEL`.

    Uses :func:`langchain.chat_models.init_chat_model` so callers can
    pick any supported provider via a single env var
    (``anthropic:claude-sonnet-4-5-20250929``,
    ``openai:gpt-5-mini``, etc.).
    """

    # Imported lazily so ``--test-mode`` runs do not pay the import cost
    # and CI environments without ``langchain`` extras installed can still
    # execute the test-mode smoke.
    from langchain.chat_models import init_chat_model

    model_id = os.environ.get(_LIVE_MODEL_ENV, _LIVE_MODEL_DEFAULT)
    logger.info("strands_model=<%s> | resolving live chat model", model_id)
    return init_chat_model(model_id)


def _compose_brief(topic: str, corpus_path: str, language: str) -> str:
    """Assemble the orchestrator brief from ``run_pipeline.py`` args.

    The DeepAgent prompt expects a single natural-language user message.
    We include the topic, the language target, and the full corpus so
    ``generate_scenario`` has research grounding. For smoke runs the
    corpus is read verbatim; if it is absent we synthesize a minimal
    placeholder so ``--test-mode`` does not require a filesystem fixture.
    """

    corpus_content = ""
    path = Path(corpus_path) if corpus_path else None
    if path is not None and path.exists():
        corpus_content = path.read_text(encoding="utf-8", errors="replace")
    else:
        corpus_content = (
            f"(no corpus file at {corpus_path or '<unset>'}; "
            "proceeding with topic only — smoke run.)"
        )

    return (
        f"Topic: {topic}\n"
        f"Language: {language}\n"
        f"Corpus:\n{corpus_content}\n"
    )


def _new_run_dir(output_dir: str | os.PathLike[str], run_id: str) -> Path:
    """Create and return an isolated run dir under ``output_dir``.

    The DeepAgent's :class:`FilesystemBackend` operates rooted at this
    dir, so giving each invocation its own directory keeps scratch
    files, approval audit records, and AGENTS.md reads isolated.
    """

    base = Path(output_dir) / f"strands-run-{run_id}"
    base.mkdir(parents=True, exist_ok=True)
    return base


async def run_strands_pipeline(
    args: argparse.Namespace,
    *,
    model: str | BaseChatModel | None = None,
    tools: Sequence[Any] | None = None,
    subagents: Sequence[SubAgent] | None = None,
) -> dict[str, Any]:
    """Drive the Strands pipeline from parsed CLI args.

    Args:
        args: Parsed argparse namespace from ``run_pipeline.py``.
            Required fields: ``topic`` (str), ``corpus`` (str),
            ``language`` (str), ``output_dir`` (str), ``test_mode``
            (bool).
        model: Optional chat-model override. When ``None`` the mode is
            picked from ``args.test_mode`` + credential discovery.
        tools: Optional tool list override. When ``None`` we use the
            placeholder surface in ``--test-mode`` and the full default
            surface in live mode.
        subagents: Optional SubAgent list override. Defaults to the
            empty list so the smoke run does not need per-component
            SubAgent modules to be merged.

    Returns:
        The final graph state dict from :func:`run_documentary`.
    """

    run_id = uuid.uuid4().hex[:12]
    run_dir = _new_run_dir(args.output_dir, run_id)
    brief = _compose_brief(args.topic, args.corpus, args.language)

    resolved_model: str | BaseChatModel
    test_mode = bool(getattr(args, "test_mode", False))
    if model is not None:
        resolved_model = model
    elif test_mode or not _has_live_credentials():
        resolved_model = _build_fake_model()
        test_mode = True
    else:
        resolved_model = _resolve_live_model()

    if tools is not None:
        resolved_tools = list(tools)
    elif test_mode:
        resolved_tools = _placeholder_tools()
    else:
        # Live mode: full default tool surface wired to real GPU workers,
        # real approval queue, real B2 sync. Anything less would silently
        # degrade production runs to canned placeholder responses, which
        # the top-level ``run_pipeline.py`` pre-flight explicitly
        # forbids ("Never silently degrade to synthetic/placeholder media").
        resolved_tools = build_default_tools()

    if subagents is not None:
        resolved_subagents = list(subagents)
    elif test_mode:
        resolved_subagents = []
    else:
        # Same rationale as tools — the SubAgent surface (visual loop,
        # production supervisor, escalation) is required for real runs.
        resolved_subagents = build_default_subagents()

    logger.info(
        "run_id=<%s>, test_mode=<%s>, tool_count=<%d> | starting strands pipeline",
        run_id,
        test_mode,
        len(resolved_tools),
    )

    # ``run_documentary`` calls ``build_documentary_orchestrator`` by
    # default, which pulls in the full leaf surface and the
    # cross-component SubAgents. For the smoke run we want the
    # injected tools/subagents to be honoured, so we build the graph
    # here via :func:`build_orchestrator` and hand it to an inline
    # interrupt loop that mirrors :func:`run_documentary`.
    agent = build_orchestrator(
        run_dir,
        model=resolved_model,
        tools=resolved_tools,
        subagents=resolved_subagents,
    )

    state = await agent.ainvoke({"messages": [("user", brief)]})
    rounds = 0
    while "__interrupt__" in state:
        rounds += 1
        if rounds > 32:
            raise RuntimeError("strands smoke run exceeded 32 interrupt rounds")
        command = await _auto_reject_interrupt(state)
        state = await agent.ainvoke(command)

    logger.info(
        "run_id=<%s>, rounds=<%d> | strands pipeline complete",
        run_id,
        rounds,
    )
    state.setdefault("_run_id", run_id)
    state.setdefault("_run_dir", str(run_dir))
    state.setdefault("_test_mode", test_mode)
    return state


def _print_summary(state: dict[str, Any]) -> None:
    """Print a compact summary of the final graph state."""

    print("\n" + "=" * 60)
    print("STRANDS PIPELINE RESULTS")
    print("=" * 60)
    print(f"Run id:     {state.get('_run_id', '<missing>')}")
    print(f"Run dir:    {state.get('_run_dir', '<missing>')}")
    print(f"Test mode:  {state.get('_test_mode', False)}")
    messages = state.get("messages") or []
    if messages:
        last = messages[-1]
        content = getattr(last, "content", None) or (
            last.get("content") if isinstance(last, dict) else None
        )
        if content:
            preview = str(content)
            if len(preview) > 400:
                preview = preview[:397] + "..."
            print(f"Last msg:   {preview}")
    keys = sorted(k for k in state if not k.startswith("_") and k != "messages")
    if keys:
        print(f"State keys: {', '.join(keys)}")


def run_from_cli_args(args: argparse.Namespace) -> int:
    """Synchronous shim invoked by ``run_pipeline.py``.

    Resolves credentials, drives the async pipeline, prints a summary,
    and returns a POSIX exit code. Raises on fatal errors so
    ``run_pipeline.py``'s existing top-level exception handler surfaces
    a non-zero exit.
    """

    if not getattr(args, "test_mode", False) and not _has_live_credentials():
        sys.stderr.write(
            "[strands] no provider credentials found in environment "
            f"({', '.join(_LIVE_CREDENTIAL_ENVS)}). Re-run with --test-mode "
            "for a hermetic smoke, or export one of the keys above.\n",
        )
        return 2

    state = asyncio.run(run_strands_pipeline(args))
    _print_summary(state)

    try:
        audit_path = Path(state["_run_dir"]) / "final_state.json"
        audit_payload = {
            "run_id": state.get("_run_id"),
            "test_mode": state.get("_test_mode"),
            "state_keys": sorted(
                k for k in state if not k.startswith("_") and k != "messages"
            ),
        }
        audit_path.write_text(json.dumps(audit_payload, indent=2))
    except (OSError, TypeError) as exc:
        logger.warning("audit_write_failed=<%s> | %s", type(exc).__name__, exc)

    return 0


__all__ = [
    "run_from_cli_args",
    "run_strands_pipeline",
]
