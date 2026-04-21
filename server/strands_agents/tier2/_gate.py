"""CLI gate that turns a Tier-2 report into an exit code.

Reads the JSON produced by :mod:`strands_agents.tier2._report` and decides
whether the run represents a regression.  Used both by the per-PR
``strands-evals.yml`` workflow (hermetic-only gate, blocks merges) and by
future PR-F+ workflows (live + hermetic gates, nightly / on-demand).

Exit codes:

* ``0``  — no regression within the declared mode.
* ``1``  — at least one gated component regressed (hermetic failures,
  errors, or — in ``live`` mode — live failures).
* ``2``  — structural problem (missing file, malformed JSON, no
  components).  Treated as a hard failure so operators notice the
  infrastructure is wrong, not silently green.

The script is intentionally Python-only (``json`` + ``argparse``) so it
can run without the strands extras installed in any workflow job that
just wants to enforce the gate.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ComponentStatus:
    """Regression status for a single component."""

    component: str
    hermetic_fail: int
    errors: int
    live_fail: int
    failing_keys: tuple[str, ...]

    def is_red(self, *, mode: str) -> bool:
        """Return ``True`` if this component counts as regressed.

        Args:
            mode: ``"hermetic"`` to only count hermetic failures + errors;
                ``"live"`` to additionally count live failures.
        """
        if self.hermetic_fail > 0 or self.errors > 0:
            return True
        if mode == "live" and self.live_fail > 0:
            return True
        return False


def parse_report(path: pathlib.Path) -> tuple[bool, list[ComponentStatus]]:
    """Parse a Tier-2 report file.

    Args:
        path: Filesystem path to the JSON report produced by the
            reporter plugin.

    Returns:
        A ``(live_mode, components)`` tuple.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        ValueError: If the file is not valid JSON or is missing the
            ``components`` key.
    """
    raw = path.read_text()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"tier2 report at {path} is not valid JSON: {exc}"
        raise ValueError(msg) from exc

    if not isinstance(data, dict) or "components" not in data:
        msg = f"tier2 report at {path} missing top-level 'components' list"
        raise ValueError(msg)

    live_mode = bool(data.get("live_mode", False))
    components = [_to_status(entry) for entry in data["components"]]
    if not components:
        msg = f"tier2 report at {path} has no components"
        raise ValueError(msg)
    return live_mode, components


def _to_status(entry: dict[str, object]) -> ComponentStatus:
    return ComponentStatus(
        component=str(entry.get("component", "<unknown>")),
        hermetic_fail=int(entry.get("hermetic_fail", 0) or 0),
        errors=int(entry.get("errors", 0) or 0),
        live_fail=int(entry.get("live_fail", 0) or 0),
        failing_keys=tuple(str(k) for k in entry.get("failing_keys", []) or ()),
    )


def red_components(
    components: Iterable[ComponentStatus], *, mode: str
) -> list[ComponentStatus]:
    """Return the components that count as regressed under ``mode``."""
    return [c for c in components if c.is_red(mode=mode)]


def format_report(reds: Sequence[ComponentStatus], *, mode: str) -> str:
    """Render a compact, grep-friendly summary of the red components."""
    if not reds:
        return f"Tier-2 gate ({mode}): all components green."
    lines = [f"Tier-2 gate ({mode}) failed — {len(reds)} red component(s):"]
    for c in reds:
        detail = (
            f"hermetic_fail={c.hermetic_fail} errors={c.errors} "
            f"live_fail={c.live_fail}"
        )
        keys = ", ".join(c.failing_keys) if c.failing_keys else "(none)"
        lines.append(f"  - {c.component}: {detail}")
        lines.append(f"      failing keys: {keys}")
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tier2-gate",
        description=(
            "Fail the current CI job if the Tier-2 report contains "
            "per-component regressions."
        ),
    )
    parser.add_argument(
        "--report",
        type=pathlib.Path,
        required=True,
        help="Path to the JSON report emitted by --tier2-report.",
    )
    parser.add_argument(
        "--mode",
        choices=("hermetic", "live"),
        default="hermetic",
        help=(
            "Which failure classes count as regressions.  'hermetic' "
            "(default) fails on hermetic_fail or errors; 'live' also "
            "fails on live_fail (use in nightly after PR-F)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the gate and return a process exit code."""
    args = _parse_args(argv)
    try:
        _, components = parse_report(args.report)
    except FileNotFoundError:
        print(f"::error::Tier-2 gate: report file not found at {args.report}")
        return 2
    except ValueError as exc:
        print(f"::error::Tier-2 gate: {exc}")
        return 2

    reds = red_components(components, mode=args.mode)
    summary = format_report(reds, mode=args.mode)

    # GitHub Actions annotation so the failure surfaces on the "Summary"
    # tab and is clickable in the file tree.
    if reds:
        names = ", ".join(c.component for c in reds)
        print(f"::error::Tier-2 {args.mode} regressions in: {names}")
    print(summary)

    # Append a GitHub-flavoured block to the step summary when GH set
    # the env var.  This is useful even on green runs — the nightly
    # dashboard can scrape every run's summary to chart trends.
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        block = ["### Tier-2 gate"]
        block.append(f"- mode: `{args.mode}`")
        block.append(f"- status: {'RED' if reds else 'green'}")
        if reds:
            block.append("- red components:")
            for c in reds:
                block.append(
                    f"  - `{c.component}` — hermetic_fail={c.hermetic_fail} "
                    f"errors={c.errors} live_fail={c.live_fail}"
                )
        with pathlib.Path(summary_path).open("a", encoding="utf-8") as fh:
            fh.write("\n".join(block) + "\n\n")

    return 1 if reds else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
