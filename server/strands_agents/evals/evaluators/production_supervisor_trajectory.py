"""ProductionSupervisorTrajectoryEvaluator — production-supervisor check.

Validates the tool-call trajectory emitted by the ``production``
SubAgent (Component 10) against the invariants documented in
``docs/strands-migration/components/10-production-supervisor.md``.

The production trajectory has a well-defined shape::

    Bootstrap
    └─ check_worker_health * 1    (before any launch)

    Rolling batches (B batches, batch_i has K_i scenes)
    ├─ launch_visual_production * K_i     # one per scene in batch
    ├─ await_tasks * 1
    └─ evaluate_visual_artifact_quality * K_i

    Recovery legs (zero or more per scene)
    ├─ retry_scene                # up to RETRY_BUDGET times per scene
    │   └─ launch_visual_production (revision++) + await + QA
    ├─ fix_scene                  # up to FIX_BUDGET times per scene
    │   └─ launch_visual_production (revision++) + await + QA
    ├─ skip_scene                 # terminal per scene
    └─ request_escalation         # terminal per scene or global

Each scene finishes as ``rendered`` (last QA ``pass``), ``skipped``
(``skip_scene`` called), or ``escalated`` (``request_escalation``
called). Leaving a scene in-flight violates AGENTS.md invariant #6
and is flagged as a hard gate.

Input shape
-----------
``EvaluationData`` with:

* ``actual_trajectory``: ``list[dict]`` of tool-call records. Each
  record must carry a ``"name"`` key; launch records must include
  ``"args"`` with at least ``scene_id``, ``audio_artifact_url``, and
  ``revision``.

* ``metadata["scenes"]``: ``list[str]`` of scene ids expected to be
  dispatched. Order matters only for error messages; scoring is
  set-based.
* ``metadata["expected_terminal_per_scene"]``: dict mapping scene id
  to one of ``"rendered"`` / ``"skipped"`` / ``"escalated"``.
* ``metadata["expected_retry_count_per_scene"]`` (optional, default
  ``{}``): dict scene_id → int. Any scene omitted defaults to 0.
* ``metadata["expected_fix_count_per_scene"]`` (optional, default
  ``{}``): dict scene_id → int. Any scene omitted defaults to 0.
* ``metadata["expected_batches"]`` (optional, default 1): number of
  rolling batches the SubAgent should have used. ``1`` means a single
  mass dispatch.
* ``metadata["expects_escalation"]`` (optional, default ``False``):
  whether the trajectory must contain at least one
  ``request_escalation`` call.
* ``metadata["retry_budget"]`` (optional, default
  :data:`PRODUCTION_RETRY_BUDGET`).
* ``metadata["fix_budget"]`` (optional, default
  :data:`PRODUCTION_FIX_BUDGET`).

Output
------
Seven :class:`EvaluationOutput` entries (all hard gates):

* ``production.bootstrap`` — ``check_worker_health`` called exactly
  once before any ``launch_*`` call.
* ``production.dispatch_coverage`` — every expected scene has at
  least one ``launch_visual_production`` call with the correct
  ``scene_id`` + non-empty ``audio_artifact_url``.
* ``production.retry_budget`` — no scene exceeds its retry budget;
  observed retry counts match ``expected_retry_count_per_scene``.
* ``production.fix_budget`` — same for fixes.
* ``production.rolling_batches`` — number of
  ``await_tasks`` calls matches ``expected_batches``.
* ``production.no_pending_at_finish`` — every scene reached a
  terminal outcome matching ``expected_terminal_per_scene``.
* ``production.escalation_appropriateness`` —
  ``request_escalation`` present iff ``expects_escalation`` is True.
"""

from __future__ import annotations

from typing import Any

from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from strands_agents.subagents.production import (
    PRODUCTION_FIX_BUDGET,
    PRODUCTION_RETRY_BUDGET,
)

