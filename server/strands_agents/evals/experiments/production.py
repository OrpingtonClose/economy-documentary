"""Production-supervisor experiment factory for component 10.

The production SubAgent dispatches GPU video renders, runs per-artifact
QA, and drives retry / fix / skip / escalate recovery. This experiment
pins the expected trajectory for six canonical cases covering the
decision tree in
``docs/strands-migration/components/10-production-supervisor.md``:

1. ``one_shot_success`` — 3 scenes, single batch, all pass QA on first
   render; zero retries/fixes/escalations.
2. ``transient_worker_error`` — 3 scenes; scene 2 fails with a
   transient worker_500 on revision 1 and passes on a retry; retry
   budget spent down by 1.
3. ``prompt_issue`` — 3 scenes; scene 1 fails with
   ``frame_count_mismatch`` on revision 1 and passes after
   ``fix_scene`` regenerates the concept; fix budget spent down by 1.
4. ``persistent_failure`` — 3 scenes; scene 3 burns through its full
   retry + fix budget (all still fail) then gets skipped; remaining
   scenes render cleanly.
5. ``worker_starved`` — 4 scenes with only 2 available workers →
   rolling batches of 2 (two ``await_tasks`` calls); everything passes
   QA first try.
6. ``budget_exhausted`` — 2 scenes; scene 1's retry + fix budgets are
   both exhausted and the failure is systemic (``worker_pool_degraded``)
   so the SubAgent escalates; scene 2 renders cleanly.

Evaluator stack mirrors ``eval-framework/THRESHOLDS.md``:

* :class:`ProductionSupervisorTrajectoryEvaluator` (hard gate ≥0.90)
  — validates bootstrap, dispatch coverage, per-scene retry + fix
  budgets, rolling batch count (``await_tasks``), per-scene terminal
  state, and escalation appropriateness.
* :class:`ContractComplianceEvaluator` (hard gate 1.00) against
  :data:`PRODUCTION_CONTRACT` — skipped for the ``budget_exhausted``
  case since escalation forwards the failure instead of writing the
  full artifact set.

``ParallelLaunchEvaluator`` is intentionally **not** in the stack.
Retry + fix legs are single-scene batches (size 1) while the main
dispatch is size N, so the per-batch evaluator's ``expected_count``
invariant cannot hold across the full trajectory. The trajectory
evaluator's ``production.rolling_batches`` output is the equivalent
check for this component.
"""

from __future__ import annotations

from typing import Any

from strands_evals.case import Case
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.experiment import Experiment

from contracts import PRODUCTION_CONTRACT
from strands_agents.evals.evaluators import (
    ContractComplianceEvaluator,
)
from strands_agents.evals.evaluators.production_supervisor_trajectory import (
    ProductionSupervisorTrajectoryEvaluator,
)
from strands_agents.subagents.production import (
    PRODUCTION_FIX_BUDGET,
    PRODUCTION_RETRY_BUDGET,
)

#: Minimum per-evaluator score / hard-gate flags. Mirrors the
#: production thresholds in ``eval-framework/THRESHOLDS.md``.
PRODUCTION_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "ProductionSupervisorTrajectoryEvaluator": (0.90, True),
    "ContractComplianceEvaluator": (1.00, True),
}


# ---------------------------------------------------------------------------
# Trajectory helpers — synthetic tool-call records shared across cases.
# ---------------------------------------------------------------------------


def _check_worker_health_call(turn: int) -> dict[str, Any]:
    return {"name": "check_worker_health", "at_turn": turn, "args": {}}


def _launch_call(
    scene_id: str,
    turn: int,
    *,
    revision: int = 1,
    concept_id: str | None = None,
    duration_sec: float = 12.0,
) -> dict[str, Any]:
    return {
        "name": "launch_visual_production",
        "at_turn": turn,
        "args": {
            "scene_id": scene_id,
            "revision": revision,
            "concept_id": concept_id or f"{scene_id}_c0",
            "prompt": f"ltx prompt for {scene_id}",
            "style_lock": {"dominant_style": "cinematic_documentary"},
            "duration_sec": duration_sec,
            "seed": 42,
            "audio_artifact_url": (
                f"b2://documentary/audio/{scene_id}.wav"
            ),
        },
    }


