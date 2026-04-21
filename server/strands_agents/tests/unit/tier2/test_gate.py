"""Unit tests for the Tier-2 regression gate."""

from __future__ import annotations

import json
import pathlib

import pytest

from strands_agents.tier2._gate import (
    ComponentStatus,
    format_report,
    main,
    parse_report,
    red_components,
)


@pytest.fixture
def report_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path


def _write(
    path: pathlib.Path, *, live_mode: bool, components: list[dict[str, object]]
) -> pathlib.Path:
    path.write_text(json.dumps({"live_mode": live_mode, "components": components}))
    return path


class TestComponentStatus:
    def test_is_red_on_hermetic_fail(self) -> None:
        c = ComponentStatus("01-a", 1, 0, 0, ())
        assert c.is_red(mode="hermetic") is True
        assert c.is_red(mode="live") is True

    def test_is_red_on_errors(self) -> None:
        c = ComponentStatus("01-a", 0, 2, 0, ())
        assert c.is_red(mode="hermetic") is True
        assert c.is_red(mode="live") is True

    def test_live_fail_only_red_in_live_mode(self) -> None:
        c = ComponentStatus("01-a", 0, 0, 3, ())
        assert c.is_red(mode="hermetic") is False
        assert c.is_red(mode="live") is True

    def test_green(self) -> None:
        c = ComponentStatus("01-a", 0, 0, 0, ())
        assert c.is_red(mode="hermetic") is False
        assert c.is_red(mode="live") is False


class TestParseReport:
    def test_parses_valid(self, report_dir: pathlib.Path) -> None:
        p = _write(
            report_dir / "r.json",
            live_mode=False,
            components=[
                {
                    "component": "01-scenario-agent",
                    "hermetic_pass": 3,
                    "hermetic_fail": 0,
                    "errors": 0,
                    "live_fail": 0,
                    "failing_keys": [],
                }
            ],
        )
        live, comps = parse_report(p)
        assert live is False
        assert len(comps) == 1
        assert comps[0].component == "01-scenario-agent"

    def test_missing_file_raises(self, report_dir: pathlib.Path) -> None:
        with pytest.raises(FileNotFoundError):
            parse_report(report_dir / "missing.json")

    def test_invalid_json_raises(self, report_dir: pathlib.Path) -> None:
        p = report_dir / "bad.json"
        p.write_text("{not valid")
        with pytest.raises(ValueError, match="not valid JSON"):
            parse_report(p)

    def test_missing_components_raises(self, report_dir: pathlib.Path) -> None:
        p = report_dir / "empty.json"
        p.write_text(json.dumps({"live_mode": False}))
        with pytest.raises(ValueError, match="missing top-level 'components'"):
            parse_report(p)

    def test_empty_components_list_raises(self, report_dir: pathlib.Path) -> None:
        p = _write(report_dir / "z.json", live_mode=False, components=[])
        with pytest.raises(ValueError, match="no components"):
            parse_report(p)


class TestRedComponents:
    def test_filters_to_red_in_hermetic_mode(self) -> None:
        comps = [
            ComponentStatus("01", 1, 0, 0, ("k1",)),
            ComponentStatus("02", 0, 0, 0, ()),
            ComponentStatus("03", 0, 0, 5, ()),
        ]
        reds = red_components(comps, mode="hermetic")
        assert [c.component for c in reds] == ["01"]

    def test_includes_live_fail_in_live_mode(self) -> None:
        comps = [
            ComponentStatus("01", 1, 0, 0, ()),
            ComponentStatus("02", 0, 0, 0, ()),
            ComponentStatus("03", 0, 0, 5, ()),
        ]
        reds = red_components(comps, mode="live")
        assert [c.component for c in reds] == ["01", "03"]


class TestFormatReport:
    def test_all_green_message(self) -> None:
        out = format_report([], mode="hermetic")
        assert "all components green" in out

    def test_renders_failing_keys(self) -> None:
        reds = [ComponentStatus("07-visual", 2, 0, 0, ("art.bad1", "art.bad2"))]
        out = format_report(reds, mode="hermetic")
        assert "07-visual" in out
        assert "hermetic_fail=2" in out
        assert "art.bad1" in out
        assert "art.bad2" in out


class TestMainCli:
    def test_exit_0_on_green_report(
        self,
        report_dir: pathlib.Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        p = _write(
            report_dir / "g.json",
            live_mode=False,
            components=[
                {
                    "component": "01-a",
                    "hermetic_pass": 1,
                    "hermetic_fail": 0,
                    "errors": 0,
                    "live_fail": 0,
                    "failing_keys": [],
                }
            ],
        )
        rc = main(["--report", str(p)])
        assert rc == 0
        assert "all components green" in capsys.readouterr().out

    def test_exit_1_on_hermetic_fail(
        self,
        report_dir: pathlib.Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        p = _write(
            report_dir / "r.json",
            live_mode=False,
            components=[
                {
                    "component": "07-visual",
                    "hermetic_pass": 1,
                    "hermetic_fail": 2,
                    "errors": 0,
                    "live_fail": 0,
                    "failing_keys": ["art.bad"],
                }
            ],
        )
        rc = main(["--report", str(p)])
        assert rc == 1
        out = capsys.readouterr().out
        assert "::error::Tier-2 hermetic regressions" in out
        assert "07-visual" in out
        assert "art.bad" in out

    def test_exit_0_on_live_fail_in_hermetic_mode(
        self, report_dir: pathlib.Path
    ) -> None:
        p = _write(
            report_dir / "live_only.json",
            live_mode=True,
            components=[
                {
                    "component": "07-visual",
                    "hermetic_pass": 1,
                    "hermetic_fail": 0,
                    "errors": 0,
                    "live_fail": 4,
                    "failing_keys": [],
                }
            ],
        )
        assert main(["--report", str(p), "--mode", "hermetic"]) == 0

    def test_exit_1_on_live_fail_in_live_mode(self, report_dir: pathlib.Path) -> None:
        p = _write(
            report_dir / "live_only.json",
            live_mode=True,
            components=[
                {
                    "component": "07-visual",
                    "hermetic_pass": 1,
                    "hermetic_fail": 0,
                    "errors": 0,
                    "live_fail": 4,
                    "failing_keys": [],
                }
            ],
        )
        assert main(["--report", str(p), "--mode", "live"]) == 1

    def test_exit_2_on_missing_file(
        self,
        report_dir: pathlib.Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = main(["--report", str(report_dir / "absent.json")])
        assert rc == 2
        assert "::error::Tier-2 gate: report file not found" in capsys.readouterr().out

    def test_exit_2_on_invalid_json(
        self,
        report_dir: pathlib.Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        p = report_dir / "bad.json"
        p.write_text("not json")
        rc = main(["--report", str(p)])
        assert rc == 2
        assert "::error::Tier-2 gate:" in capsys.readouterr().out

    def test_writes_github_step_summary(
        self,
        report_dir: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        p = _write(
            report_dir / "r.json",
            live_mode=False,
            components=[
                {
                    "component": "07-visual",
                    "hermetic_pass": 1,
                    "hermetic_fail": 2,
                    "errors": 0,
                    "live_fail": 0,
                    "failing_keys": ["art.bad"],
                }
            ],
        )
        summary = report_dir / "step_summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        assert main(["--report", str(p)]) == 1
        text = summary.read_text()
        assert "Tier-2 gate" in text
        assert "07-visual" in text
        assert "RED" in text