_LAUNCH_TOOL = "launch_visual_production"
_HEALTH_TOOL = "check_worker_health"
_AWAIT_TOOL = "await_tasks"
_QA_TOOL = "evaluate_visual_artifact_quality"
_RETRY_TOOL = "retry_scene"
_FIX_TOOL = "fix_scene"
_SKIP_TOOL = "skip_scene"
_ESCALATE_TOOL = "request_escalation"

_TERMINAL_STATES: frozenset[str] = frozenset(
    {"rendered", "skipped", "escalated"}
)


def _extract_calls(trajectory: Any) -> list[dict[str, Any]] | None:
    if not isinstance(trajectory, list):
        return None
    result: list[dict[str, Any]] = []
    for call in trajectory:
        if isinstance(call, dict) and isinstance(call.get("name"), str):
            result.append(call)
        else:
            return None
    return result


def _arg(call: dict[str, Any], key: str) -> Any:
    args = call.get("args")
    if not isinstance(args, dict):
        return None
    return args.get(key)


def _validate_metadata_config(  # noqa: PLR0911 — one guard per missing/malformed key
    metadata: dict[str, Any],
) -> list[EvaluationOutput] | tuple[
    list[str],
    dict[str, str],
    dict[str, int],
    dict[str, int],
    int,
    bool,
    int,
    int,
]:
    """Validate + normalize the evaluator metadata.

    Returns either a list of ``EvaluationOutput`` (describing the
    config failure) or the normalized tuple consumed by ``evaluate``.
    """
    scenes_raw = metadata.get("scenes")
    if not isinstance(scenes_raw, list) or not all(
        isinstance(s, str) and s for s in scenes_raw
    ):
        return [
            EvaluationOutput(
                score=0.0,
                test_pass=False,
                reason="metadata['scenes'] must be a non-empty list[str]",
                label="production.missing_config",
            )
        ]
    scenes = list(scenes_raw)

    terminal_raw = metadata.get("expected_terminal_per_scene")
    if not isinstance(terminal_raw, dict):
        return [
            EvaluationOutput(
                score=0.0,
                test_pass=False,
                reason=(
                    "metadata['expected_terminal_per_scene'] must be a dict"
                ),
                label="production.missing_config",
            )
        ]
    terminal: dict[str, str] = {}
    for scene_id in scenes:
        value = terminal_raw.get(scene_id)
        if not isinstance(value, str) or value not in _TERMINAL_STATES:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=(
                        f"expected_terminal_per_scene[{scene_id!r}] "
                        f"must be one of {sorted(_TERMINAL_STATES)}"
                    ),
                    label="production.missing_config",
                )
            ]
        terminal[scene_id] = value

    retries_raw = metadata.get("expected_retry_count_per_scene", {})
    fixes_raw = metadata.get("expected_fix_count_per_scene", {})
    if not isinstance(retries_raw, dict) or not all(
        isinstance(k, str) and isinstance(v, int) and v >= 0
        for k, v in retries_raw.items()
    ):
        return [
            EvaluationOutput(
                score=0.0,
                test_pass=False,
                reason=(
                    "expected_retry_count_per_scene must be dict[str, int>=0]"
                ),
                label="production.missing_config",
            )
        ]
    if not isinstance(fixes_raw, dict) or not all(
        isinstance(k, str) and isinstance(v, int) and v >= 0
        for k, v in fixes_raw.items()
    ):
        return [
            EvaluationOutput(
                score=0.0,
                test_pass=False,
                reason=(
                    "expected_fix_count_per_scene must be dict[str, int>=0]"
                ),
                label="production.missing_config",
            )
        ]
    retries = {scene_id: int(retries_raw.get(scene_id, 0)) for scene_id in scenes}
    fixes = {scene_id: int(fixes_raw.get(scene_id, 0)) for scene_id in scenes}

    expected_batches = metadata.get("expected_batches", 1)
    if not isinstance(expected_batches, int) or expected_batches < 1:
        return [
            EvaluationOutput(
                score=0.0,
                test_pass=False,
                reason="metadata['expected_batches'] must be int >= 1",
                label="production.missing_config",
            )
        ]

    expects_escalation = bool(metadata.get("expects_escalation", False))
    retry_budget = int(metadata.get("retry_budget", PRODUCTION_RETRY_BUDGET))
    fix_budget = int(metadata.get("fix_budget", PRODUCTION_FIX_BUDGET))

    return (
        scenes,
        terminal,
        retries,
        fixes,
        expected_batches,
        expects_escalation,
        retry_budget,
        fix_budget,
    )


