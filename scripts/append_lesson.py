#!/usr/bin/env python3
"""Append a lesson entry to a markdown ledger under docs/strands-migration/lessons/.

Ledger files are append-only: this script writes one entry with validated
frontmatter + body below the `<!-- NEW ENTRIES APPENDED BELOW -->` marker.

Usage:
    python scripts/append_lesson.py \\
        --ledger gpu-sizing \\
        --source slice-4b \\
        --severity info \\
        --tags qwen3-tts,vram \\
        --body-file /tmp/entry.md

    # or inline body
    python scripts/append_lesson.py \\
        --ledger cost-telemetry \\
        --source slice-4b \\
        --severity info \\
        --tags qwen3-tts,cost \\
        --body "- vm_class: L4 (24 GB)\\n- \$/hr: 0.32\\n- wall_minutes: 12\\n"

Frontmatter validated:
    observed: YYYY-MM-DD (default: today, UTC)
    source: run_id | pr | slice | manual
    severity: info | friction | incident
    tags: comma-separated list
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

LESSONS_DIR = Path(__file__).resolve().parent.parent / "docs" / "strands-migration" / "lessons"

VALID_SEVERITIES = {"info", "friction", "incident"}
APPEND_MARKER = "<!-- NEW ENTRIES APPENDED BELOW -->"

KNOWN_LEDGERS = {
    "gpu-sizing": LESSONS_DIR / "gpu-sizing.md",
    "cost-telemetry": LESSONS_DIR / "cost-telemetry.md",
    "guardian-tuning": LESSONS_DIR / "guardian-tuning.md",
}


def _today_utc_iso() -> str:
    """Return today's UTC date in ISO format."""
    return datetime.now(timezone.utc).date().isoformat()


def _validate_date(date_str: str) -> str:
    """Validate YYYY-MM-DD format. Raise ValueError on bad input."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        raise ValueError(f"observed date must be YYYY-MM-DD, got {date_str!r}")
    datetime.fromisoformat(date_str)
    return date_str


def _validate_severity(severity: str) -> str:
    """Validate severity is one of the known values."""
    if severity not in VALID_SEVERITIES:
        raise ValueError(
            f"severity must be one of {sorted(VALID_SEVERITIES)}, got {severity!r}"
        )
    return severity


def _parse_tags(tags_raw: str) -> list[str]:
    """Parse comma-separated tags, strip whitespace, drop empties."""
    return [t.strip() for t in tags_raw.split(",") if t.strip()]


def _format_tags(tags: list[str]) -> str:
    """Format tags list as YAML inline list."""
    if not tags:
        return "[]"
    return "[" + ", ".join(tags) + "]"


def _render_entry(
    *,
    title: str,
    observed: str,
    source: str,
    severity: str,
    tags: list[str],
    body: str,
) -> str:
    """Render a single ledger entry with frontmatter + body."""
    frontmatter = (
        f"```yaml\n"
        f"observed: {observed}\n"
        f"source: {source}\n"
        f"severity: {severity}\n"
        f"tags: {_format_tags(tags)}\n"
        f"```"
    )
    body_stripped = body.strip()
    return f"\n### {title}\n\n{frontmatter}\n\n{body_stripped}\n"


def append_entry(
    ledger_path: Path,
    *,
    title: str,
    observed: str,
    source: str,
    severity: str,
    tags: list[str],
    body: str,
    dry_run: bool = False,
) -> str:
    """Append an entry to a ledger file at the append marker.

    Returns the rendered entry string (useful for tests / dry runs).
    Raises FileNotFoundError if the ledger doesn't exist.
    Raises ValueError if the append marker is missing.
    """
    if not ledger_path.exists():
        raise FileNotFoundError(f"ledger file not found: {ledger_path}")

    content = ledger_path.read_text(encoding="utf-8")
    if APPEND_MARKER not in content:
        raise ValueError(
            f"ledger {ledger_path.name} missing append marker {APPEND_MARKER!r}"
        )

    _validate_date(observed)
    _validate_severity(severity)
    if not source.strip():
        raise ValueError("source must be non-empty")
    if not title.strip():
        raise ValueError("title must be non-empty")

    entry = _render_entry(
        title=title,
        observed=observed,
        source=source,
        severity=severity,
        tags=tags,
        body=body,
    )

    new_content = content.replace(
        APPEND_MARKER,
        APPEND_MARKER + entry,
        1,
    )

    if not dry_run:
        ledger_path.write_text(new_content, encoding="utf-8")

    return entry


def _resolve_ledger(ledger: str) -> Path:
    """Resolve a ledger name or relative path to an absolute path."""
    if ledger in KNOWN_LEDGERS:
        return KNOWN_LEDGERS[ledger]
    candidate = LESSONS_DIR / f"{ledger}.md"
    if candidate.exists():
        return candidate
    direct = Path(ledger)
    if direct.exists():
        return direct
    raise ValueError(
        f"unknown ledger {ledger!r}; known: {sorted(KNOWN_LEDGERS)}"
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Append a validated lesson entry to a markdown ledger.",
    )
    parser.add_argument(
        "--ledger",
        required=True,
        help="Ledger name (e.g. gpu-sizing) or relative path.",
    )
    parser.add_argument(
        "--title",
        required=True,
        help="Entry title (will be rendered as a level-3 heading).",
    )
    parser.add_argument(
        "--observed",
        default=None,
        help="Observation date YYYY-MM-DD (default: today UTC).",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="run_id, pr, slice label, or 'manual'.",
    )
    parser.add_argument(
        "--severity",
        default="info",
        choices=sorted(VALID_SEVERITIES),
        help="info | friction | incident (default: info).",
    )
    parser.add_argument(
        "--tags",
        default="",
        help="Comma-separated tag list.",
    )
    body_group = parser.add_mutually_exclusive_group(required=True)
    body_group.add_argument(
        "--body",
        help="Body markdown as an inline string.",
    )
    body_group.add_argument(
        "--body-file",
        help="Path to a file containing the body markdown.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and render without writing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        ledger_path = _resolve_ledger(args.ledger)
    except ValueError as exc:
        parser.error(str(exc))

    if args.body is not None:
        body_text = args.body.replace("\\n", "\n")
    else:
        body_text = Path(args.body_file).read_text(encoding="utf-8")

    observed = args.observed or _today_utc_iso()
    tags = _parse_tags(args.tags)

    try:
        entry = append_entry(
            ledger_path,
            title=args.title,
            observed=observed,
            source=args.source,
            severity=args.severity,
            tags=tags,
            body=body_text,
            dry_run=args.dry_run,
        )
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    action = "would append" if args.dry_run else "appended"
    sys.stdout.write(f"{action} to {ledger_path.name}:\n{entry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
