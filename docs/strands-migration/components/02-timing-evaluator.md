# 02 — timing-evaluator

**Not an agent.** A single deterministic `@tool` function.

---

## Intent

Given the scenes list, OTIO timeline, and `whisperx_alignment`, compute
whether the total narration duration (adjusted for inter-voice / inter-scene
gaps) and per-scene durations are within tolerance of their targets. Set
`timing_passed: bool` and emit a structured `timing_report` for the
orchestrator (component 14) to read.

The orchestrator uses `timing_passed` to decide whether to call
`refine_scenario` + `launch_audio_render` again (see component 05), or to
move on to visual production.

---

## Current implementation

[`server/agents/timing_evaluator.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/agents/timing_evaluator.py)
lines 30–247. Currently exposed as an ADK `Agent` just so it fits into a
`LoopAgent` — but the logic is entirely deterministic. No LLM calls.

The tolerance model is **dual-mode**:

- **Typed `BriefIntent` path (preferred)** — when the blackboard carries a
  typed `BriefIntent` with a non-zero target, tolerance is an **absolute
  ±2 s** (`_TIMING_TOLERANCE_SEC = 2.0`, line 42), and the duration being
  compared is the **movie duration**, i.e. the WhisperX total plus
  computed gap overhead (see below).
- **Legacy path** — when no typed intent is available, tolerance falls
  back to `max(target * _TIMING_TOLERANCE_PCT, _TIMING_TOLERANCE_MIN_SEC)`
  where `_TIMING_TOLERANCE_PCT = 0.15` and `_TIMING_TOLERANCE_MIN_SEC =
  5.0` (lines 47–48). The duration being compared is the raw WhisperX
  total.

Gap overhead (lines 146–155): inter-voice pauses of `1.5 s` between
consecutive voices within a scene, inter-scene pauses of `2.5 s` between
scenes:

```
_INTER_VOICE_PAUSE = 1.5
_INTER_SCENE_PAUSE = 2.5
total_voice_gaps = sum over scenes of max(0, active_voice_count - 1) * 1.5
total_scene_gaps = max(0, len(scenes) - 1) * 2.5
gap_overhead_sec = total_voice_gaps + total_scene_gaps
movie_duration   = actual_duration + gap_overhead_sec
```

**Both paths must be preserved.** Regression tests in the legacy path
(`max(target*0.15, 5)`) still pass; new tests in the typed path require
the ±2 s constant.

---

## Strands implementation

Single module: `server/strands_agents/timing_tool.py`. One function. All
constants and math are ported from `timing_evaluator.py` unchanged:

```python
# server/strands_agents/timing_tool.py
from typing import Any

from strands import tool

# Ported verbatim from server/agents/timing_evaluator.py:42-48
_TIMING_TOLERANCE_SEC = 2.0        # absolute tolerance when BriefIntent available
_TIMING_TOLERANCE_PCT = 0.15       # legacy percentage when it is not
_TIMING_TOLERANCE_MIN_SEC = 5.0    # legacy floor

# Ported verbatim from server/agents/timing_evaluator.py:146-147
_INTER_VOICE_PAUSE = 1.5
_INTER_SCENE_PAUSE = 2.5


def _gap_overhead_sec(scenes: list[dict[str, Any]]) -> float:
    total_voice_gaps = 0.0
    for scene in scenes:
        voices = scene.get("voices") or []
        active = sum(1 for v in voices if (v.get("text") or "").strip())
        total_voice_gaps += max(0, active - 1) * _INTER_VOICE_PAUSE
    total_scene_gaps = max(0, len(scenes) - 1) * _INTER_SCENE_PAUSE
    return total_voice_gaps + total_scene_gaps


@tool(context=True)
async def evaluate_timing(
    context,
    scenes: list[dict[str, Any]],
    whisperx_alignment: dict[str, Any],
    target_duration_sec: float,
    intent_target_sec: float | None = None,
) -> dict[str, Any]:
    """Compare narration duration to target; fail if outside tolerance.

    Args:
        scenes: Scene objects carrying `voices[].text` and `target_duration_sec`.
        whisperx_alignment: WhisperX output with `total_duration_sec` and `per_scene`.
        target_duration_sec: Legacy target (from brief / blackboard).
        intent_target_sec: Typed `BriefIntent.target_duration_sec` if available.
            When set, switches to the ±2s absolute tolerance path.

    Returns:
        `{"timing_passed": bool, "timing_report": {...}}`.
    """
    actual_duration = float(whisperx_alignment["total_duration_sec"])
    gap_overhead = _gap_overhead_sec(scenes)
    movie_duration = actual_duration + gap_overhead

    if intent_target_sec is not None and intent_target_sec > 0:
        # Ported from timing_evaluator.py:160-162
        deviation_sec = movie_duration - intent_target_sec
        tolerance_sec = _TIMING_TOLERANCE_SEC
        target_for_report = intent_target_sec
        compared_duration = movie_duration
    else:
        # Ported from timing_evaluator.py:163-168
        deviation_sec = actual_duration - target_duration_sec
        tolerance_sec = max(
            target_duration_sec * _TIMING_TOLERANCE_PCT,
            _TIMING_TOLERANCE_MIN_SEC,
        )
        target_for_report = target_duration_sec
        compared_duration = actual_duration

    per_scene: list[dict[str, Any]] = []
    for scene, seg in zip(scenes, whisperx_alignment["per_scene"], strict=False):
        target = float(scene["target_duration_sec"])
        actual = float(seg["duration_sec"])
        scene_tolerance_sec = max(target * _TIMING_TOLERANCE_PCT, _TIMING_TOLERANCE_MIN_SEC)
        scene_dev_sec = actual - target
        per_scene.append({
            "scene_id": scene["id"],
            "target_sec": target,
            "actual_sec": actual,
            "deviation_sec": scene_dev_sec,
            "tolerance_sec": scene_tolerance_sec,
            "ok": abs(scene_dev_sec) <= scene_tolerance_sec,
        })

    violations: list[str] = []
    if abs(deviation_sec) > tolerance_sec:
        violations.append(
            "total_duration=<%.2f>, target=<%.2f>, deviation_sec=<%+.2f>, tolerance_sec=<%.2f>"
            % (compared_duration, target_for_report, deviation_sec, tolerance_sec)
        )
    violations.extend(
        f"scene {s['scene_id']} off by {s['deviation_sec']:+.2f}s (tol {s['tolerance_sec']:.2f}s)"
        for s in per_scene if not s["ok"]
    )

    report = {
        "mode": "intent" if intent_target_sec else "legacy",
        "target_duration_sec": target_for_report,
        "actual_duration_sec": actual_duration,
        "gap_overhead_sec": gap_overhead,
        "movie_duration_sec": movie_duration,
        "deviation_sec": deviation_sec,
        "tolerance_sec": tolerance_sec,
        "per_scene_analysis": per_scene,
        "violations": violations,
    }
    timing_passed = not violations

    context.invocation_state["timing_passed"] = timing_passed
    context.invocation_state["timing_report"] = report
    return {"timing_passed": timing_passed, "timing_report": report}
```

All constants and math are ported verbatim from
`server/agents/timing_evaluator.py`. Any change to tolerance values is a
separate RFC — do not silently relax them.

### Why a tool, not an agent

Logic is deterministic and side-effect-free. Wrapping it in an `Agent`
would:

- Cost an LLM call per cycle of the timing loop.
- Introduce non-determinism into a correctness check.
- Require a prompt for something that a 60-line function does.

The orchestrator (component 14) calls `evaluate_timing` directly after
`await_tasks` has completed the audio tasks launched for this scene set.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    ContractComplianceEvaluator(TIMING_CONTRACT),
    Equals("timing_passed", expected),   # deterministic per-case
]
```

### Test cases (minimum 7, mirroring the dual-mode behaviour)

All durations in seconds. `actual` = WhisperX total; `target` is the
relevant mode target; `gap` is computed from scenes.

| Case name | Mode | actual | target | gap | movie | Expected `timing_passed` |
|-----------|------|--------|--------|-----|-------|--------------------------|
| `intent_exact` | intent | 296.0 | 300.0 | 4.0 | 300.0 | True |
| `intent_within_2s_over` | intent | 297.5 | 300.0 | 4.0 | 301.5 | True |
| `intent_over_by_3s` | intent | 299.0 | 300.0 | 4.0 | 303.0 | False |
| `legacy_exact` | legacy | 300.0 | 300.0 | — | — | True |
| `legacy_within_15pct_under` | legacy | 260.0 | 300.0 | — | — | True |
| `legacy_over_by_18pct` | legacy | 354.0 | 300.0 | — | — | False |
| `per_scene_spike` | intent | total ok | any | ok | ok | False |

The `per_scene_spike` case: overall movie duration within ±2 s but one
scene deviates by more than `max(scene.target * 0.15, 5 s)`. Expected
output populates `violations` and sets `timing_passed=False`.

### Simulators

None. Pure function over structured inputs.

### Thresholds

- `ContractComplianceEvaluator` ≥ 1.00 (hard)
- `Equals("timing_passed", expected)` = 1.00 per case (hard)
- Same-input determinism verified (run twice, identical `timing_report`).

---

## File layout

```
server/strands_agents/
├── timing_tool.py                   # ≤ 120 LOC
└── evals/
    ├── evaluators/
    │   └── contract_compliance_evaluator.py (shared, lands with 01)
    └── experiments/
        └── timing_experiment.json
```

---

## Acceptance criteria

- [ ] `evaluate_timing` unit-tested with the 7 cases above.
- [ ] Dual-mode behaviour preserved: `intent_target_sec=None` uses the
      legacy `max(target*0.15, 5s)` tolerance on `actual_duration`;
      `intent_target_sec>0` uses the `±2s` absolute tolerance on
      `movie_duration`.
- [ ] Gap overhead computed exactly as in
      `server/agents/timing_evaluator.py:146-154`.
- [ ] Experiment loads + runs via `Experiment.from_file`.
- [ ] Same-input determinism verified (run twice, identical `timing_report`).
- [ ] No LLM call in traces.