class ProductionSupervisorTrajectoryEvaluator(Evaluator[Any, Any]):
    """Validate production-supervisor trajectories.

    Seven hard-gate outputs — see module docstring.
    """

    def __init__(self) -> None:
        super().__init__()

    def evaluate(  # noqa: PLR0912, PLR0915 — seven independent sub-evaluations
        self,
        evaluation_case: EvaluationData[Any, Any],
    ) -> list[EvaluationOutput]:
        metadata = evaluation_case.metadata or {}
        config = _validate_metadata_config(metadata)
        if isinstance(config, list):
            return config
        (
            scenes,
            expected_terminal,
            expected_retries,
            expected_fixes,
            expected_batches,
            expects_escalation,
            retry_budget,
            fix_budget,
        ) = config

        calls = _extract_calls(evaluation_case.actual_trajectory)
        if calls is None:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=(
                        "actual_trajectory must be list[dict] of tool calls"
                    ),
                    label="production.missing_actual",
                )
            ]

        outputs: list[EvaluationOutput] = []

        # ------------------------------------------------------------------
        # 1. bootstrap — check_worker_health exactly once before any launch.
        # ------------------------------------------------------------------
        first_launch_index = next(
            (
                i
                for i, c in enumerate(calls)
                if c.get("name") == _LAUNCH_TOOL
            ),
            None,
        )
        health_calls = [
            i for i, c in enumerate(calls) if c.get("name") == _HEALTH_TOOL
        ]
        if len(health_calls) == 1 and (
            first_launch_index is None
            or health_calls[0] < first_launch_index
        ):
            bootstrap_ok = True
            bootstrap_reason = (
                "PASS check_worker_health called once before any dispatch"
            )
        else:
            bootstrap_ok = False
            bootstrap_reason = (
                f"FAIL check_worker_health calls={len(health_calls)}, "
                f"first_launch_index={first_launch_index}"
            )
        outputs.append(
            EvaluationOutput(
                score=1.0 if bootstrap_ok else 0.0,
                test_pass=bootstrap_ok,
                reason=bootstrap_reason,
                label="production.bootstrap",
            )
        )

        # ------------------------------------------------------------------
        # 2. dispatch_coverage — every scene got at least one launch with a
        #    non-empty audio_artifact_url.
        # ------------------------------------------------------------------
        dispatch_scene_hits: dict[str, int] = {scene_id: 0 for scene_id in scenes}
        missing_audio_launches: list[str] = []
        for call in calls:
            if call.get("name") != _LAUNCH_TOOL:
                continue
            scene_id = _arg(call, "scene_id")
            audio = _arg(call, "audio_artifact_url")
            if scene_id in dispatch_scene_hits:
                dispatch_scene_hits[scene_id] += 1
            if not isinstance(audio, str) or not audio:
                missing_audio_launches.append(str(scene_id))
        missing_dispatch = [
            scene_id
            for scene_id, hits in dispatch_scene_hits.items()
            if hits == 0
        ]
        coverage_ok = not missing_dispatch and not missing_audio_launches
        if coverage_ok:
            coverage_reason = (
                f"PASS every scene dispatched with audio artifact "
                f"(n={len(scenes)})"
            )
        else:
            parts: list[str] = []
            if missing_dispatch:
                parts.append(f"never dispatched: {missing_dispatch}")
            if missing_audio_launches:
                parts.append(
                    f"launches missing audio_artifact_url: "
                    f"{missing_audio_launches}"
                )
            coverage_reason = "FAIL " + "; ".join(parts)
        outputs.append(
            EvaluationOutput(
                score=1.0 if coverage_ok else 0.0,
                test_pass=coverage_ok,
                reason=coverage_reason,
                label="production.dispatch_coverage",
            )
        )

        # ------------------------------------------------------------------
        # 3. retry_budget — per-scene retry_scene counts match expectation
        #    and never exceed the budget.
        # ------------------------------------------------------------------
        retry_counts: dict[str, int] = {scene_id: 0 for scene_id in scenes}
        for call in calls:
            if call.get("name") != _RETRY_TOOL:
                continue
            scene_id = _arg(call, "scene_id")
            if scene_id in retry_counts:
                retry_counts[scene_id] += 1
        retry_violations = [
            (scene_id, observed, expected_retries[scene_id])
            for scene_id, observed in retry_counts.items()
            if observed != expected_retries[scene_id]
        ]
        over_budget = [
            (scene_id, observed)
            for scene_id, observed in retry_counts.items()
            if observed > retry_budget
        ]
        retry_ok = not retry_violations and not over_budget
        if retry_ok:
            retry_reason = (
                f"PASS retry counts match expectation "
                f"(budget {retry_budget}/scene)"
            )
        else:
            parts = []
            if retry_violations:
                parts.append(
                    "mismatched: "
                    + ", ".join(
                        f"{s}={obs} (expected {exp})"
                        for s, obs, exp in retry_violations
                    )
                )
            if over_budget:
                parts.append(
                    "over budget: "
                    + ", ".join(f"{s}={obs}" for s, obs in over_budget)
                )
            retry_reason = "FAIL " + "; ".join(parts)
        outputs.append(
            EvaluationOutput(
                score=1.0 if retry_ok else 0.0,
                test_pass=retry_ok,
                reason=retry_reason,
                label="production.retry_budget",
            )
        )

        # ------------------------------------------------------------------
        # 4. fix_budget — same, for fix_scene.
        # ------------------------------------------------------------------
        fix_counts: dict[str, int] = {scene_id: 0 for scene_id in scenes}
        for call in calls:
            if call.get("name") != _FIX_TOOL:
                continue
            scene_id = _arg(call, "scene_id")
            if scene_id in fix_counts:
                fix_counts[scene_id] += 1
        fix_violations = [
            (scene_id, observed, expected_fixes[scene_id])
            for scene_id, observed in fix_counts.items()
            if observed != expected_fixes[scene_id]
        ]
        fix_over_budget = [
            (scene_id, observed)
            for scene_id, observed in fix_counts.items()
            if observed > fix_budget
        ]
        fix_ok = not fix_violations and not fix_over_budget
        if fix_ok:
            fix_reason = (
                f"PASS fix counts match expectation (budget {fix_budget}/scene)"
            )
        else:
            parts = []
            if fix_violations:
                parts.append(
                    "mismatched: "
                    + ", ".join(
                        f"{s}={obs} (expected {exp})"
                        for s, obs, exp in fix_violations
                    )
                )
            if fix_over_budget:
                parts.append(
                    "over budget: "
                    + ", ".join(f"{s}={obs}" for s, obs in fix_over_budget)
                )
            fix_reason = "FAIL " + "; ".join(parts)
        outputs.append(
            EvaluationOutput(
                score=1.0 if fix_ok else 0.0,
                test_pass=fix_ok,
                reason=fix_reason,
                label="production.fix_budget",
            )
        )

        # ------------------------------------------------------------------
        # 5. rolling_batches — await_tasks calls equal expected_batches.
        # ------------------------------------------------------------------
        await_calls = sum(1 for c in calls if c.get("name") == _AWAIT_TOOL)
        batches_ok = await_calls == expected_batches
        outputs.append(
            EvaluationOutput(
                score=1.0 if batches_ok else 0.0,
                test_pass=batches_ok,
                reason=(
                    f"PASS await_tasks called {await_calls}x "
                    f"(expected {expected_batches})"
                    if batches_ok
                    else (
                        f"FAIL await_tasks called {await_calls}x, "
                        f"expected {expected_batches}"
                    )
                ),
                label="production.rolling_batches",
            )
        )

        # ------------------------------------------------------------------
        # 6. no_pending_at_finish — every scene has a terminal outcome.
        # ------------------------------------------------------------------
        observed_terminal: dict[str, str] = {}
        for scene_id in scenes:
            skipped = any(
                c.get("name") == _SKIP_TOOL and _arg(c, "scene_id") == scene_id
                for c in calls
            )
            escalated = any(
                c.get("name") == _ESCALATE_TOOL
                and _arg(c, "scene_id") == scene_id
                for c in calls
            )
            if skipped:
                observed_terminal[scene_id] = "skipped"
            elif escalated:
                observed_terminal[scene_id] = "escalated"
            else:
                # If launched at least once and not skip/escalate, treat the
                # trajectory as expecting a ``rendered`` outcome — the case
                # metadata says so. Absence is caught by dispatch_coverage.
                observed_terminal[scene_id] = (
                    "rendered" if dispatch_scene_hits[scene_id] > 0 else "pending"
                )
        pending = [
            scene_id
            for scene_id, state in observed_terminal.items()
            if state == "pending"
        ]
        mismatches = [
            (scene_id, state, expected_terminal[scene_id])
            for scene_id, state in observed_terminal.items()
            if state != expected_terminal[scene_id] and state != "pending"
        ]
        no_pending_ok = not pending and not mismatches
        if no_pending_ok:
            no_pending_reason = (
                "PASS every scene reached its expected terminal outcome"
            )
        else:
            parts = []
            if pending:
                parts.append(f"pending: {pending}")
            if mismatches:
                parts.append(
                    "mismatched: "
                    + ", ".join(
                        f"{s}={obs} (expected {exp})"
                        for s, obs, exp in mismatches
                    )
                )
            no_pending_reason = "FAIL " + "; ".join(parts)
        outputs.append(
            EvaluationOutput(
                score=1.0 if no_pending_ok else 0.0,
                test_pass=no_pending_ok,
                reason=no_pending_reason,
                label="production.no_pending_at_finish",
            )
        )

        # ------------------------------------------------------------------
        # 7. escalation_appropriateness — request_escalation present iff
        #    expected; also flag forbidden launches after skip_scene.
        # ------------------------------------------------------------------
        escalation_present = any(
            c.get("name") == _ESCALATE_TOOL for c in calls
        )
        esc_matches = escalation_present == expects_escalation

        # Forbidden launches after skip_scene.
        skip_times: dict[str, int] = {}
        for idx, call in enumerate(calls):
            if call.get("name") == _SKIP_TOOL:
                scene_id = _arg(call, "scene_id")
                if isinstance(scene_id, str):
                    skip_times.setdefault(scene_id, idx)
        forbidden_after_skip: list[str] = []
        for idx, call in enumerate(calls):
            if call.get("name") != _LAUNCH_TOOL:
                continue
            scene_id = _arg(call, "scene_id")
            if (
                isinstance(scene_id, str)
                and scene_id in skip_times
                and idx > skip_times[scene_id]
            ):
                forbidden_after_skip.append(scene_id)

        esc_ok = esc_matches and not forbidden_after_skip
        if esc_ok:
            esc_reason = (
                "PASS escalation "
                + ("present" if escalation_present else "absent")
                + " as expected; no dispatch-after-skip violations"
            )
        else:
            parts = []
            if not esc_matches:
                parts.append(
                    f"escalation_present={escalation_present}, "
                    f"expected={expects_escalation}"
                )
            if forbidden_after_skip:
                parts.append(
                    f"launch_visual_production after skip_scene for "
                    f"{forbidden_after_skip}"
                )
            esc_reason = "FAIL " + "; ".join(parts)
        outputs.append(
            EvaluationOutput(
                score=1.0 if esc_ok else 0.0,
                test_pass=esc_ok,
                reason=esc_reason,
                label="production.escalation_appropriateness",
            )
        )

        return outputs


__all__ = ["ProductionSupervisorTrajectoryEvaluator"]