def _await_tasks_call(task_ids: list[str], turn: int) -> dict[str, Any]:
    return {
        "name": "await_tasks",
        "at_turn": turn,
        "args": {"task_ids": task_ids},
    }


def _qa_call(
    scene_id: str,
    turn: int,
    *,
    verdict: str = "pass",
) -> dict[str, Any]:
    return {
        "name": "evaluate_visual_artifact_quality",
        "at_turn": turn,
        "args": {
            "artifact": {
                "scene_id": scene_id,
                "verdict": verdict,
            },
            "target_duration_sec": 12.0,
        },
    }


def _retry_scene_call(scene_id: str, turn: int, *, reason: str) -> dict[str, Any]:
    return {
        "name": "retry_scene",
        "at_turn": turn,
        "args": {"scene_id": scene_id, "reason": reason},
    }


def _fix_scene_call(scene_id: str, turn: int, *, reason: str) -> dict[str, Any]:
    return {
        "name": "fix_scene",
        "at_turn": turn,
        "args": {"scene_id": scene_id, "reason": reason},
    }


def _skip_scene_call(scene_id: str, turn: int, *, reason: str) -> dict[str, Any]:
    return {
        "name": "skip_scene",
        "at_turn": turn,
        "args": {"scene_id": scene_id, "reason": reason},
    }


def _request_escalation_call(
    scene_id: str,
    turn: int,
    *,
    reason: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": "request_escalation",
        "at_turn": turn,
        "args": {
            "scene_id": scene_id,
            "reason": reason,
            "evidence": evidence or {},
        },
    }


# ---------------------------------------------------------------------------
# Contract-state helpers (inputs / produced artifacts for each case).
# ---------------------------------------------------------------------------


def _scenes(n: int) -> list[dict[str, Any]]:
    return [{"id": f"s{i + 1}", "target_duration_sec": 12.0} for i in range(n)]


def _whisperx_alignment(n: int) -> dict[str, Any]:
    return {
        "total_duration_sec": 12.0 * n,
        "per_clip": {
            f"s{i + 1}": {"start": i * 12.0, "end": (i + 1) * 12.0}
            for i in range(n)
        },
    }


def _visual_concepts(n: int) -> list[dict[str, Any]]:
    return [
        {
            "scene_num": i + 1,
            "scene_id": f"s{i + 1}",
            "concept_id": f"s{i + 1}_c0",
            "prompt": f"ltx prompt for s{i + 1}",
        }
        for i in range(n)
    ]


def _state_with_concepts(n: int) -> dict[str, Any]:
    return {
        "scenes": _scenes(n),
        "whisperx_alignment": _whisperx_alignment(n),
        "visual_concepts": _visual_concepts(n),
    }


def _video_artifacts(n: int, *, skipped: list[str] | None = None) -> list[str]:
    skipped_set = set(skipped or [])
    return [
        f"video/s{i + 1}.mp4"
        for i in range(n)
        if f"s{i + 1}" not in skipped_set
    ]


# ---------------------------------------------------------------------------
# Cases — six canonical production-supervisor trajectories.
# ---------------------------------------------------------------------------


