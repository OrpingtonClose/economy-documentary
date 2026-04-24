"""Unit tests for scripts/append_lesson.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "append_lesson.py"


def _load_module() -> object:
    spec = importlib.util.spec_from_file_location("append_lesson", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["append_lesson"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> object:
    return _load_module()


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    path = tmp_path / "gpu-sizing.md"
    path.write_text(
        "# GPU sizing ledger\n\n"
        "Seed observations feed the downshift decisions.\n\n"
        "<!-- NEW ENTRIES APPENDED BELOW -->\n",
        encoding="utf-8",
    )
    return path


def test_append_entry_writes_frontmatter_and_body(
    mod: object, ledger: Path
) -> None:
    entry = mod.append_entry(  # type: ignore[attr-defined]
        ledger,
        title="2026-04-22 — A10 / Qwen3-TTS",
        observed="2026-04-22",
        source="slice-4b",
        severity="info",
        tags=["qwen3-tts", "vram"],
        body="- vram_peak_gb: 18\n- disk_peak_gb: 40\n",
    )

    contents = ledger.read_text(encoding="utf-8")
    assert "### 2026-04-22 — A10 / Qwen3-TTS" in contents
    assert "observed: 2026-04-22" in contents
    assert "source: slice-4b" in contents
    assert "severity: info" in contents
    assert "tags: [qwen3-tts, vram]" in contents
    assert "vram_peak_gb: 18" in contents
    assert entry.strip().startswith("### 2026-04-22")


def test_append_entry_keeps_marker_so_next_append_works(
    mod: object, ledger: Path
) -> None:
    mod.append_entry(  # type: ignore[attr-defined]
        ledger,
        title="t1",
        observed="2026-04-22",
        source="slice-4b",
        severity="info",
        tags=[],
        body="body1",
    )
    mod.append_entry(  # type: ignore[attr-defined]
        ledger,
        title="t2",
        observed="2026-04-23",
        source="slice-4b",
        severity="friction",
        tags=["gpu"],
        body="body2",
    )

    contents = ledger.read_text(encoding="utf-8")
    assert contents.count("<!-- NEW ENTRIES APPENDED BELOW -->") == 1
    # New entries are inserted immediately after the marker, so the
    # newest entry ends up above the older one (reverse-chronological).
    t1_idx = contents.index("### t1")
    t2_idx = contents.index("### t2")
    assert t2_idx < t1_idx


def test_append_entry_dry_run_does_not_write(mod: object, ledger: Path) -> None:
    before = ledger.read_text(encoding="utf-8")

    mod.append_entry(  # type: ignore[attr-defined]
        ledger,
        title="t",
        observed="2026-04-22",
        source="s",
        severity="info",
        tags=[],
        body="b",
        dry_run=True,
    )

    assert ledger.read_text(encoding="utf-8") == before


def test_append_entry_rejects_bad_severity(mod: object, ledger: Path) -> None:
    with pytest.raises(ValueError, match="severity must be one of"):
        mod.append_entry(  # type: ignore[attr-defined]
            ledger,
            title="t",
            observed="2026-04-22",
            source="s",
            severity="critical",
            tags=[],
            body="b",
        )


def test_append_entry_rejects_bad_date(mod: object, ledger: Path) -> None:
    with pytest.raises(ValueError, match="observed date must be"):
        mod.append_entry(  # type: ignore[attr-defined]
            ledger,
            title="t",
            observed="april-22",
            source="s",
            severity="info",
            tags=[],
            body="b",
        )


def test_append_entry_rejects_missing_marker(
    mod: object, tmp_path: Path
) -> None:
    path = tmp_path / "no-marker.md"
    path.write_text("# just a header\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing append marker"):
        mod.append_entry(  # type: ignore[attr-defined]
            path,
            title="t",
            observed="2026-04-22",
            source="s",
            severity="info",
            tags=[],
            body="b",
        )


def test_parse_tags_strips_and_drops_empties(mod: object) -> None:
    assert mod._parse_tags("a, b , ,c") == ["a", "b", "c"]  # type: ignore[attr-defined]
    assert mod._parse_tags("") == []  # type: ignore[attr-defined]


def test_format_tags_renders_yaml_inline(mod: object) -> None:
    assert mod._format_tags([]) == "[]"  # type: ignore[attr-defined]
    assert mod._format_tags(["a", "b"]) == "[a, b]"  # type: ignore[attr-defined]


def test_append_entry_rejects_empty_title(mod: object, ledger: Path) -> None:
    with pytest.raises(ValueError, match="title must be non-empty"):
        mod.append_entry(  # type: ignore[attr-defined]
            ledger,
            title="   ",
            observed="2026-04-22",
            source="s",
            severity="info",
            tags=[],
            body="b",
        )


def test_append_entry_rejects_empty_source(mod: object, ledger: Path) -> None:
    with pytest.raises(ValueError, match="source must be non-empty"):
        mod.append_entry(  # type: ignore[attr-defined]
            ledger,
            title="t",
            observed="2026-04-22",
            source=" ",
            severity="info",
            tags=[],
            body="b",
        )
