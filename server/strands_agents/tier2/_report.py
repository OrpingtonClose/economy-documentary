"""Pytest plugin that emits a per-component Tier-2 nightly report.

The nightly workflow runs ``pytest --tier2-live`` against the full corpus
and needs a machine-readable summary (to diff against yesterday) plus a
human-readable markdown block (for ``$GITHUB_STEP_SUMMARY``).  Doing this
inside pytest — rather than parsing ``pytest -v`` output afterwards —
lets us see exactly *which* component/artifact failed, not just that
"one test failed".

The plugin activates when either:

- ``--tier2-report=PATH`` is passed, or
- the ``GITHUB_STEP_SUMMARY`` env var is set.

It never fails a test session; it only observes outcomes.  Tests that
hermetic-skip (live-only) are counted separately from live failures so
nightly dashboards can distinguish "judge disagreed" from "fleet
unavailable".
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

import pytest

logger = logging.getLogger(__name__)

# Module filename -> component id derived via the fixed naming convention
# ``test_component_NN_<slug>.py``.  We regex rather than import-time
# registration so the builder stays the only source of truth for which
# component a module grades.
# Nodeid shape pytest gives us:
#   ``path/to/test_component_01_scenario_agent.py::test_foo[artifact.key]``
# We extract the component number+slug from just before the ``.py::``
# boundary so both the numeric prefix and the free-form suffix stay
# loose.
_COMPONENT_RE = re.compile(r"test_component_(\d{2})_([a-z_]+)\.py::")

_KNOWN_COMPONENTS: tuple[str, ...] = (
    "01-scenario-agent",
    "02-timing-evaluator",
    "03-scenario-refiner",
    "04-audio-agent",
    "05-timing-loop",
    "06-content-analyst",
    "07-visual-concepter",
    "08-coherence-evaluator",
    "09-visual-loop",
    "10-production-supervisor",
    "11-assembly-agent",
    "12-recovery-agents",
    "13-escalation-supervisor",
    "14-pipeline-graph",
    "15-approval-gates",
)


@dataclass(frozen=True)
class ComponentStats:
    """Per-component outcome tally.

    A single pytest run populates one ``ComponentStats`` per component;
    the fields mirror pytest's outcome categories plus our own
    ``hermetic`` / ``live`` split.
    """

    component: str
    hermetic_pass: int = 0
    hermetic_fail: int = 0
    live_pass: int = 0
    live_fail: int = 0
    live_skip: int = 0
    xfail: int = 0
    xpass: int = 0
    errors: int = 0
    failing_keys: tuple[str, ...] = ()

    def total(self) -> int:
        return (
            self.hermetic_pass
            + self.hermetic_fail
            + self.live_pass
            + self.live_fail
            + self.live_skip
            + self.xfail
            + self.xpass
            + self.errors
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _component_from_nodeid(nodeid: str) -> Optional[str]:
    """Resolve a test nodeid to a component key.

    Returns None for non-component tests (harness tests, conftest
    helpers) so the report stays focused on the 15 atomic components.
    """
    match = _COMPONENT_RE.search(nodeid)
    if match is None:
        return None
    number, slug = match.group(1), match.group(2)
    slug = slug.replace("_", "-")
    for known in _KNOWN_COMPONENTS:
        if known.startswith(f"{number}-") and known.endswith(slug):
            return known
    # Fallback — synthesise the key even if naming drifts.  Better to
    # log a new component than to silently drop it.
    return f"{number}-{slug}"


def _is_live_test(nodeid: str) -> bool:
    return "test_live_judge_matches_expected_verdict" in nodeid


def _is_hermetic_test(nodeid: str) -> bool:
    return "test_hermetic_artifact_loads" in nodeid


def _is_coverage_gate(nodeid: str) -> bool:
    return "test_corpus_seeded" in nodeid


def _artifact_key_from_nodeid(nodeid: str) -> Optional[str]:
    """Pull the ``[artifact-key]`` parameter id out of a parametrised nodeid."""
    start = nodeid.find("[")
    end = nodeid.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    return nodeid[start + 1 : end]


class Tier2Reporter:
    """Accumulate per-component outcomes and emit a summary.

    Registered as a pytest plugin when the nightly workflow passes
    ``--tier2-report=PATH`` or sets ``GITHUB_STEP_SUMMARY``.  The
    plugin is idempotent — repeated calls to :meth:`emit` are safe,
    and the JSON file is overwritten atomically on final flush.
    """

    def __init__(
        self,
        *,
        report_path: Optional[Path] = None,
        github_summary_path: Optional[Path] = None,
        live_mode: bool = False,
    ) -> None:
        self._stats: dict[str, dict[str, int]] = defaultdict(
            lambda: {
                "hermetic_pass": 0,
                "hermetic_fail": 0,
                "live_pass": 0,
                "live_fail": 0,
                "live_skip": 0,
                "xfail": 0,
                "xpass": 0,
                "errors": 0,
            }
        )
        self._failing_keys: dict[str, list[str]] = defaultdict(list)
        self._report_path = report_path
        self._github_summary_path = github_summary_path
        self._live_mode = live_mode

    def record(self, report: pytest.TestReport) -> None:
        """Record a single test report.

        Pytest emits three reports per test (``setup``, ``call``,
        ``teardown``).  We care about:

        - ``call`` phase for passed / failed outcomes (the normal path).
        - ``setup`` phase for skipped outcomes — skipped tests never
          reach ``call`` because the skip marker fires during setup.
        - ``setup`` phase for errors (fixture failures).

        Everything else is noise and would double-count.
        """
        if report.when == "call":
            pass
        elif report.when == "setup" and report.outcome in {"skipped", "error"}:
            pass
        else:
            return

        component = _component_from_nodeid(report.nodeid)
        if component is None:
            return

        is_live = _is_live_test(report.nodeid)
        is_hermetic = _is_hermetic_test(report.nodeid)

        stats = self._stats[component]

        if report.outcome == "error":
            stats["errors"] += 1
            self._failing_keys[component].append(report.nodeid)
            return

        if hasattr(report, "wasxfail"):
            # Distinguishing xfail (expected failure, passed) vs xpassed
            # (expected failure, unexpectedly passed) matters for drift
            # detection: xpassed means the corpus/rubric has likely
            # changed and the xfail should be removed.
            if report.outcome == "passed":
                stats["xpass"] += 1
            else:
                stats["xfail"] += 1
            return

        if is_hermetic:
            if report.outcome == "passed":
                stats["hermetic_pass"] += 1
            elif report.outcome == "failed":
                stats["hermetic_fail"] += 1
                key = _artifact_key_from_nodeid(report.nodeid) or report.nodeid
                self._failing_keys[component].append(key)
        elif is_live:
            if report.outcome == "passed":
                stats["live_pass"] += 1
            elif report.outcome == "failed":
                stats["live_fail"] += 1
                key = _artifact_key_from_nodeid(report.nodeid) or report.nodeid
                self._failing_keys[component].append(key)
            elif report.outcome == "skipped":
                stats["live_skip"] += 1

    def snapshot(self) -> list[ComponentStats]:
        """Return the current per-component tally as frozen records."""
        result: list[ComponentStats] = []
        for component in _KNOWN_COMPONENTS:
            stats = self._stats.get(component, {})
            result.append(
                ComponentStats(
                    component=component,
                    hermetic_pass=stats.get("hermetic_pass", 0),
                    hermetic_fail=stats.get("hermetic_fail", 0),
                    live_pass=stats.get("live_pass", 0),
                    live_fail=stats.get("live_fail", 0),
                    live_skip=stats.get("live_skip", 0),
                    xfail=stats.get("xfail", 0),
                    xpass=stats.get("xpass", 0),
                    errors=stats.get("errors", 0),
                    failing_keys=tuple(self._failing_keys.get(component, ())),
                )
            )
        return result

    def as_json(self) -> dict[str, Any]:
        return {
            "live_mode": self._live_mode,
            "components": [s.as_dict() for s in self.snapshot()],
        }

    def as_markdown(self) -> str:
        """Render a GitHub-flavoured markdown summary.

        Table has one row per component.  The dashboard-friendly shape:

        | Component | Hermetic | Live | Status |
        | --- | ---: | ---: | :---: |
        | 01-scenario-agent | 2/2 | 2/2 | ✅ |

        A summary line at the end reports totals + any components that
        failed.  ASCII-only to stay readable on terminal pipes.
        """
        lines: list[str] = []
        mode = "live" if self._live_mode else "hermetic-only"
        lines.append(f"## Tier-2 atomic-robustness report ({mode})")
        lines.append("")
        lines.append("| Component | Hermetic | Live | xfail | Status |")
        lines.append("| --- | ---: | ---: | ---: | :---: |")

        any_failed = False
        per_component_total = 0
        per_component_failed = 0

        for snap in self.snapshot():
            hermetic_total = snap.hermetic_pass + snap.hermetic_fail
            live_total = snap.live_pass + snap.live_fail
            hermetic_cell = (
                f"{snap.hermetic_pass}/{hermetic_total}" if hermetic_total else "-"
            )
            if self._live_mode:
                live_cell = (
                    f"{snap.live_pass}/{live_total}"
                    if live_total
                    else ("skipped" if snap.live_skip else "-")
                )
            else:
                live_cell = "-"
            xfail_cell = str(snap.xfail) if snap.xfail else "-"

            component_failed = (
                snap.hermetic_fail > 0 or snap.live_fail > 0 or snap.errors > 0
            )
            if component_failed:
                any_failed = True
                per_component_failed += 1
            if hermetic_total or live_total or snap.xfail or snap.xpass:
                per_component_total += 1

            status = (
                "FAIL"
                if component_failed
                else ("OK" if (hermetic_total or live_total) else "skip")
            )
            lines.append(
                f"| `{snap.component}` | {hermetic_cell} | {live_cell} | "
                f"{xfail_cell} | {status} |"
            )

        lines.append("")
        if any_failed:
            lines.append(
                f"**{per_component_failed}/{per_component_total} components "
                f"failed.**  See the detailed failure list below."
            )
            lines.append("")
            for snap in self.snapshot():
                if not snap.failing_keys:
                    continue
                lines.append(f"### `{snap.component}` failures")
                for key in snap.failing_keys:
                    lines.append(f"- `{key}`")
                lines.append("")
        else:
            lines.append("All components passed.")

        return "\n".join(lines) + "\n"

    # ---- pytest hook methods ---------------------------------------
    # Registering the reporter via ``pluginmanager.register`` makes pytest
    # call hook methods on the instance.  Module-level hook wrappers would
    # have to fish the reporter out of ``config.stash``; making them
    # methods avoids the lookup and keeps ownership clear.

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self.record(report)

    def pytest_sessionfinish(
        self,
        session: pytest.Session,  # noqa: ARG002 — session unused, hook signature
        exitstatus: int,  # noqa: ARG002 — exitstatus unused, hook signature
    ) -> None:
        self.emit()

    def emit(self) -> None:
        """Flush the JSON + markdown outputs.  Called on sessionfinish."""
        if self._report_path is not None:
            tmp = self._report_path.with_suffix(self._report_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.as_json(), indent=2, sort_keys=True))
            tmp.replace(self._report_path)
            logger.info(
                "path=<%s> | tier2 report json written",
                self._report_path,
            )

        if self._github_summary_path is not None:
            # GITHUB_STEP_SUMMARY is append-only — one pytest run may
            # live alongside other summary chunks.
            with self._github_summary_path.open("a", encoding="utf-8") as fh:
                fh.write(self.as_markdown())
            logger.info(
                "path=<%s> | tier2 markdown summary appended",
                self._github_summary_path,
            )


def build_reporter_from_config(
    config: pytest.Config,
) -> Optional[Tier2Reporter]:
    """Construct a reporter if either sink (JSON path or GH summary) is set.

    Called from the tier2 conftest's ``pytest_configure`` hook.  Kept
    in this module so the activation logic lives alongside the
    reporter class itself — the conftest is just the registration
    point.

    Returns ``None`` when no output sink is configured, in which case
    the reporter is deliberately not installed.  This keeps the
    hermetic PR-gate runs zero-overhead.
    """
    report_path_str = config.getoption("--tier2-report", default=None)
    report_path = Path(report_path_str) if report_path_str else None

    gh_path_str = os.environ.get("GITHUB_STEP_SUMMARY")
    gh_path = Path(gh_path_str) if gh_path_str else None

    if report_path is None and gh_path is None:
        return None

    live_mode = bool(config.getoption("--tier2-live", default=False))
    return Tier2Reporter(
        report_path=report_path,
        github_summary_path=gh_path,
        live_mode=live_mode,
    )


# Legacy alias — older tests import ``_plugin_from_config``.  Kept
# as a thin wrapper so the public name is the expressive one.
_plugin_from_config = build_reporter_from_config


reporter_stash_key: pytest.StashKey[Tier2Reporter] = pytest.StashKey()