def _one_shot_success() -> Case:
    n = 3
    scenes = [f"s{i + 1}" for i in range(n)]
    turn = 1
    trajectory: list[dict[str, Any]] = [_check_worker_health_call(turn)]
    turn += 1
    trajectory.extend(
        _launch_call(scene_id, turn) for scene_id in scenes
    )
    turn += 1
    trajectory.append(
        _await_tasks_call([f"task-{s}-rev1" for s in scenes], turn)
    )
    turn += 1
    trajectory.extend(_qa_call(scene_id, turn) for scene_id in scenes)
    return Case(
        name="one_shot_success",
        input=_state_with_concepts(n),
        expected_output={
            "rendered": n,
            "skipped": 0,
            "escalation_requested": False,
        },
        expected_trajectory=trajectory,
        metadata={
            "case_name": "one_shot_success",
            "final_state": {
                **_state_with_concepts(n),
                "__artifacts__": _video_artifacts(n),
            },
            "scenes": scenes,
            "expected_terminal_per_scene": {s: "rendered" for s in scenes},
            "expected_retry_count_per_scene": {s: 0 for s in scenes},
            "expected_fix_count_per_scene": {s: 0 for s in scenes},
            "expected_batches": 1,
            "expects_escalation": False,
            "tool_name": "launch_visual_production",
            "expected_count": n,
            "completion_tool": "await_tasks",
            "contract_name": "production",
        },
    )


def _transient_worker_error() -> Case:
    n = 3
    scenes = [f"s{i + 1}" for i in range(n)]
    turn = 1
    trajectory: list[dict[str, Any]] = [_check_worker_health_call(turn)]
    turn += 1
    # Batch 1 — all three launches in parallel.
    trajectory.extend(_launch_call(scene_id, turn) for scene_id in scenes)
    turn += 1
    trajectory.append(
        _await_tasks_call([f"task-{s}-rev1" for s in scenes], turn)
    )
    turn += 1
    trajectory.extend(
        _qa_call(s, turn, verdict="pass" if s != "s2" else "fail")
        for s in scenes
    )
    turn += 1
    # Scene 2 — transient retry leg.
    trajectory.append(
        _retry_scene_call("s2", turn, reason="worker_500")
    )
    turn += 1
    trajectory.append(_launch_call("s2", turn, revision=2))
    turn += 1
    trajectory.append(_await_tasks_call(["task-s2-rev2"], turn))
    turn += 1
    trajectory.append(_qa_call("s2", turn, verdict="pass"))
    return Case(
        name="transient_worker_error",
        input=_state_with_concepts(n),
        expected_output={
            "rendered": n,
            "skipped": 0,
            "escalation_requested": False,
        },
        expected_trajectory=trajectory,
        metadata={
            "case_name": "transient_worker_error",
            "final_state": {
                **_state_with_concepts(n),
                "__artifacts__": _video_artifacts(n),
            },
            "scenes": scenes,
            "expected_terminal_per_scene": {s: "rendered" for s in scenes},
            "expected_retry_count_per_scene": {"s1": 0, "s2": 1, "s3": 0},
            "expected_fix_count_per_scene": {s: 0 for s in scenes},
            "expected_batches": 2,
            "expects_escalation": False,
            "tool_name": "launch_visual_production",
            # Per-batch parallelism: the first batch launches all 3
            # scenes; the retry batch is a single-scene batch, so the
            # evaluator's per-batch check is skipped by excluding the
            # retry launch from the parallel-launch assertion. That
            # follow-up is covered by the trajectory evaluator instead.
            "expected_count": n,
            "completion_tool": "await_tasks",
            "contract_name": "production",
        },
    )


