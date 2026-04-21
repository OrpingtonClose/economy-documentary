"""Unit tests for the Strands CLI shim.

These tests cover the pure plumbing paths — credential detection, brief
composition, run-dir isolation, test-mode fake-model routing, and the
argparse contract with ``run_pipeline.py`` — without requiring any
external services. The smoke-run-level end-to-end test that drives a
real orchestrator with placeholder tools lives in the same file and
uses a :class:`FakeMessagesListChatModel` so the test remains hermetic.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from strands_agents import cli as strands_cli


@pytest.fixture(autouse=True)
def _clear_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure provider env vars never leak from the host into a test."""

    for name in strands_cli._LIVE_CREDENTIAL_ENVS:
        monkeypatch.delenv(name, raising=False)


def _args(
    tmp_path: Path,
    *,
    topic: str = "how does inflation affect purchasing power",
    corpus: str | None = None,
    language: str = "en-US",
    test_mode: bool = True,
) -> argparse.Namespace:
    """Build an argparse namespace matching ``run_pipeline.py``'s shape."""

    corpus_path = corpus if corpus is not None else str(tmp_path / "corpus.md")
    return argparse.Namespace(
        topic=topic,
        corpus=corpus_path,
        language=language,
        output_dir=str(tmp_path),
        test_mode=test_mode,
    )


