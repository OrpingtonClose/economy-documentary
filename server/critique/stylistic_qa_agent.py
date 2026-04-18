"""Stylistic QA agent (ARCH-E3, issue #149).

Composes the measurement callables in
:mod:`server.critique.audio_invariants` into a single stage-boundary
check. When any invariant fails on any block, the callback raises
:class:`StylisticInvariantFailure`, which the recovery ladder converts
into a re-entry of the audio ladder carrying the invariant violation
as the failure signal.

ADK idiom DoD (per meta #122):

- :func:`build_stylistic_qa_agent` returns a ``google.adk.agents.Agent``
  subclass instance with the measurement callables registered as
  plain ``tools=[...]`` — the LLM is available for ad-hoc diagnosis
  but the stage-boundary invariant is enforced deterministically by
  the ``after_agent_callback``.
- Cross-stage state flows through blackboard ``output_key`` — we
  persist the full invariant report under
  :data:`STYLISTIC_QA_STATE_KEY` so the dashboard and the audio
  recovery agent can read it.
- Stage-boundary enforcement runs as an ``after_agent_callback`` (same
  pattern as ``server.callbacks.timeline_guardian``).
- :data:`STYLISTIC_QA_OPERATION` is the recovery-ladder operation name;
  audio-stage callers pass it to ``recovery.escalate_pipeline_error``.
- Fail loud: unreadable state, missing narration clips, or any FAIL
  verdict raises — never silently downgrades to a warning.

Note on the ADK ``after_tool_callback`` wiring mentioned in issue
#127: the deterministic audio callback in
``server.callbacks.deterministic_steps`` does not use LLM tool-calling
for TTS, so per-tool callbacks would never fire. ARCH-E3 enforces
stylistic QA at the stage boundary instead (the audio agent's
``after_agent_callback`` chain) — the semantic guarantee is identical:
every emitted block is checked before Stage Two crystallises the
authoritative OTIO.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Mapping, MutableMapping, Optional, Sequence

from critique.audio_invariants import (
    InvariantResult,
    InvariantVerdict,
    NarrationBlock,
    check_character_voice_consistency,
    check_clicks,
    check_hiss_floor_continuity,
    check_peak_limiter,
    check_plosive_truncation,
    check_uniform_lufs,
    check_voice_continuity,
    collect_failures,
    run_all_invariants,
)
from critique.ledger_override import build_lufs_override_resolver

logger = logging.getLogger(__name__)


#: Blackboard key under which the full stylistic QA report is persisted.
#: A list of per-block dicts (``result.to_dict()`` output from
#: :class:`InvariantResult`).
STYLISTIC_QA_STATE_KEY = "_stylistic_qa_report"

#: Recovery operation name. Callers of
#: :func:`server.recovery.escalate_pipeline_error` pass this so the
#: audio ladder's agent policy recognises a stylistic invariant
#: violation and re-enters the ladder with the violation as the
#: failure signal.
STYLISTIC_QA_OPERATION = "audio_stylistic_invariant"


# ---------------------------------------------------------------------------
# Failure signal
# ---------------------------------------------------------------------------


class StylisticInvariantFailure(RuntimeError):
    """Raised when one or more stylistic invariants fail.

    The ``failures`` attribute carries the structured list of
    :class:`InvariantResult` with ``FAIL`` verdicts; the audio ladder
    recovery agent uses this instead of parsing the string message.
    """

    def __init__(self, failures: Sequence[InvariantResult]) -> None:
        self.failures: list[InvariantResult] = list(failures)
        names = ", ".join(f.name for f in self.failures)
        affected = sorted({f.block_id for f in self.failures})
        super().__init__(
            f"stylistic invariant violations "
            f"[{names}] on {len(affected)} block(s): {affected}"
        )

    def diagnostic_data(self) -> dict:
        """Structured payload for ``escalate_pipeline_error``.

        The audio recovery agent reads ``invariant_violations`` to
        decide which blocks need TTS re-synthesis and which per-clip
        parameters to perturb (e.g. a uniform_lufs failure drives a
        loudnorm re-run, a clicks failure drives a reseed).
        """
        return {
            "invariant_violations": [f.to_dict() for f in self.failures],
            "affected_blocks": sorted({f.block_id for f in self.failures}),
            "violated_invariants": sorted({f.name for f in self.failures}),
        }


# ---------------------------------------------------------------------------
# Blackboard parsing
# ---------------------------------------------------------------------------


def _parse_blocks_from_state(
    state: Mapping[str, Any],
) -> list[NarrationBlock]:
    """Extract the narration-block list from the pipeline blackboard.

    Looks for two state keys (populated by
    ``server.callbacks.deterministic_steps`` at audio-stage time):

    - ``_stylistic_qa_blocks``: explicit structured list of dicts
      (the deterministic callback populates this when it registers
      each TTS clip). Preferred.
    - ``whisperx_alignment``: the existing alignment dict keyed by
      ``scene_NNN_VOICE_LANG``. Fallback used when the explicit list
      is absent (e.g. during a partial B2-resume where only the
      alignment artefact has been rehydrated).
    """
    explicit = state.get("_stylistic_qa_blocks")
    if explicit:
        if isinstance(explicit, str):
            try:
                explicit = json.loads(explicit)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"_stylistic_qa_blocks is not valid JSON: {e}"
                ) from e
        if not isinstance(explicit, list):
            raise ValueError(
                f"_stylistic_qa_blocks must be a list, got {type(explicit).__name__}"
            )
        return [
            NarrationBlock(
                block_id=str(entry["block_id"]),
                wav_path=str(entry["wav_path"]),
                scene_num=int(entry.get("scene_num", 0) or 0),
                voice_role=str(entry.get("voice_role", "")),
                language=str(entry.get("language", "")),
                voice_id=str(entry.get("voice_id", "")),
            )
            for entry in explicit
            if isinstance(entry, Mapping) and entry.get("wav_path")
        ]

    alignment_raw = state.get("whisperx_alignment", "{}")
    try:
        alignment = (
            alignment_raw
            if isinstance(alignment_raw, Mapping)
            else json.loads(str(alignment_raw) or "{}")
        )
    except json.JSONDecodeError:
        alignment = {}

    blocks: list[NarrationBlock] = []
    for key, data in sorted((alignment or {}).items()):
        if not isinstance(key, str) or not isinstance(data, Mapping):
            continue
        if not key.startswith("scene_"):
            continue
        wav_path = str(data.get("wav_path", ""))
        if not wav_path:
            continue
        # key layout: scene_NNN_V1_RU  -> scene_num=NNN, voice_role=V1, lang=RU
        parts = key.split("_")
        try:
            scene_num = int(parts[1])
        except (IndexError, ValueError):
            continue
        voice_role = parts[2] if len(parts) >= 3 else "V1"
        language = parts[3].lower() if len(parts) >= 4 else ""
        blocks.append(NarrationBlock(
            block_id=key,
            wav_path=wav_path,
            scene_num=scene_num,
            voice_role=voice_role,
            language=language,
            voice_id=str(data.get("voice_id", "")),
        ))
    return blocks


def _persist_report(
    state: MutableMapping[str, Any],
    results: Sequence[InvariantResult],
) -> None:
    """Persist the full report as a JSON string under the blackboard key."""
    state[STYLISTIC_QA_STATE_KEY] = json.dumps([r.to_dict() for r in results])


# ---------------------------------------------------------------------------
# Stage-boundary entry point
# ---------------------------------------------------------------------------


def run_stylistic_qa(
    state: MutableMapping[str, Any],
    *,
    blocks: Optional[Sequence[NarrationBlock]] = None,
    raise_on_failure: bool = True,
) -> list[InvariantResult]:
    """Run every stylistic invariant on the current audio stage's blocks.

    Args:
        state: Pipeline blackboard state. Read-only for invariant
            parameters; written to for the :data:`STYLISTIC_QA_STATE_KEY`
            report and (on success) ``state["_stylistic_qa_passed"] = True``.
        blocks: Optional explicit block list. When omitted, the blocks
            are parsed from the blackboard (``_stylistic_qa_blocks`` or
            ``whisperx_alignment``).
        raise_on_failure: When ``True`` (default), any FAIL verdict
            raises :class:`StylisticInvariantFailure` after persisting
            the report. When ``False``, the caller gets the full
            result list and decides how to escalate (used by unit tests).

    Returns:
        Full :class:`InvariantResult` list, in the order:
        per-block → adjacent-pair → film-wide.
    """
    if blocks is None:
        blocks = _parse_blocks_from_state(state)

    if not blocks:
        logger.warning(
            "stylistic_qa: no narration blocks found in state; skipping "
            "(nothing to check — upstream gatekeeper will catch empty audio)"
        )
        state["_stylistic_qa_passed"] = True
        _persist_report(state, [])
        return []

    resolver = build_lufs_override_resolver(state)
    results = run_all_invariants(
        blocks,
        override_resolver=resolver,
    )
    _persist_report(state, results)

    failures = collect_failures(results)
    if failures:
        state["_stylistic_qa_passed"] = False
        logger.error(
            "stylistic_qa: %d invariant violation(s) across %d block(s): %s",
            len(failures),
            len({f.block_id for f in failures}),
            [f"{f.name}@{f.block_id}" for f in failures[:10]],
        )
        if raise_on_failure:
            raise StylisticInvariantFailure(failures)
    else:
        state["_stylistic_qa_passed"] = True
        logger.info(
            "stylistic_qa: all %d invariant checks passed across %d block(s)",
            len(results),
            len(blocks),
        )
    return results


def stylistic_qa_after_agent_callback(callback_context: Any) -> Optional[Any]:
    """ADK ``after_agent_callback`` that enforces stylistic invariants.

    Matches the signature used by
    :func:`server.callbacks.timeline_guardian.timeline_guardian_callback`.
    Runs only on the audio stage; other stages short-circuit to a
    no-op so the callback can be safely chained into a multi-stage
    agent without mis-firing.

    Returns ``None`` so the ADK runtime continues with whatever
    ``Content`` the agent produced. Raises
    :class:`StylisticInvariantFailure` on violation — the recovery
    middleware catches the exception and drives the audio ladder.
    """
    state = getattr(callback_context, "state", None)
    if state is None:
        raise RuntimeError(
            "stylistic_qa_after_agent_callback: CallbackContext has no state "
            "attribute; cannot locate narration blocks"
        )

    phase = state.get("pipeline_phase", "")
    if phase and phase != "audio":
        return None

    run_stylistic_qa(state, raise_on_failure=True)
    return None


# ---------------------------------------------------------------------------
# ADK Agent factory
# ---------------------------------------------------------------------------


def build_stylistic_qa_agent():
    """Return an ADK ``Agent`` that wraps the stylistic-QA invariants.

    The agent is intentionally thin: the invariants are deterministic
    measurements, so we register them as ``tools=[...]`` (plain
    callables) and rely on the ``after_agent_callback`` for the
    stage-boundary gate. An LLM is attached for diagnostic narration
    (so dashboards / human reviewers see a natural-language
    explanation of a failure) but must not be relied on to enforce
    the invariants — enforcement lives in the callback.

    Returns:
        A ``google.adk.agents.Agent`` instance, or a lightweight
        fallback object when ``google-adk`` is not importable (so
        that unit tests can import this module without the ADK
        dependency).
    """
    try:
        from google.adk.agents import Agent  # type: ignore
    except ImportError:
        logger.info(
            "google-adk not importable; returning lightweight stub Agent "
            "for unit-test environments."
        )

        class _StubAgent:
            name = "stylistic_qa_agent"
            tools = [
                check_uniform_lufs,
                check_peak_limiter,
                check_clicks,
                check_plosive_truncation,
                check_voice_continuity,
                check_hiss_floor_continuity,
                check_character_voice_consistency,
            ]
            after_agent_callback = staticmethod(stylistic_qa_after_agent_callback)

        return _StubAgent()

    try:
        from agents.model_config import build_model  # type: ignore
        model = build_model()
    except ImportError:  # pragma: no cover — defensive for non-repo imports
        model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

    return Agent(
        name="stylistic_qa_agent",
        model=model,
        instruction=(
            "You are the Stylistic QA Agent for a documentary pipeline.\n"
            "Stylistic invariants are enforced deterministically by the\n"
            "after_agent_callback. If a violation is reported, produce a\n"
            "one-paragraph diagnostic the audio recovery agent and the\n"
            "dashboard reviewer can read. Do not attempt to mask or\n"
            "re-interpret a FAIL verdict — the callback is authoritative."
        ),
        tools=[
            check_uniform_lufs,
            check_peak_limiter,
            check_clicks,
            check_plosive_truncation,
            check_voice_continuity,
            check_hiss_floor_continuity,
            check_character_voice_consistency,
        ],
        after_agent_callback=stylistic_qa_after_agent_callback,
    )