def _prompt_issue() -> Case:
    n = 3
    scenes = [f"s{i + 1}" for i in range(n)]
    turn = 1
    trajectory: list[dict[str, Any]] = [_check_worker_health_call(turn)]
    turn += 1
    trajectory.extend(_launch_call(scene_id, turn) for scene_id in scenes)
    turn += 1
    trajectory.append(
        _await_tasks_call([f"task-{s}-rev1" for s in scenes], turn)
    )
    turn += 1
    trajectory.extend(
        _qa_call(s, turn, verdict="pass" if s != "s1" else "fail")
        for s in scenes
    )
    turn += 1
    # Fix leg: prompt-level regen, not a retry.
    trajectory.append(
        _fix_scene_call("s1", turn, reason="frame_count_mismatch")
    )
    turn += 1
    trajectory.append(_launch_call("s1", turn, revision=2))
    turn += 1
    trajectory.append(_await_tasks_call(["task-s1-rev2"], turn))
    turn += 1
    trajectory.append(_qa_call("s1", turn, verdict="pass"))
    return Case(
        name="prompt_issue",
        input=_state_with_concepts(n),
        expected_output={
            "rendered": n,
            "skipped": 0,
            "escalation_requested": False,
        },
        expected_trajectory=trajectory,
        metadata={
            "case_name": "prompt_issue",
            "final_state": {
                **_state_with_concepts(n),
                "__artifacts__": _video_artifacts(n),
            },
            "scenes": scenes,
            "expected_terminal_per_scene": {s: "rendered" for s in scenes},
            "expected_retry_count_per_scene": {s: 0 for s in scenes},
            "expected_fix_count_per_scene": {"s1": 1, "s2": 0, "s3": 0},
            "expected_batches": 2,
            "expects_escalation": False,
            "tool_name": "launch_visual_production",
            "expected_count": n,
            "completion_tool": "await_tasks",
            "contract_name": "production",
        },
    )


def _persistent_failure() -> Case:
    n = 3
    scenes = [f"s{i + 1}" for i in range(n)]
    turn = 1
    trajectory: list[dict[str, Any]] = [_check_worker_health_call(turn)]
    turn += 1
    trajectory.extend(_launch_call(scene_id, turn) for scene_id in scenes)
    turn += 1
    trajectory.append(
        _await_tasks_call([f"task-{s}-rev1" for s in scenes], turn)
    )
    turn += 1
    trajectory.extend(
        _qa_call(s, turn, verdict="pass" if s != "s3" else "fail")
        for s in scenes
    )
    turn += 1
    # Scene 3 burns through full retry budget (2 retries).
    for retry_idx in range(PRODUCTION_RETRY_BUDGET):
        trajectory.append(
            _retry_scene_call("s3", turn, reason="worker_500")
        )
        turn += 1
        trajectory.append(
            _launch_call("s3", turn, revision=retry_idx + 2)
        )
        turn += 1
        trajectory.append(
            _await_tasks_call([f"task-s3-rev{retry_idx + 2}"], turn)
        )
        turn += 1
        trajectory.append(_qa_call("s3", turn, verdict="fail"))
        turn += 1
    # Then full fix budget (1 fix) — still fails.
    for fix_idx in range(PRODUCTION_FIX_BUDGET):
        trajectory.append(
            _fix_scene_call("s3", turn, reason="style_drift")
        )
        turn += 1
        fix_revision = PRODUCTION_RETRY_BUDGET + fix_idx + 2
        trajectory.append(
            _launch_call("s3", turn, revision=fix_revision)
        )
        turn += 1
        trajectory.append(
            _await_tasks_call([f"task-s3-rev{fix_revision}"], turn)
        )
        turn += 1
        trajectory.append(_qa_call("s3", turn, verdict="fail"))
        turn += 1
    # Skip — the failure is localised, so the documentary still ships.
    trajectory.append(
        _skip_scene_call("s3", turn, reason="retry_and_fix_exhausted")
    )
    return Case(
        name="persistent_failure",
        input=_state_with_concepts(n),
        expected_output={
            "rendered": 2,
            "skipped": 1,
            "escalation_requested": False,
        },
        expected_trajectory=trajectory,
        metadata={
            "case_name": "persistent_failure",
            "final_state": {
                **_state_with_concepts(n),
                "__artifacts__": _video_artifacts(n, skipped=["s3"]),
            },
            "scenes": scenes,
            "expected_terminal_per_scene": {
                "s1": "rendered",
                "s2": "rendered",
                "s3": "skipped",
            },
            "expected_retry_count_per_scene": {
                "s1": 0,
                "s2": 0,
                "s3": PRODUCTION_RETRY_BUDGET,
            },
            "expected_fix_count_per_scene": {
                "s1": 0,
                "s2": 0,
                "s3": PRODUCTION_FIX_BUDGET,
            },
            # 1 mass dispatch + 1 retry await per retry leg + 1 per fix.
            "expected_batches": 1 + PRODUCTION_RETRY_BUDGET + PRODUCTION_FIX_BUDGET,
            "expects_escalation": False,
            "tool_name": "launch_visual_production",
            "expected_count": n,
            "completion_tool": "await_tasks",
            "contract_name": "production",
        },
    )


