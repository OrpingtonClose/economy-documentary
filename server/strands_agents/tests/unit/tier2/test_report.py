"""Unit tests for the Tier-2 per-component reporter plugin.

Rather than driving pytest-from-pytest (which is fragile and coupled
to pytest internals), we stub a minimal ``pytest.TestReport`` shape
and exercise ``Tier2Reporter`` directly.  This keeps the tests fast
and deterministic, and catches the regressions that bit us during
development:

- nodeid regex must match parametrised nodeids like ``...py::test_x[k]``
- skipped-in-setup outcomes must be recorded (not silently dropped)
- xfail vs xpass must be distinguished
- markdown output must be stable so the nightly dashboard doesn't drift
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from strands_agents.tier2._report import (
    Tier2Reporter,
    _artifact_key_from_nodeid,
    _component_from_nodeid,
)


def _fake_report(
    nodeid: str,
    *,
    when: str = "call",
    outcome: str = "passed",
    wasxfail: str | None = None,
) -> Any:
    """Build a minimal stand-in for a ``pytest.TestReport`` record."""
    report: SimpleNamespace = SimpleNamespace(nodeid=nodeid, when=when, outcome=outcome)
    if wasxfail is not None:
        report.wasxfail = wasxfail
    return report


class TestComponentFromNodeid:
    """Regex + known-component resolution."""

    def test_matches_plain_module_nodeid(self) -> None:
        nodeid = (
            "server/strands_agents/tests/unit/tier2/"
            "test_component_01_scenario_agent.py::test_corpus_seeded"
        )
        assert _component_from_nodeid(nodeid) == "01-scenario-agent"

    def test_matches_parametrised_nodeid(self) -> None:
        nodeid = (
            "server/strands_agents/tests/unit/tier2/"
            "test_component_04_audio_agent.py::"
            "test_hermetic_artifact_loads[audio.golden.intro]"
        )
        assert _component_from_nodeid(nodeid) == "04-audio-agent"

    def test_all_fifteen_components_resolve(self) -> None:
        for nn, slug in [
            ("01", "scenario_agent"),
            ("02", "timing_evaluator"),
            ("03", "scenario_refiner"),
            ("04", "audio_agent"),
            ("05", "timing_loop"),
            ("06", "content_analyst"),
            ("07", "visual_concepter"),
            ("08", "coherence_evaluator"),
            ("09", "visual_loop"),
            ("10", "production_supervisor"),
            ("11", "assembly_agent"),
            ("12", "recovery_agents"),
            ("13", "escalation_supervisor"),
            ("14", "pipeline_graph"),
            ("15", "approval_gates"),
        ]:
            nodeid = f"path/test_component_{nn}_{slug}.py::test_x"
            resolved = _component_from_nodeid(nodeid)
            assert resolved is not None
            assert resolved.startswith(f"{nn}-")

    def test_ignores_non_component_tests(self) -> None:
        assert _component_from_nodeid("tests/unit/test_harness.py::test_x") is None

    def test_extracts_artifact_key(self) -> None:
        nodeid = "x.py::test_y[foo.bar.baz]"
        assert _artifact_key_from_nodeid(nodeid) == "foo.bar.baz"

    def test_no_artifact_key_on_non_parametrised(self) -> None:
        assert _artifact_key_from_nodeid("x.py::test_y") is None


class TestReporterRecord:
    """Outcome bookkeeping."""

    def test_records_hermetic_pass(self) -> None:
        reporter = Tier2Reporter()
        reporter.record(
            _fake_report(
                "x/test_component_01_scenario_agent.py::"
                "test_hermetic_artifact_loads[scenario.golden.k]"
            )
        )
        snap = {s.component: s for s in reporter.snapshot()}
        assert snap["01-scenario-agent"].hermetic_pass == 1
        assert snap["01-scenario-agent"].hermetic_fail == 0

    def test_records_hermetic_fail_with_key(self) -> None:
        reporter = Tier2Reporter()
        reporter.record(
            _fake_report(
                "x/test_component_02_timing_evaluator.py::"
                "test_hermetic_artifact_loads[timing.adversarial.k]",
                outcome="failed",
            )
        )
        snap = {s.component: s for s in reporter.snapshot()}
        assert snap["02-timing-evaluator"].hermetic_fail == 1
        assert snap["02-timing-evaluator"].failing_keys == ("timing.adversarial.k",)

    def test_records_live_skip_from_setup_phase(self) -> None:
        # Live-only tests are skipped during setup when --tier2-live
        # is absent; the call phase never runs.  If the reporter only
        # listened to ``when=='call'`` it would miss every skip.
        reporter = Tier2Reporter()
        reporter.record(
            _fake_report(
                "x/test_component_03_scenario_refiner.py::"
                "test_live_judge_matches_expected_verdict[scenario.golden.k]",
                when="setup",
                outcome="skipped",
            )
        )
        snap = {s.component: s for s in reporter.snapshot()}
        assert snap["03-scenario-refiner"].live_skip == 1

    def test_distinguishes_xfail_from_xpass(self) -> None:
        reporter = Tier2Reporter()
        reporter.record(
            _fake_report(
                "x/test_component_14_pipeline_graph.py::test_corpus_seeded",
                outcome="failed",
                wasxfail="missing corpus",
            )
        )
        reporter.record(
            _fake_report(
                "x/test_component_15_approval_gates.py::test_corpus_seeded",
                outcome="passed",
                wasxfail="missing corpus",
            )
        )
        snap = {s.component: s for s in reporter.snapshot()}
        assert snap["14-pipeline-graph"].xfail == 1
        assert snap["15-approval-gates"].xpass == 1

    def test_ignores_teardown_reports(self) -> None:
        reporter = Tier2Reporter()
        reporter.record(
            _fake_report(
                "x/test_component_01_scenario_agent.py::"
                "test_hermetic_artifact_loads[k]",
                when="teardown",
            )
        )
        snap = {s.component: s for s in reporter.snapshot()}
        assert snap["01-scenario-agent"].hermetic_pass == 0

    def test_ignores_non_component_tests(self) -> None:
        reporter = Tier2Reporter()
        reporter.record(_fake_report("tests/unit/test_harness.py::test_x"))
        # All 15 components are present in the snapshot but empty.
        for stats in reporter.snapshot():
            assert stats.total() == 0


class TestReporterEmit:
    """JSON + markdown rendering."""

    def test_emits_stable_json(self, tmp_path: Path) -> None:
        target = tmp_path / "report.json"
        reporter = Tier2Reporter(report_path=target, live_mode=False)
        reporter.record(
            _fake_report(
                "x/test_component_01_scenario_agent.py::"
                "test_hermetic_artifact_loads[scenario.golden.k]"
            )
        )
        reporter.emit()

        import json as _json

        data = _json.loads(target.read_text())
        assert data["live_mode"] is False
        components = {c["component"]: c for c in data["components"]}
        assert components["01-scenario-agent"]["hermetic_pass"] == 1
        # All 15 present even when only one has data — dashboard
        # diffs rely on the full shape.
        assert len(data["components"]) == 15

    def test_appends_markdown_to_github_summary(self, tmp_path: Path) -> None:
        gh = tmp_path / "summary.md"
        gh.write_text("# existing content\n")
        reporter = Tier2Reporter(github_summary_path=gh, live_mode=True)
        reporter.record(
            _fake_report(
                "x/test_component_01_scenario_agent.py::"
                "test_live_judge_matches_expected_verdict[scenario.golden.k]"
            )
        )
        reporter.emit()

        content = gh.read_text()
        assert content.startswith("# existing content\n")
        assert "Tier-2 atomic-robustness report" in content
        assert "01-scenario-agent" in content
        # Live column should show 1/1 for the one recorded pass.
        assert "1/1" in content

    def test_markdown_reports_failures_separately(self) -> None:
        reporter = Tier2Reporter(live_mode=False)
        reporter.record(
            _fake_report(
                "x/test_component_07_visual_concepter.py::"
                "test_hermetic_artifact_loads[visual.golden.bad]",
                outcome="failed",
            )
        )
        md = reporter.as_markdown()
        assert "FAIL" in md
        assert "visual.golden.bad" in md
        assert "components failed" in md

    def test_markdown_celebrates_clean_run(self) -> None:
        reporter = Tier2Reporter(live_mode=False)
        reporter.record(
            _fake_report(
                "x/test_component_01_scenario_agent.py::test_hermetic_artifact_loads[k]"
            )
        )
        md = reporter.as_markdown()
        assert "All components passed" in md


@pytest.fixture
def dummy_config() -> Any:
    """A stub ``pytest.Config`` that supports ``getoption`` + ``stash``."""

    class _Stash:
        def __init__(self) -> None:
            self._data: dict[Any, Any] = {}

        def __setitem__(self, key: Any, value: Any) -> None:
            self._data[key] = value

        def get(self, key: Any, default: Any = None) -> Any:
            return self._data.get(key, default)

    class _Config:
        def __init__(self, options: dict[str, Any]) -> None:
            self._options = options
            self.stash = _Stash()

        def getoption(self, name: str, default: Any = None) -> Any:
            return self._options.get(name, default)

    return _Config


class TestPluginActivation:
    """``_plugin_from_config`` gate."""

    def test_activates_with_report_path(
        self, dummy_config: Any, tmp_path: Path
    ) -> None:
        from strands_agents.tier2._report import _plugin_from_config

        config = dummy_config({"--tier2-report": str(tmp_path / "r.json")})
        assert _plugin_from_config(config) is not None

    def test_skips_without_any_output_sink(
        self, dummy_config: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from strands_agents.tier2._report import _plugin_from_config

        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        config = dummy_config({"--tier2-report": None})
        assert _plugin_from_config(config) is None

    def test_activates_from_github_summary_env(
        self,
        dummy_config: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from strands_agents.tier2._report import _plugin_from_config

        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "s.md"))
        config = dummy_config({"--tier2-report": None})
        assert _plugin_from_config(config) is not None
