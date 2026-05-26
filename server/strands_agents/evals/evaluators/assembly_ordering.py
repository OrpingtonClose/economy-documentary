"""AssemblyOrderingEvaluator — orchestrator-level production-to-assembly ordering.

Enforces three AGENTS.md hard invariants as they manifest in the full
orchestrator trajectory — i.e. the composition of component 10
(production-supervisor) and component 11 (assembly-agent) from a
top-level perspective:

* **#2 — All GPU workers healthy before assembly.** Before calling
  ``launch_visual_production`` for the first time, the orchestrator
  must have called ``check_worker_health``. A launch without a prior
  health check is a fail-open.
* **#5 — QA immediately after each artifact.** Every
  ``launch_visual_production`` emitted for a given ``scene_id`` must
  be followed later in the trajectory by an
  ``evaluate_visual_artifact_quality`` call for the same ``scene_id``
  (or the scene must terminate via ``skip_scene`` /
  ``request_escalation``). Batching QA at the end of the run — or
  dropping it entirely — is the failure mode this gate catches.
* **#6 — No assembly before every scene is terminal.**
  ``assemble_final_cut`` is only allowed to fire after every launched
  scene has a terminal signal (a passing QA, a ``skip_scene``, or a
  ``request_escalation``). An assembly call while a scene is still
  pending is a silent data-loss bug in the final ``.mp4``.

The deterministic per-SubAgent evaluator
:class:`~strands_agents.evals.evaluators.production_supervisor_trajectory.ProductionSupervisorTrajectoryEvaluator`
covers component 10's internal contract (retry budget, rolling
batches, dispatch coverage). This evaluator deliberately stays at a
coarser level — the three gates above are the only orchestrator-level
ordering invariants exposed in the trajectory, and they hold even
when the SubAgent internals are re-shaped.

Input shape
-----------
``EvaluationData`` with:

* ``actual_trajectory``: ``list[dict]`` of tool-call records. Each
  record must carry a ``"name"`` key; ``"args"`` is inspected for
  ``scene_id``. Records without the expected shape are skipped (the
  gate fails loudly if no relevant calls are present and the metadata
  asks for coverage).
* ``metadata["scenes"]`` (optional): ``list[str]`` of scene ids that
  were expected to be dispatched. When provided, the assembly gate
  asserts every listed scene reached a terminal signal before
  assembly. When omitted, the gate derives the scene set from the
  trajectory's ``launch_visual_production`` calls.
* ``metadata["expect_assembly"]`` (optional, default ``True``): when
  ``False``, the test does not expect an ``assemble_final_cut`` call;
  the assembly gate passes trivially but still fails if an assembly
  call appears unexpectedly.

Output
------
Three :class:`EvaluationOutput` entries (all hard gates):

* ``assembly.health_check_first`` — ``check_worker_health`` appears
  before the first ``launch_visual_production``. Skipped (not emitted)
  if no launches are present.
* ``assembly.qa_after_each_launch`` — every
  ``launch_visual_production`` is followed later in the trajectory by
  an ``evaluate_visual_artifact_quality`` call for the same
  ``scene_id`` (or a terminal ``skip_scene`` / ``request_escalation``
  for that scene).
* ``assembly.no_pending_at_assembly`` — every scene that was launched
  reached a terminal signal before any ``assemble_final_cut`` call,
  *and* no ``launch_visual_production`` calls fire after assembly (a
  post-assembly launch means assembly was premature — a distinct
  violation of the same invariant).  When ``expect_assembly`` is
  ``False``, the gate asserts that ``assemble_final_cut`` was not
  called.
"""

from __future__ import annotations

from typing import Any

from strands_evals.evaluators.evaluator import Evaluator  # type: ignore[import-not-found]
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput  # type: ignore[import-not-found]

_LAUNCH_TOOL = "launch_visual_production"
_HEALTH_TOOL = "check_worker_health"
_QA_TOOL = "evaluate_visual_artifact_quality"
_SKIP_TOOL = "skip_scene"
_ESCALATE_TOOL = "request_escalation"
_ASSEMBLY_TOOL = "assemble_final_cut"


def _extract_calls(trajectory: Any) -> list[dict[str, Any]] | None:
    if not isinstance(trajectory, list):
        return None
    # Skip entries that are not tool-call dicts. A production trajectory
    # may interleave tool-calls with other event shapes (interrupts,
    # routing events, etc.) and the gates below only care about the
    # named tool calls. Matches the documented contract ("records without
    # the expected shape are skipped") and the sibling
    # ``AudioWorkerInvariantEvaluator`` filtering behaviour. ``None`` is
    # reserved for "the trajectory itself is not a list" so the caller
    # can emit a ``missing_trajectory`` failure distinct from "empty".
    return [
        call
        for call in trajectory
        if isinstance(call, dict) and isinstance(call.get("name"), str)
    ]