def _worker_starved() -> Case:
    n = 4
    scenes = [f"s{i + 1}" for i in range(n)]
    turn = 1
    trajectory: list[dict[str, Any]] = [_check_worker_health_call(turn)]
    turn += 1
    # Batch 1 — scenes 1 and 2 only.
    trajectory.append(_launch_call("s1", turn))
    trajectory.append(_launch_call("s2", turn))
    turn += 1
    trajectory.append(_await_tasks_call(["task-s1-rev1", "task-s2-rev1"], turn))
    turn += 1
    trajectory.append(_qa_call("s1", turn, verdict="pass"))
    trajectory.append(_qa_call("s2", turn, verdict="pass"))
    turn += 1
    # Batch 2 — scenes 3 and 4.
    trajectory.append(_launch_call("s3", turn))
    trajectory.append(_launch_call("s4", turn))
    turn += 1
    trajectory.append(_await_tasks_call(["task-s3-rev1", "task-s4-rev1"], turn))
    turn += 1
    trajectory.append(_qa_call("s3", turn, verdict="pass"))
    trajectory.append(_qa_call("s4", turn, verdict="pass"))
    return Case(
        name="worker_starved",
        input=_state_with_concepts(n),
        expected_output={
            "rendered": n,
            "skipped": 0,
            "escalation_requested": False,
        },
        expected_trajectory=trajectory,
        metadata={
            "case_name": "worker_starved",
            "final_state": {
                **_state_with_concepts(n),
                "__artifacts__": _video_artifacts(n),
            },
            "scenes": scenes,
            "expected_terminal_per_scene": {s: "rendered" for s in scenes},
            "expected_retry_count_per_scene": {s: 0 for s in scenes},
            "expected_fix_count_per_scene": {s: 0 for s in scenes},
            "expected_batches": 2,
            "expects_escalation": False,
            "tool_name": "launch_visual_production",
            # ParallelLaunchEvaluator is per-batch: each rolling batch
            # of 2 scenes launches on one turn.
            "expected_count": 2,
            "completion_tool": "await_tasks",
            "contract_name": "production",
        },
    )


