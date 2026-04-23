"""Slice 1 — AG-UI wire format test harness.

Dispatches scenarios against staging, waits till terminal (no time cutoff from the
test side), then asserts the AG-UI wire contract over the full event list.

Exit codes:
  0  — every predicate passed
  1  — at least one predicate failed
  2  — a run didn't close within SAFETY_TIMEOUT_S (harness bailed)
  3  — HTTP / JSON / unreachable backend
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Mirror of server/strands_agents/playground/agui.py _KIND_TO_AGUI, kept in-test
# so a local/remote mismatch is visible instead of silently passing.
KIND_TO_AGUI: dict[str, tuple[str, dict[str, Any]]] = {
    "run.dispatched":  ("RUN_STARTED",         {}),
    "probe.start":     ("STEP_STARTED",        {"step_name": "probe"}),
    "probe.done":      ("STEP_FINISHED",       {"step_name": "probe"}),
    "task.pick_model": ("CUSTOM",              {"name": "task.pick_model"}),
    "task.start":      ("STEP_STARTED",        {"step_name": "task"}),
    "tool.called":     ("TOOL_CALL_START",     {}),
    "tool.returned":   ("TOOL_CALL_END",       {}),
    "task.done":       ("STEP_FINISHED",       {"step_name": "task"}),
    "evaluate.start":  ("STEP_STARTED",        {"step_name": "evaluate"}),
    "evaluate.scored": ("STEP_FINISHED",       {"step_name": "evaluate"}),
    "narrate":         ("TEXT_MESSAGE_CONTENT", {"source": "narrator"}),
    "interpret":       ("TEXT_MESSAGE_CONTENT", {"source": "interpreter"}),
    "run.ok":          ("RUN_FINISHED",        {}),
    "run.error":       ("RUN_ERROR",           {}),
    "run.cancelled":   ("RUN_FINISHED",        {"cancelled": True}),
}
AGUI_TYPES = {v[0] for v in KIND_TO_AGUI.values()}
LEGACY_KEYS = {"seq", "ts", "kind", "summary", "detail"}
AGUI_EXTRA_KEYS = {"type", "step_name", "source", "name", "cancelled"}
ALLOWED_KEYS = LEGACY_KEYS | AGUI_EXTRA_KEYS

SAFETY_TIMEOUT_S = 15 * 60
POLL_INTERVAL_S = 1.5


def _http(method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def dispatch_run(base_url: str, component: str, case_name: str) -> str:
    r = _http(
        "POST",
        f"{base_url}/playground/components/{component}/runs",
        {"case_name": case_name},
    )
    return r["run_id"]


def poll_until_terminal(base_url: str, run_id: str) -> dict[str, Any]:
    start = time.time()
    last_seq = 0
    while True:
        snap = _http("GET", f"{base_url}/playground/runs/{run_id}")
        closed = bool(snap.get("closed"))
        events = snap.get("events", [])
        for ev in events[last_seq:]:
            print(
                f"  [{run_id[-8:]}] seq={ev.get('seq'):>3} "
                f"kind={ev.get('kind'):30s} type={ev.get('type')}",
                flush=True,
            )
        last_seq = len(events)
        if closed:
            return snap
        if time.time() - start > SAFETY_TIMEOUT_S:
            print(f"  [{run_id[-8:]}] HARNESS SAFETY TIMEOUT after {SAFETY_TIMEOUT_S}s", flush=True)
            sys.exit(2)
        time.sleep(POLL_INTERVAL_S)


def assert_envelope(ev: dict[str, Any], predicate_log: list[str]) -> list[str]:
    """Return list of failures for this single envelope."""
    fails: list[str] = []
    kind = ev.get("kind")
    typ = ev.get("type")

    if "type" not in ev:
        fails.append(f"envelope seq={ev.get('seq')} kind={kind} missing `type` key")
        return fails

    if typ not in AGUI_TYPES:
        fails.append(
            f"envelope seq={ev.get('seq')} kind={kind} has unknown type={typ!r}"
        )

    for legacy_key in LEGACY_KEYS:
        if legacy_key not in ev:
            fails.append(
                f"envelope seq={ev.get('seq')} kind={kind} missing legacy `{legacy_key}`"
            )

    unknown_keys = set(ev.keys()) - ALLOWED_KEYS
    if unknown_keys:
        fails.append(
            f"envelope seq={ev.get('seq')} kind={kind} has unknown top-level keys {unknown_keys}"
        )

    if kind in KIND_TO_AGUI:
        expected_type, expected_extras = KIND_TO_AGUI[kind]
        if typ != expected_type:
            fails.append(
                f"envelope seq={ev.get('seq')} kind={kind}: expected type={expected_type}, got type={typ}"
            )
        for ek, ev_val in expected_extras.items():
            if ev.get(ek) != ev_val:
                fails.append(
                    f"envelope seq={ev.get('seq')} kind={kind}: expected {ek}={ev_val!r}, got {ek}={ev.get(ek)!r}"
                )
        # Extras that shouldn't be there.
        for ek in AGUI_EXTRA_KEYS - {"type"}:
            if ek in ev and ek not in expected_extras:
                fails.append(
                    f"envelope seq={ev.get('seq')} kind={kind}: "
                    f"extra key {ek}={ev[ek]!r} leaked but mapping says none"
                )
    return fails


def run_scenario(
    base_url: str,
    component: str,
    case_name: str,
    out_dir: Path,
    expected_terminal_kinds: set[str],
    required_kinds: set[str] | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    label = label or f"{component}:{case_name}"
    print(f"\n=== {label} ===", flush=True)
    run_id = dispatch_run(base_url, component, case_name)
    print(f"  run_id={run_id}", flush=True)
    snap = poll_until_terminal(base_url, run_id)
    out_file = out_dir / f"{label.replace(':', '_').replace('/', '_')}.json"
    out_file.write_text(json.dumps(snap, indent=2))

    events = snap.get("events", [])
    fails: list[str] = []
    kinds_seen = {e.get("kind") for e in events}
    types_seen = {e.get("type") for e in events}

    terminal_kinds_seen = kinds_seen & {"run.ok", "run.error", "run.cancelled"}
    if not terminal_kinds_seen:
        fails.append(f"{label}: NO terminal event in stream")
    elif not (terminal_kinds_seen <= expected_terminal_kinds):
        fails.append(
            f"{label}: unexpected terminal kinds: expected {expected_terminal_kinds}, got {terminal_kinds_seen}"
        )

    for ev in events:
        fails.extend(assert_envelope(ev, []))

    if required_kinds:
        missing = required_kinds - kinds_seen
        if missing:
            fails.append(f"{label}: required kinds not seen: {missing}")

    print(f"  terminal events: {terminal_kinds_seen}")
    print(f"  kinds seen: {sorted(kinds_seen)}")
    print(f"  types seen: {sorted(types_seen)}")
    print(f"  events: {len(events)}")
    if fails:
        print("  FAILS:")
        for f in fails:
            print(f"    - {f}")
    else:
        print("  PASS ✓")
    return {"label": label, "snapshot": snap, "fails": fails, "kinds": sorted(kinds_seen), "types": sorted(types_seen)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://142.171.48.138:29561")
    ap.add_argument("--out-dir", default="docs/strands-migration/deploy/slice-1-agui-wire-format-test-results")
    ap.add_argument("--scenarios", default="c04,c03,c01")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []

    if "c04" in args.scenarios:
        results.append(run_scenario(
            args.base_url, "c04", "basic_3_scenes", out_dir,
            expected_terminal_kinds={"run.ok"},
            required_kinds={"run.dispatched", "task.start", "task.done", "run.ok"},
            label="curl1_c04_basic_3_scenes",
        ))

    if "c03" in args.scenarios:
        results.append(run_scenario(
            args.base_url, "c03", "timing_passed_noop", out_dir,
            expected_terminal_kinds={"run.ok"},
            required_kinds={"run.dispatched", "probe.start", "probe.done", "task.start", "task.done", "run.ok"},
            label="curl2_c03_timing_passed_noop",
        ))

    if "c01" in args.scenarios:
        results.append(run_scenario(
            args.base_url, "c01", "economics_basics", out_dir,
            expected_terminal_kinds={"run.ok", "run.error"},
            required_kinds={"run.dispatched", "probe.start", "probe.done", "task.start", "tool.called", "tool.returned", "narrate"},
            label="curl3_c01_economics_basics",
        ))

    if "c01_error" in args.scenarios:
        results.append(run_scenario(
            args.base_url, "c01", "economics_basics", out_dir,
            expected_terminal_kinds={"run.error"},
            required_kinds={"run.dispatched", "probe.start", "probe.done", "run.error"},
            label="curl4_c01_error_scrubbed_creds",
        ))

    summary = out_dir / "summary.json"
    summary.write_text(json.dumps([
        {"label": r["label"], "fails": r["fails"], "kinds": r["kinds"], "types": r["types"]}
        for r in results
    ], indent=2))

    all_fails = sum((r["fails"] for r in results), start=[])
    print(f"\n=== SUMMARY: {len(results)} scenarios, {len(all_fails)} fails ===")
    for f in all_fails:
        print(f"  FAIL: {f}")
    sys.exit(1 if all_fails else 0)


if __name__ == "__main__":
    main()