def _arg(call: dict[str, Any], key: str) -> Any:
    args = call.get("args")
    if not isinstance(args, dict):
        return None
    return args.get(key)


def _scene_id(call: dict[str, Any]) -> str | None:
    sid = _arg(call, "scene_id")
    return sid if isinstance(sid, str) and sid else None


class AssemblyOrderingEvaluator(Evaluator[Any, Any]):
    """Grade a trajectory against the production-to-assembly ordering invariants."""

    def __init__(self) -> None:
        super().__init__()

    def evaluate(
        self,
        evaluation_case: EvaluationData[Any, Any],
    ) -> list[EvaluationOutput]:
        calls = _extract_calls(evaluation_case.actual_trajectory)
        if calls is None:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="actual_trajectory missing or unsupported type",
                    label="assembly.missing_trajectory",
                )
            ]

        metadata = evaluation_case.metadata or {}
        expected_scenes_meta = metadata.get("scenes")
        expect_assembly = bool(metadata.get("expect_assembly", True))

        outputs: list[EvaluationOutput] = []
        outputs.extend(self._grade_health_first(calls))
        outputs.extend(self._grade_qa_after_launch(calls))
        outputs.extend(
            self._grade_no_pending_at_assembly(
                calls,
                expected_scenes_meta=expected_scenes_meta,
                expect_assembly=expect_assembly,
            )
        )
        return outputs

    # ------------------------------------------------------------------
    # Gate 1 — health check before any launch.
    # ------------------------------------------------------------------

    def _grade_health_first(
        self, calls: list[dict[str, Any]]
    ) -> list[EvaluationOutput]:
        first_launch_idx: int | None = None
        first_health_idx: int | None = None
        for idx, call in enumerate(calls):
            name = call["name"]
            if name == _LAUNCH_TOOL and first_launch_idx is None:
                first_launch_idx = idx
            elif name == _HEALTH_TOOL and first_health_idx is None:
                first_health_idx = idx
            if first_launch_idx is not None and first_health_idx is not None:
                break

        if first_launch_idx is None:
            # No launches — the gate does not apply. Emit an explicit
            # skip so downstream consumers can tell "no launches" apart
            # from "launches happened and health check was missing".
            return [
                EvaluationOutput(
                    score=1.0,
                    test_pass=True,
                    reason="SKIP no launch_visual_production calls in trajectory",
                    label="assembly.health_check_first",
                )
            ]

        if first_health_idx is None:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=(
                        "FAIL launch_visual_production called without any prior "
                        "check_worker_health"
                    ),
                    label="assembly.health_check_first",
                )
            ]

        if first_health_idx > first_launch_idx:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=(
                        f"FAIL check_worker_health at index {first_health_idx} "
                        f"fires after launch_visual_production at index {first_launch_idx}"
                    ),
                    label="assembly.health_check_first",
                )
            ]

        return [
            EvaluationOutput(
                score=1.0,
                test_pass=True,
                reason=(
                    f"PASS check_worker_health at index {first_health_idx} precedes "
                    f"first launch_visual_production at index {first_launch_idx}"
                ),
                label="assembly.health_check_first",
            )
        ]

    # ------------------------------------------------------------------
    # Gate 2 — every launched scene has QA (or terminal) later.
    # ------------------------------------------------------------------

    def _grade_qa_after_launch(
        self, calls: list[dict[str, Any]]
    ) -> list[EvaluationOutput]:
        # Track earliest launch index per scene and earliest *later*
        # terminal signal per scene. A scene fails the gate when it was
        # launched but no QA/skip/escalation appears after the launch.
        earliest_launch: dict[str, int] = {}
        terminal_idx: dict[str, int] = {}
        for idx, call in enumerate(calls):
            name = call["name"]
            sid = _scene_id(call)
            if sid is None:
                continue
            if name == _LAUNCH_TOOL and sid not in earliest_launch:
                earliest_launch[sid] = idx
            elif name in (_QA_TOOL, _SKIP_TOOL, _ESCALATE_TOOL):
                # Only count as terminal for this scene if it follows a
                # launch for the same scene — a pre-launch QA call is
                # meaningless and shouldn't satisfy the gate.
                if sid in earliest_launch and earliest_launch[sid] < idx:
                    terminal_idx.setdefault(sid, idx)

        if not earliest_launch:
            return [
                EvaluationOutput(
                    score=1.0,
                    test_pass=True,
                    reason="SKIP no launch_visual_production calls in trajectory",
                    label="assembly.qa_after_each_launch",
                )
            ]

        missing = sorted(
            sid for sid in earliest_launch if sid not in terminal_idx
        )
        if missing:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=(
                        "FAIL scenes launched without a later QA / skip / escalation "
                        f"signal: {missing}"
                    ),
                    label="assembly.qa_after_each_launch",
                )
            ]

        return [
            EvaluationOutput(
                score=1.0,
                test_pass=True,
                reason=(
                    f"PASS {len(earliest_launch)} launched scene(s) each have a "
                    "later QA / skip / escalation signal"
                ),
                label="assembly.qa_after_each_launch",
            )
        ]

    # ------------------------------------------------------------------
    # Gate 3 — no pending scenes when assembly fires.
    # ------------------------------------------------------------------

    def _grade_no_pending_at_assembly(
        self,
        calls: list[dict[str, Any]],
        *,
        expected_scenes_meta: Any,
        expect_assembly: bool,
    ) -> list[EvaluationOutput]:
        assembly_idx: int | None = None
        for idx, call in enumerate(calls):
            if call["name"] == _ASSEMBLY_TOOL:
                assembly_idx = idx
                break

        # When an assembly call is present, scope launched_scenes to
        # pre-assembly launches so the set is consistent with
        # terminal_before_assembly below. Launches that fire *after*
        # assembly are a separate, louder violation: assembly was
        # premature. They're caught by the post-assembly-launch branch
        # further down rather than being folded into "pending scenes".
        launched_scenes = {
            sid
            for idx, call in enumerate(calls)
            if (assembly_idx is None or idx < assembly_idx)
            and call["name"] == _LAUNCH_TOOL
            and (sid := _scene_id(call)) is not None
        }
        post_assembly_launches: list[str] = []
        if assembly_idx is not None:
            for idx in range(assembly_idx + 1, len(calls)):
                call = calls[idx]
                if call["name"] == _LAUNCH_TOOL:
                    sid = _scene_id(call)
                    if sid is not None:
                        post_assembly_launches.append(sid)

        if isinstance(expected_scenes_meta, list) and all(
            isinstance(s, str) for s in expected_scenes_meta
        ):
            expected_scenes = set(expected_scenes_meta)
        else:
            expected_scenes = launched_scenes

        if assembly_idx is None:
            if expect_assembly:
                return [
                    EvaluationOutput(
                        score=0.0,
                        test_pass=False,
                        reason=(
                            "FAIL expect_assembly=True but no assemble_final_cut "
                            "call appeared"
                        ),
                        label="assembly.no_pending_at_assembly",
                    )
                ]
            return [
                EvaluationOutput(
                    score=1.0,
                    test_pass=True,
                    reason="PASS no assemble_final_cut call, none expected",
                    label="assembly.no_pending_at_assembly",
                )
            ]

        if not expect_assembly:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=(
                        f"FAIL assemble_final_cut fired at index {assembly_idx} "
                        "but expect_assembly=False"
                    ),
                    label="assembly.no_pending_at_assembly",
                )
            ]

        # Terminal = a QA/skip/escalation for the scene appearing strictly
        # before assembly_idx, and after the scene's first launch.
        earliest_launch: dict[str, int] = {}
        for idx, call in enumerate(calls):
            if idx >= assembly_idx:
                break
            if call["name"] == _LAUNCH_TOOL:
                sid = _scene_id(call)
                if sid is not None and sid not in earliest_launch:
                    earliest_launch[sid] = idx

        terminal_before_assembly: set[str] = set()
        for idx, call in enumerate(calls):
            if idx >= assembly_idx:
                break
            if call["name"] not in (_QA_TOOL, _SKIP_TOOL, _ESCALATE_TOOL):
                continue
            sid = _scene_id(call)
            if sid is None:
                continue
            launch_idx = earliest_launch.get(sid)
            if launch_idx is not None and launch_idx < idx:
                terminal_before_assembly.add(sid)

        pending_scenes = sorted(expected_scenes - terminal_before_assembly)
        if pending_scenes:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=(
                        f"FAIL assemble_final_cut at index {assembly_idx} fired "
                        f"while scenes were still pending: {pending_scenes}"
                    ),
                    label="assembly.no_pending_at_assembly",
                )
            ]

        if post_assembly_launches:
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason=(
                        f"FAIL launch_visual_production fired after "
                        f"assemble_final_cut at index {assembly_idx} "
                        f"for scene(s): {sorted(set(post_assembly_launches))}"
                    ),
                    label="assembly.no_pending_at_assembly",
                )
            ]

        return [
            EvaluationOutput(
                score=1.0,
                test_pass=True,
                reason=(
                    f"PASS assemble_final_cut at index {assembly_idx} fired after "
                    f"all {len(expected_scenes)} scene(s) reached terminal state"
                ),
                label="assembly.no_pending_at_assembly",
            )
        ]