def _budget_exhausted() -> Case:
    n = 2
    scenes = [f"s{i + 1}" for i in range(n)]
    turn = 1
    trajectory: list[dict[str, Any]] = [_check_worker_health_call(turn)]
    turn += 1
    trajectory.extend(_launch_call(scene_id, turn) for scene_id in scenes)
    turn += 1
    trajectory.append(
        _await_tasks_call([f"task-{s}-rev1" for s in scenes], turn)
    )
    turn += 1
    trajectory.append(_qa_call("s1", turn, verdict="fail"))
    trajectory.append(_qa_call("s2", turn, verdict="pass"))
    turn += 1
    # Scene 1 burns through retry + fix budgets, then escalates
    # because the failure is systemic (worker_pool_degraded).
    for retry_idx in range(PRODUCTION_RETRY_BUDGET):
        trajectory.append(
            _retry_scene_call("s1", turn, reason="pool_starved")
        )
        turn += 1
        trajectory.append(
            _launch_call("s1", turn, revision=retry_idx + 2)
        )
        turn += 1
        trajectory.append(
            _await_tasks_call([f"task-s1-rev{retry_idx + 2}"], turn)
        )
        turn += 1
        trajectory.append(_qa_call("s1", turn, verdict="fail"))
        turn += 1
    for fix_idx in range(PRODUCTION_FIX_BUDGET):
        trajectory.append(
            _fix_scene_call("s1", turn, reason="style_drift")
        )
        turn += 1
        fix_revision = PRODUCTION_RETRY_BUDGET + fix_idx + 2
        trajectory.append(
            _launch_call("s1", turn, revision=fix_revision)
        )
        turn += 1
        trajectory.append(
            _await_tasks_call([f"task-s1-rev{fix_revision}"], turn)
        )
        turn += 1
        trajectory.append(_qa_call("s1", turn, verdict="fail"))
        turn += 1
    trajectory.append(
        _request_escalation_call(
            "s1",
            turn,
            reason="retry_and_fix_exhausted_systemic",
            evidence={"worker_health": "degraded"},
        )
    )
    return Case(
        name="budget_exhausted",
        input=_state_with_concepts(n),
        expected_output={
            "rendered": 1,
            "skipped": 0,
            "escalation_requested": True,
        },
        expected_trajectory=trajectory,
        metadata={
            "case_name": "budget_exhausted",
            # Artifacts for s1 are still listed — the orchestrator will
            # consume the escalation payload and decide downstream; the
            # production SubAgent's postcondition is "scene has a
            # terminal decision", not "video artifact exists".
            "final_state": {
                **_state_with_concepts(n),
                "__artifacts__": _video_artifacts(n, skipped=["s1"]),
            },
            "scenes": scenes,
            "expected_terminal_per_scene": {
                "s1": "escalated",
                "s2": "rendered",
            },
            "expected_retry_count_per_scene": {
                "s1": PRODUCTION_RETRY_BUDGET,
                "s2": 0,
            },
            "expected_fix_count_per_scene": {
                "s1": PRODUCTION_FIX_BUDGET,
                "s2": 0,
            },
            "expected_batches": 1 + PRODUCTION_RETRY_BUDGET + PRODUCTION_FIX_BUDGET,
            "expects_escalation": True,
            "tool_name": "launch_visual_production",
            "expected_count": n,
            "completion_tool": "await_tasks",
            # Escalation case intentionally does not write the full
            # production artifact set; suppress the contract evaluator
            # by omitting ``contract_name``.
        },
    )


# ---------------------------------------------------------------------------
# Public factories
# ---------------------------------------------------------------------------


def production_cases() -> list[Case]:
    """Return the six canonical production-supervisor cases."""
    return [
        _one_shot_success(),
        _transient_worker_error(),
        _prompt_issue(),
        _persistent_failure(),
        _worker_starved(),
        _budget_exhausted(),
    ]


def production_evaluators() -> list[Evaluator]:
    """Return the production evaluator stack in spec order."""
    return [
        ProductionSupervisorTrajectoryEvaluator(),
        ContractComplianceEvaluator(PRODUCTION_CONTRACT),
    ]


def build_production_experiment() -> Experiment:
    """Build the production-supervisor experiment."""
    return Experiment(
        cases=production_cases(),
        evaluators=production_evaluators(),
    )


def production_task(case: Case) -> dict[str, Any]:
    """Replay task adapter for the component-playground surface.

    Returns the case's canonical envelope so the evaluate endpoint can
    score a known-good payload against this component's evaluator
    stack without a live agent run. A live runner can replace this
    once provider plumbing lands in the playground.
    """
    metadata = case.metadata or {}
    return {
        "output": case.expected_output or {},
        "trajectory": list(
            case.expected_trajectory
            or metadata.get("canonical_trajectory")
            or []
        ),
        "metadata": {"mode": "replay", "case": case.name},
    }


__all__ = [
    "PRODUCTION_EVALUATOR_THRESHOLDS",
    "build_production_experiment",
    "production_cases",
    "production_evaluators",
]