class TestCredentialDetection:
    def test_no_env_means_no_credentials(self) -> None:
        assert strands_cli._has_live_credentials() is False

    def test_anthropic_key_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        assert strands_cli._has_live_credentials() is True

    def test_openai_key_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert strands_cli._has_live_credentials() is True

    def test_empty_key_does_not_count(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        assert strands_cli._has_live_credentials() is False


class TestBriefComposition:
    def test_corpus_contents_inlined(self, tmp_path: Path) -> None:
        corpus = tmp_path / "brief.md"
        corpus.write_text("Inflation is the sustained rise in prices.")
        result = strands_cli._compose_brief(
            "inflation", str(corpus), "en-US",
        )
        assert "Topic: inflation" in result
        assert "Language: en-US" in result
        assert "Inflation is the sustained rise" in result

    def test_missing_corpus_falls_back_to_placeholder(
        self,
        tmp_path: Path,
    ) -> None:
        result = strands_cli._compose_brief(
            "inflation", str(tmp_path / "missing.md"), "en-US",
        )
        assert "Topic: inflation" in result
        assert "no corpus file" in result
        assert "smoke run" in result

    def test_empty_corpus_path_placeholder(self) -> None:
        result = strands_cli._compose_brief("t", "", "en-US")
        assert "<unset>" in result


class TestRunDir:
    def test_run_dir_unique_per_id(self, tmp_path: Path) -> None:
        a = strands_cli._new_run_dir(tmp_path, "abc")
        b = strands_cli._new_run_dir(tmp_path, "def")
        assert a.is_dir()
        assert b.is_dir()
        assert a != b
        assert a.parent == tmp_path

    def test_existing_dir_reused(self, tmp_path: Path) -> None:
        first = strands_cli._new_run_dir(tmp_path, "xyz")
        first.joinpath("marker").write_text("keep")
        second = strands_cli._new_run_dir(tmp_path, "xyz")
        assert first == second
        assert (second / "marker").read_text() == "keep"


class TestFakeModel:
    def test_fake_model_returns_single_scripted_message(self) -> None:
        model = strands_cli._build_fake_model()
        assert isinstance(model, strands_cli._BindingFakeChatModel)
        assert len(model.responses) == 1
        assert isinstance(model.responses[0], AIMessage)
        assert "smoke run" in str(model.responses[0].content)

    def test_placeholder_tools_include_core_leaves(self) -> None:
        tools = strands_cli._placeholder_tools()
        names = {t.name for t in tools if hasattr(t, "name")}
        # These must be present — the smoke run uses them as the default
        # tool surface and the orchestrator prompt references them.
        required = {
            "generate_scenario",
            "evaluate_scenario",
            "refine_scenario",
            "evaluate_timing",
            "launch_audio_render",
            "launch_visual_production",
            "launch_assembly",
            "launch_b2_sync",
            "check_tasks",
            "await_tasks",
            "request_human_approval",
        }
        assert required.issubset(names)


class TestRunStrandsPipelineHermetic:
    """End-to-end smoke: orchestrator reaches a terminal state.

    Uses a FakeMessagesListChatModel so no network calls happen. This
    is the canonical "plumbing works" assertion — a successful run
    means argparse → brief → build_orchestrator → ainvoke →
    interrupt loop → final state all connected correctly.
    """

    def test_test_mode_run_completes(self, tmp_path: Path) -> None:
        state = asyncio.run(strands_cli.run_strands_pipeline(_args(tmp_path)))
        assert state.get("_test_mode") is True
        assert state.get("_run_id")
        run_dir = Path(state["_run_dir"])
        assert run_dir.is_dir()
        assert run_dir.parent == tmp_path

    def test_missing_credentials_forces_test_mode(
        self,
        tmp_path: Path,
    ) -> None:
        args = _args(tmp_path, test_mode=False)
        state = asyncio.run(strands_cli.run_strands_pipeline(args))
        assert state.get("_test_mode") is True

    def test_explicit_model_override_honoured(self, tmp_path: Path) -> None:
        # Use the binding variant — DeepAgent's factory calls
        # ``bind_tools`` on any injected model.
        override = strands_cli._BindingFakeChatModel(
            responses=[AIMessage(content="override reached")],
        )
        state = asyncio.run(
            strands_cli.run_strands_pipeline(
                _args(tmp_path, test_mode=False),
                model=override,
                tools=[],
                subagents=[],
            ),
        )
        # Explicit injection should NOT flip _test_mode True automatically;
        # the caller declared live mode by providing a model.
        assert state.get("_test_mode") is False

    def test_interrupt_loop_rejects_by_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the orchestrator yields ``__interrupt__``, we resume it.

        The placeholder surface does not interrupt, so we simulate the
        behaviour by patching the agent builder. This guards the
        contract that ``run_strands_pipeline`` drives the loop itself
        rather than leaving the caller holding an unresumed state.
        """

        calls: list[Any] = []

        class _StubAgent:
            def __init__(self) -> None:
                self.rounds = 2

            async def ainvoke(self, value: Any) -> dict[str, Any]:
                calls.append(value)
                if self.rounds > 0:
                    self.rounds -= 1
                    return {"__interrupt__": [{"value": "pending"}]}
                return {"messages": [AIMessage(content="done")]}

        monkeypatch.setattr(
            "strands_agents.cli.build_orchestrator",
            lambda *a, **kw: _StubAgent(),  # noqa: ARG005
        )
        state = asyncio.run(strands_cli.run_strands_pipeline(_args(tmp_path)))
        # 3 invocations: initial + 2 resumes
        assert len(calls) == 3
        assert state.get("messages")


class TestLiveModeToolResolution:
    """Live runs must use the full default tool surface, not placeholders.

    Regression guard for the contract that live-mode runs (real model,
    credentials present, ``--test-mode`` off) pick up the real GPU /
    TTS / LTX / approval tools from ``build_default_tools`` — not the
    hermetic placeholder list. Silently driving a live run with
    placeholders would violate ``run_pipeline.py``'s no-silent-degrade
    invariant.
    """

    def test_live_mode_uses_default_tools_and_subagents(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, Any] = {}

        class _StubAgent:
            async def ainvoke(self, _: Any) -> dict[str, Any]:
                return {"messages": [AIMessage(content="done")]}

        def _capture(run_dir: Any, **kw: Any) -> _StubAgent:  # noqa: ARG001
            captured["tools"] = list(kw["tools"])
            captured["subagents"] = list(kw["subagents"])
            return _StubAgent()

        monkeypatch.setattr(
            "strands_agents.cli.build_orchestrator",
            _capture,
        )
        sentinel_tools = [object(), object(), object()]
        sentinel_subagents = [{"name": "alpha"}, {"name": "beta"}]
        monkeypatch.setattr(
            "strands_agents.cli.build_default_tools",
            lambda: list(sentinel_tools),
        )
        monkeypatch.setattr(
            "strands_agents.cli.build_default_subagents",
            lambda: list(sentinel_subagents),
        )
        live_model = strands_cli._BindingFakeChatModel(
            responses=[AIMessage(content="live")],
        )

        asyncio.run(
            strands_cli.run_strands_pipeline(
                _args(tmp_path, test_mode=False),
                model=live_model,
            ),
        )

        assert captured["tools"] == sentinel_tools
        assert captured["subagents"] == sentinel_subagents

    def test_test_mode_uses_placeholders(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, Any] = {}

        class _StubAgent:
            async def ainvoke(self, _: Any) -> dict[str, Any]:
                return {"messages": [AIMessage(content="done")]}

        def _capture(run_dir: Any, **kw: Any) -> _StubAgent:  # noqa: ARG001
            captured["tools"] = list(kw["tools"])
            captured["subagents"] = list(kw["subagents"])
            return _StubAgent()

        monkeypatch.setattr(
            "strands_agents.cli.build_orchestrator",
            _capture,
        )

        def _unexpected_default_tools() -> list[Any]:
            raise AssertionError(
                "build_default_tools must not be called in test mode",
            )

        monkeypatch.setattr(
            "strands_agents.cli.build_default_tools",
            _unexpected_default_tools,
        )
        monkeypatch.setattr(
            "strands_agents.cli.build_default_subagents",
            _unexpected_default_tools,
        )

        asyncio.run(strands_cli.run_strands_pipeline(_args(tmp_path)))

        tool_names = {getattr(t, "name", "") for t in captured["tools"]}
        assert "generate_scenario" in tool_names
        assert "request_human_approval" in tool_names
        assert captured["subagents"] == []


class TestInterruptCap:
    def test_caps_at_32_rounds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _Infinite:
            async def ainvoke(self, _: Any) -> dict[str, Any]:
                return {"__interrupt__": [{"value": "loop"}]}

        monkeypatch.setattr(
            "strands_agents.cli.build_orchestrator",
            lambda *a, **kw: _Infinite(),  # noqa: ARG005
        )
        with pytest.raises(RuntimeError, match="interrupt rounds"):
            asyncio.run(
                strands_cli.run_strands_pipeline(_args(tmp_path)),
            )


class TestRunFromCliArgs:
    def test_success_returns_zero_and_writes_audit(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = strands_cli.run_from_cli_args(_args(tmp_path))
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "STRANDS PIPELINE RESULTS" in captured.out
        # Audit file lives next to the run dir under output_dir.
        run_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
        assert len(run_dirs) == 1
        audit = run_dirs[0] / "final_state.json"
        assert audit.is_file()
        payload = json.loads(audit.read_text())
        assert payload["test_mode"] is True
        assert "run_id" in payload

    def test_live_run_without_credentials_exits_two(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = strands_cli.run_from_cli_args(
            _args(tmp_path, test_mode=False),
        )
        assert exit_code == 2
        captured = capsys.readouterr()
        assert "no provider credentials" in captured.err

    def test_live_run_with_credentials_proceeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Live credentials should bypass the guard.

        We still substitute a fake model so no network calls happen —
        the goal here is to prove the guard does not fire, not to hit
        Anthropic.
        """

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

        def _fake_resolve() -> strands_cli._BindingFakeChatModel:
            return strands_cli._BindingFakeChatModel(
                responses=[AIMessage(content="live mode reached")],
            )

        monkeypatch.setattr(
            "strands_agents.cli._resolve_live_model", _fake_resolve,
        )

        exit_code = strands_cli.run_from_cli_args(
            _args(tmp_path, test_mode=False),
        )
        assert exit_code == 0


class TestModuleContract:
    """Guards against accidental surface regressions."""

    def test_public_exports(self) -> None:
        assert set(strands_cli.__all__) == {
            "run_from_cli_args",
            "run_strands_pipeline",
        }

    def test_live_model_default_is_pinned(self) -> None:
        # If we ever change the default, update the docs too.
        assert strands_cli._LIVE_MODEL_DEFAULT.startswith("anthropic:")

    def test_anthropic_env_first_in_ordering(self) -> None:
        # Credential precedence matters: Anthropic is the default model
        # provider and should be probed first for the friendly error.
        assert strands_cli._LIVE_CREDENTIAL_ENVS[0] == "ANTHROPIC_API_KEY"
