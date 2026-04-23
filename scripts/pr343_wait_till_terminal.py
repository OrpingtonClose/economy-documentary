#!/usr/bin/env python3
"""PR #343 wait-till-terminal test harness.

Kicks a live playground run against a deployed backend, polls
``GET /playground/runs/<run_id>`` once per second until the run
reaches a terminal event (``run.ok`` / ``run.error`` / ``run.cancelled``),
then asserts the PR #343 state predicates over the full event list.

Contract (see ``docs/strands-migration/deploy/pr-343-inner-tool-events-test-plan.md``):

1. Wait till completion, always. No time cutoff from the test side;
   a single 15-minute harness safety timeout distinguishes
   "agent did not terminate" from a hung harness.
2. During the wait, the narrator must emit at least once per 3 s.
3. At terminal, assert narration diversity, inner-loop tool trajectory,
   and stall-rail suppression (as a pure predicate over events).

Exit codes:
    0 — all predicates passed.
    1 — at least one predicate failed.
    2 — harness safety timeout (run did not terminate within 15 min).
    3 — harness error (HTTP / JSON parse / unreachable backend).

Usage:
    python scripts/pr343_wait_till_terminal.py \\
        --base-url http://142.171.48.138:29561 \\
        --component c01 \\
        --case economics_basics

The output is verbose by design — the user's testing rule is "do not
paraphrase results; quote the exact narration line text."
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_BASE_URL = "http://142.171.48.138:29561"
_DEFAULT_COMPONENT = "c01"
_DEFAULT_CASE = "economics_basics"

_POLL_INTERVAL_SECONDS = 1.0
_NARRATOR_SILENCE_WARN_SECONDS = 3.0
_HARNESS_SAFETY_TIMEOUT_SECONDS = 15 * 60

_TERMINAL_KINDS = frozenset({"run.ok", "run.error", "run.cancelled"})
_SCENARIO_TOOL_NAMES = frozenset(
    {"generate_scenario", "evaluate_scenario", "refine_scenario", "create_timeline"}
)
_RICH_DETAIL_SUBSTRINGS = (
    "step=",
    "elapsed_ms=",
    "rating=",
    "num_scenes=",
    "num_issues=",
    "returning",
    "completed in",
)
_TOOL_NAME_IN_NARRATION_RE = re.compile(
    r"generate_scenario|evaluate_scenario|refine_scenario|create_timeline|tool\.called"
)


# ---------------------------------------------------------------------------
# Result reporting
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PredicateResult:
    name: str
    passed: bool
    detail: str


def _print_result(result: PredicateResult) -> None:
    tag = "PASS" if result.passed else "FAIL"
    print(f"  [{tag}] {result.name} — {result.detail}")


# ---------------------------------------------------------------------------
# HTTP helpers — stdlib-only so this runs on any box with Python 3.11+
# ---------------------------------------------------------------------------


def _post_json(url: str, body: dict[str, Any], *, timeout: float = 10.0) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = resp.read().decode("utf-8")
    return json.loads(payload)


def _get_json(url: str, *, timeout: float = 10.0) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = resp.read().decode("utf-8")
    return json.loads(payload)


# ---------------------------------------------------------------------------
# Predicates — all take the terminal snapshot and return a PredicateResult
# ---------------------------------------------------------------------------


def _predicate_narration_cadence(
    snapshot: dict[str, Any],
    *,
    wait_duration_seconds: float,
) -> PredicateResult:
    events = snapshot.get("events", [])
    narrates = [e for e in events if e.get("kind") == "narrate"]
    required = max(1, int(wait_duration_seconds / 3.0))
    n = len(narrates)
    passed = n >= required
    return PredicateResult(
        name="narration.cadence",
        passed=passed,
        detail=(
            f"narrate_events={n}, required_at_least={required} "
            f"(wait_duration_seconds={wait_duration_seconds:.1f})"
        ),
    )


def _predicate_narration_distinctness(snapshot: dict[str, Any]) -> PredicateResult:
    events = snapshot.get("events", [])
    summaries = [e.get("summary", "") for e in events if e.get("kind") == "narrate"]
    distinct = {s for s in summaries if s}
    passed = len(distinct) >= 3
    return PredicateResult(
        name="narration.distinct_summaries",
        passed=passed,
        detail=(
            f"distinct={len(distinct)} required_at_least=3. "
            f"samples={sorted(distinct)[:5]}"
        ),
    )


def _predicate_narration_honest_repetition(snapshot: dict[str, Any]) -> PredicateResult:
    events = snapshot.get("events", [])
    summaries = [e.get("summary", "") for e in events if e.get("kind") == "narrate"]
    matches = [s for s in summaries if "no new signal, still on" in s]
    passed = len(matches) >= 1
    return PredicateResult(
        name="narration.honest_repetition",
        passed=passed,
        detail=(
            f"matches={len(matches)} required_at_least=1. "
            f"first_match={matches[0] if matches else 'N/A'}"
        ),
    )


def _predicate_narration_cites_tool(snapshot: dict[str, Any]) -> PredicateResult:
    events = snapshot.get("events", [])
    summaries = [e.get("summary", "") for e in events if e.get("kind") == "narrate"]
    matches = [s for s in summaries if _TOOL_NAME_IN_NARRATION_RE.search(s)]
    passed = len(matches) >= 1
    return PredicateResult(
        name="narration.cites_scenario_tool",
        passed=passed,
        detail=(
            f"matches={len(matches)} required_at_least=1. "
            f"first_match={matches[0] if matches else 'N/A'}"
        ),
    )


def _predicate_narration_surfaces_detail(snapshot: dict[str, Any]) -> PredicateResult:
    events = snapshot.get("events", [])
    summaries = [e.get("summary", "") for e in events if e.get("kind") == "narrate"]
    matches = [s for s in summaries if any(sub in s for sub in _RICH_DETAIL_SUBSTRINGS)]
    passed = len(matches) >= 1
    return PredicateResult(
        name="narration.surfaces_rich_detail",
        passed=passed,
        detail=(
            f"matches={len(matches)} required_at_least=1. "
            f"first_match={matches[0] if matches else 'N/A'}"
        ),
    )


def _predicate_inner_tool_trajectory(snapshot: dict[str, Any]) -> PredicateResult:
    events = snapshot.get("events", [])
    called = [e for e in events if e.get("kind") == "tool.called"]
    returned = [e for e in events if e.get("kind") == "tool.returned"]
    called_with_scenario_tool = [
        e for e in called if (e.get("detail") or {}).get("tool") in _SCENARIO_TOOL_NAMES
    ]
    returned_with_positive_elapsed = [
        e
        for e in returned
        if isinstance((e.get("detail") or {}).get("elapsed_ms"), (int, float))
        and (e.get("detail") or {}).get("elapsed_ms", 0) > 0
    ]
    called_ids = {(e.get("detail") or {}).get("tool_use_id") for e in called}
    orphan_returns = [
        e
        for e in returned
        if (e.get("detail") or {}).get("tool_use_id") not in called_ids
    ]
    steps = [
        (e.get("detail") or {}).get("step")
        for e in called
        if isinstance((e.get("detail") or {}).get("step"), int)
    ]
    monotonic = steps == sorted(steps)
    passed = (
        len(called_with_scenario_tool) >= 1
        and len(returned_with_positive_elapsed) >= 1
        and not orphan_returns
        and monotonic
    )
    return PredicateResult(
        name="inner_tool.trajectory",
        passed=passed,
        detail=(
            f"called={len(called)} (scenario_named={len(called_with_scenario_tool)}), "
            f"returned={len(returned)} "
            f"(with_positive_elapsed={len(returned_with_positive_elapsed)}), "
            f"orphan_returns={len(orphan_returns)}, steps_monotonic={monotonic}"
        ),
    )


def _predicate_stall_rail_suppression(snapshot: dict[str, Any]) -> PredicateResult:
    events = snapshot.get("events", [])
    first_terminal_index = next(
        (i for i, e in enumerate(events) if e.get("kind") in _TERMINAL_KINDS),
        None,
    )
    if first_terminal_index is None:
        return PredicateResult(
            name="stall_rail.suppression",
            passed=False,
            detail="no terminal event found in snapshot",
        )
    post_terminal = events[first_terminal_index:]
    narrate_after = [e for e in post_terminal if e.get("kind") == "narrate"]
    stall_text_leaks = [
        e for e in narrate_after if "stalled at" in e.get("summary", "")
    ]
    passed = not stall_text_leaks
    return PredicateResult(
        name="stall_rail.suppression",
        passed=passed,
        detail=(
            f"first_terminal_at_index={first_terminal_index}, "
            f"narrate_events_after_terminal={len(narrate_after)}, "
            f"stall_text_leaks={len(stall_text_leaks)}. "
            f"first_leak={stall_text_leaks[0].get('summary') if stall_text_leaks else 'N/A'}"
        ),
    )


def _predicate_post_run_interpretation(snapshot: dict[str, Any]) -> PredicateResult:
    events = snapshot.get("events", [])
    interprets = [e for e in events if e.get("kind") == "interpret"]
    terminals = [e for e in events if e.get("kind") in _TERMINAL_KINDS]
    if not terminals:
        return PredicateResult(
            name="post_run.interpretation",
            passed=False,
            detail="no terminal event found in snapshot",
        )
    terminal_ts = terminals[0].get("ts", 0.0)
    interprets_after_terminal = [e for e in interprets if e.get("ts", 0.0) >= terminal_ts]
    passed = len(interprets) == 1 and len(interprets_after_terminal) == 1
    return PredicateResult(
        name="post_run.interpretation",
        passed=passed,
        detail=(
            f"interpret_events={len(interprets)}, "
            f"interprets_after_terminal={len(interprets_after_terminal)}. "
            f"summary={interprets[0].get('summary') if interprets else 'N/A'}"
        ),
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def wait_till_terminal(
    *,
    base_url: str,
    component: str,
    case: str,
    timeout_seconds: float = _HARNESS_SAFETY_TIMEOUT_SECONDS,
) -> int:
    """Run the full wait-till-terminal harness.

    Returns the process exit code.
    """
    base = base_url.rstrip("/")
    post_url = f"{base}/playground/components/{component}/runs"
    print(f"POST {post_url}  body=<{{case_name: '{case}'}}>")
    try:
        dispatch = _post_json(post_url, {"case_name": case})
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"HARNESS ERROR: dispatch failed: {exc}", file=sys.stderr)
        return 3

    run_id = dispatch.get("run_id")
    if not run_id:
        print(f"HARNESS ERROR: no run_id in dispatch response: {dispatch}", file=sys.stderr)
        return 3
    print(f"  run_id={run_id}")
    print(f"  state_url={dispatch.get('state_url')}")

    state_url = f"{base}/playground/runs/{run_id}"
    start = time.time()
    last_narrate_ts = start
    narrate_silence_flagged = False
    snapshot: dict[str, Any] = {}

    print(
        f"Polling {state_url} every {_POLL_INTERVAL_SECONDS}s "
        f"for up to {timeout_seconds/60:.0f} min..."
    )
    while True:
        elapsed = time.time() - start
        if elapsed > timeout_seconds:
            print(
                f"HARNESS TIMEOUT: run did not terminate within "
                f"{timeout_seconds/60:.0f} min. This is a c01 convergence "
                f"failure, not a PR #343 regression.",
                file=sys.stderr,
            )
            return 2
        try:
            snapshot = _get_json(state_url)
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            print(f"  poll error (will retry): {exc}", file=sys.stderr)
            time.sleep(_POLL_INTERVAL_SECONDS)
            continue

        events = snapshot.get("events", [])
        narrate_events = [e for e in events if e.get("kind") == "narrate"]
        if narrate_events:
            last_narrate_ts = max(float(e.get("ts", 0.0)) for e in narrate_events)
        wall_now = time.time()
        silence = wall_now - max(last_narrate_ts, start)

        if silence > _NARRATOR_SILENCE_WARN_SECONDS and not narrate_silence_flagged:
            print(
                f"  [WARN] narrator silent for {silence:.1f}s — "
                f"progress-channel contract at risk"
            )
            narrate_silence_flagged = True
        elif silence <= _NARRATOR_SILENCE_WARN_SECONDS and narrate_silence_flagged:
            narrate_silence_flagged = False

        if snapshot.get("closed") is True:
            print(
                f"Run closed after {elapsed:.1f}s with "
                f"status={snapshot.get('terminal', {}).get('status')}"
            )
            break

        progress_note = ""
        if narrate_events:
            latest = narrate_events[-1]
            progress_note = f"  latest narrate: {latest.get('summary', '')[:120]}"
        print(
            f"  elapsed={elapsed:.1f}s events={len(events)} "
            f"narrates={len(narrate_events)}{progress_note}"
        )
        time.sleep(_POLL_INTERVAL_SECONDS)

    wait_duration = time.time() - start

    print("\nPredicate assertions over full event list at terminal:")
    results: list[PredicateResult] = [
        _predicate_narration_cadence(snapshot, wait_duration_seconds=wait_duration),
        _predicate_narration_distinctness(snapshot),
        _predicate_narration_honest_repetition(snapshot),
        _predicate_narration_cites_tool(snapshot),
        _predicate_narration_surfaces_detail(snapshot),
        _predicate_inner_tool_trajectory(snapshot),
        _predicate_stall_rail_suppression(snapshot),
        _predicate_post_run_interpretation(snapshot),
    ]
    for r in results:
        _print_result(r)

    failed = [r for r in results if not r.passed]
    if failed:
        print(f"\n{len(failed)}/{len(results)} predicates FAILED.")
        print("Run snapshot for diagnostic:")
        print(json.dumps(snapshot, indent=2)[:4000])
        return 1

    print(f"\nAll {len(results)} predicates passed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=_DEFAULT_BASE_URL)
    parser.add_argument("--component", default=_DEFAULT_COMPONENT)
    parser.add_argument("--case", default=_DEFAULT_CASE)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=_HARNESS_SAFETY_TIMEOUT_SECONDS,
        help="Harness safety timeout. A run that doesn't terminate is a c01 bug.",
    )
    args = parser.parse_args()
    return wait_till_terminal(
        base_url=args.base_url,
        component=args.component,
        case=args.case,
        timeout_seconds=args.timeout_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
