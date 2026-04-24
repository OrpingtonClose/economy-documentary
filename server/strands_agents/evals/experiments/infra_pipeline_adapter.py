"""Pipeline playground adapter experiment (slice 7).

Drives :mod:`strands_agents.playground.pipeline_adapter` through
scripted orchestrator event sequences and scores the resulting
playground event stream with deterministic evaluators.

Each case's ``input`` is a list of ``(event_type, data)`` pairs
exactly as the orchestrator would emit them. The task replays the
list through :func:`translate_pipeline_event` and returns the full
stream of :class:`TranslatedEvent` triples. Evaluators then check:

* :class:`AdapterEventShapeEvaluator` — for each input, the output
  ``kind`` matches expectation and the ``detail`` carries every key
  the case declares (extra keys are fine — the translator may learn
  to carry richer payloads over time, which must not regress already
  well-shaped cases).

* :class:`StageSequenceEvaluator` — the sequence of
  ``pipeline.stage.*.start`` / ``pipeline.stage.*.end`` kinds in the
  output matches the case's declared stage order, and every started
  stage finishes. This is the adapter-side gate for AGENTS.md's
  "pipeline shape is stable" invariant — a regression that swaps
  stage order or drops a stage is visible here.

Cases cover:

* **happy_path_full_run** — the five-stage canned plan from
  :func:`default_simulation_stages`, end-to-end. Exercises every
  branch of the translator and the full stage ribbon. The single
  largest case; everything else is intentionally narrower.
* **approval_gate_pauses** — a run that emits
  ``pipeline.approval_gate`` and ``pipeline.approval_resumed`` at
  the visual→production boundary. Confirms those events become
  ``pipeline.approval.waiting`` / ``pipeline.approval.resumed`` and
  carry the gate name.
* **unknown_event_surfaces** — the translator sees an event type it
  was never taught about. Output must carry ``pipeline.unknown`` and
  preserve the original ``event_type`` under
  ``detail.source_event_type`` — the "never drop" invariant.
* **missing_fields_defaulted** — events arrive without optional
  fields (e.g. a ``stage_finished`` without ``elapsed_ms``). Output
  must use conservative defaults, not raise ``KeyError``.
* **stage_failure_surfaces** — ``pipeline.stage_failed`` with a
  reason + nested detail. Output kind is ``pipeline.stage_failed``
  and the summary includes the reason so the UI shows a useful
  red banner.
"""

from __future__ import annotations

from typing import Any

from strands_evals.case import Case
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.experiment import Experiment
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from strands_agents.playground.pipeline_adapter import (
    default_simulation_stages,
    generate_simulation_events,
    translate_pipeline_event,
)


#: Thresholds advertised to the playground catalog. Both evaluators
#: are hard gates — a translator that mis-shapes a single event or
#: breaks the stage sequence is a user-visible regression.
INFRA_PIPELINE_ADAPTER_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "AdapterEventShapeEvaluator": (1.0, True),
    "StageSequenceEvaluator": (1.0, True),
}


# ── Case schema ──────────────────────────────────────────────────────
#
# Each case's ``input`` is ``{"events": [(event_type, data), …]}``.
# Metadata carries:
#   * ``expected_kinds``: list of kinds, one per input event, in
#     order. The shape evaluator checks positional match.
#   * ``expected_detail_subsets``: list of dicts, one per input
#     event, in order. Each subset is a set of key/value pairs the
#     output ``detail`` must contain (extras OK).
#   * ``expected_stage_sequence``: list of stage names the stage
#     evaluator expects to see bracketed as ``start`` + ``end`` in
#     order.


def _case(
    name: str,
    *,
    events: list[tuple[str, dict[str, Any]]],
    expected_kinds: list[str],
    expected_detail_subsets: list[dict[str, Any]],
    expected_stage_sequence: list[str],
) -> Case[dict[str, Any], dict[str, Any]]:
    if len(expected_kinds) != len(events):
        raise ValueError(
            f"case {name}: expected_kinds length {len(expected_kinds)} "
            f"does not match events length {len(events)}"
        )
    if len(expected_detail_subsets) != len(events):
        raise ValueError(
            f"case {name}: expected_detail_subsets length "
            f"{len(expected_detail_subsets)} does not match events "
            f"length {len(events)}"
        )
    return Case[dict[str, Any], dict[str, Any]](
        name=name,
        session_id=f"infra-pipeline-adapter-{name}",
        input={"events": [list(pair) for pair in events]},
        expected_output={"kinds": expected_kinds},
        metadata={
            "expected_kinds": expected_kinds,
            "expected_detail_subsets": expected_detail_subsets,
            "expected_stage_sequence": expected_stage_sequence,
        },
    )


def _happy_path_case() -> Case[dict[str, Any], dict[str, Any]]:
    """Build the full five-stage happy-path case from the canned plan."""
    events = generate_simulation_events(
        topic="industrial policy",
        target_duration_sec=60,
        language="en",
        stages=default_simulation_stages(),
    )
    expected_kinds: list[str] = []
    expected_detail_subsets: list[dict[str, Any]] = []
    for event_type, data in events:
        if event_type == "pipeline.run_started":
            expected_kinds.append("pipeline.run_started")
            expected_detail_subsets.append(
                {
                    "topic": "industrial policy",
                    "target_duration_sec": 60,
                    "language": "en",
                }
            )
        elif event_type == "pipeline.stage_started":
            expected_kinds.append(f"pipeline.stage.{data['stage']}.start")
            expected_detail_subsets.append({"stage": data["stage"]})
        elif event_type == "pipeline.stage_finished":
            expected_kinds.append(f"pipeline.stage.{data['stage']}.end")
            expected_detail_subsets.append({"stage": data["stage"]})
        elif event_type == "pipeline.tool_call_started":
            expected_kinds.append(f"pipeline.tool.{data['tool']}.start")
            expected_detail_subsets.append({"tool": data["tool"]})
        elif event_type == "pipeline.tool_call_finished":
            expected_kinds.append(f"pipeline.tool.{data['tool']}.end")
            expected_detail_subsets.append({"tool": data["tool"], "ok": True})
        elif event_type == "pipeline.artifact":
            expected_kinds.append("pipeline.artifact")
            expected_detail_subsets.append({"artifact_kind": data["kind"]})
        elif event_type == "pipeline.approval_gate":
            expected_kinds.append("pipeline.approval.waiting")
            expected_detail_subsets.append({"gate_name": data["gate_name"]})
        elif event_type == "pipeline.approval_resumed":
            expected_kinds.append("pipeline.approval.resumed")
            expected_detail_subsets.append(
                {"gate_name": data["gate_name"], "decision": "accept"}
            )
        elif event_type == "pipeline.run_finished":
            expected_kinds.append("pipeline.run_finished")
            expected_detail_subsets.append({"status": "ok"})
        else:
            raise AssertionError(
                f"simulation generated unexpected event type {event_type!r}"
            )
    return _case(
        "happy_path_full_run",
        events=events,
        expected_kinds=expected_kinds,
        expected_detail_subsets=expected_detail_subsets,
        expected_stage_sequence=[
            "scenario",
            "audio",
            "visual",
            "production",
            "assembly",
        ],
    )


def infra_pipeline_adapter_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Return the canonical adapter translation suite."""
    return [
        _happy_path_case(),
        _case(
            "approval_gate_pauses",
            events=[
                (
                    "pipeline.run_started",
                    {
                        "topic": "tariffs",
                        "target_duration_sec": 45,
                        "language": "en",
                    },
                ),
                (
                    "pipeline.stage_started",
                    {"stage": "visual", "scene_count": 3},
                ),
                (
                    "pipeline.stage_finished",
                    {"stage": "visual", "elapsed_ms": 900},
                ),
                (
                    "pipeline.approval_gate",
                    {
                        "gate_name": "launch_visual_production",
                        "allowed_decisions": [
                            "accept",
                            "edit",
                            "reject",
                        ],
                    },
                ),
                (
                    "pipeline.approval_resumed",
                    {
                        "gate_name": "launch_visual_production",
                        "decision": "accept",
                    },
                ),
                (
                    "pipeline.stage_started",
                    {"stage": "production", "scene_count": 3},
                ),
                (
                    "pipeline.stage_finished",
                    {"stage": "production", "elapsed_ms": 5500},
                ),
                ("pipeline.run_finished", {"status": "ok"}),
            ],
            expected_kinds=[
                "pipeline.run_started",
                "pipeline.stage.visual.start",
                "pipeline.stage.visual.end",
                "pipeline.approval.waiting",
                "pipeline.approval.resumed",
                "pipeline.stage.production.start",
                "pipeline.stage.production.end",
                "pipeline.run_finished",
            ],
            expected_detail_subsets=[
                {"topic": "tariffs"},
                {"stage": "visual"},
                {"stage": "visual", "elapsed_ms": 900},
                {"gate_name": "launch_visual_production"},
                {
                    "gate_name": "launch_visual_production",
                    "decision": "accept",
                },
                {"stage": "production"},
                {"stage": "production", "elapsed_ms": 5500},
                {"status": "ok"},
            ],
            expected_stage_sequence=["visual", "production"],
        ),
        _case(
            "unknown_event_surfaces",
            events=[
                (
                    "pipeline.run_started",
                    {
                        "topic": "future event",
                        "target_duration_sec": 30,
                        "language": "en",
                    },
                ),
                (
                    # Event type the translator has not been taught
                    # about — must not drop, must carry source type.
                    "pipeline.brand_new_event_not_in_contract",
                    {"extra_field": 42},
                ),
                ("pipeline.run_finished", {"status": "ok"}),
            ],
            expected_kinds=[
                "pipeline.run_started",
                "pipeline.unknown",
                "pipeline.run_finished",
            ],
            expected_detail_subsets=[
                {"topic": "future event"},
                {"source_event_type": ("pipeline.brand_new_event_not_in_contract")},
                {"status": "ok"},
            ],
            expected_stage_sequence=[],
        ),
        _case(
            "missing_fields_defaulted",
            events=[
                # Intentionally missing ``topic``, ``target_duration_sec``
                # and ``language`` — translator must default, not raise.
                ("pipeline.run_started", {}),
                # ``stage_finished`` without ``elapsed_ms``.
                (
                    "pipeline.stage_finished",
                    {"stage": "scenario"},
                ),
                # ``tool_call_finished`` without ``ok``.
                (
                    "pipeline.tool_call_finished",
                    {
                        "tool": "evaluate_scenario",
                        "agent": "scenario_agent",
                        "elapsed_ms": 120,
                    },
                ),
                ("pipeline.run_finished", {"status": "ok"}),
            ],
            expected_kinds=[
                "pipeline.run_started",
                "pipeline.stage.scenario.end",
                "pipeline.tool.evaluate_scenario.end",
                "pipeline.run_finished",
            ],
            expected_detail_subsets=[
                {"topic": "unknown topic"},
                {"stage": "scenario", "elapsed_ms": 0},
                {"tool": "evaluate_scenario", "ok": True},
                {"status": "ok"},
            ],
            expected_stage_sequence=[],
        ),
        _case(
            "stage_failure_surfaces",
            events=[
                (
                    "pipeline.run_started",
                    {
                        "topic": "production failure",
                        "target_duration_sec": 60,
                        "language": "en",
                    },
                ),
                (
                    "pipeline.stage_started",
                    {"stage": "production", "scene_count": 4},
                ),
                (
                    "pipeline.stage_failed",
                    {
                        "stage": "production",
                        "reason": "ltx_video_worker_unreachable",
                        "detail": {"worker_id": "ltx-h200-1"},
                    },
                ),
                (
                    "pipeline.run_finished",
                    {"status": "failed"},
                ),
            ],
            expected_kinds=[
                "pipeline.run_started",
                "pipeline.stage.production.start",
                "pipeline.stage_failed",
                "pipeline.run_finished",
            ],
            expected_detail_subsets=[
                {"topic": "production failure"},
                {"stage": "production"},
                {
                    "stage": "production",
                    "reason": "ltx_video_worker_unreachable",
                },
                {"status": "failed"},
            ],
            expected_stage_sequence=["production"],
        ),
    ]


# ── Task adapter ─────────────────────────────────────────────────────


def infra_pipeline_adapter_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Replay the case's event sequence through the translator.

    Called by the playground's run endpoint. Returns an
    ``actual_output`` shaped for the two evaluators below:

    * ``kinds``: list of kinds the translator emitted, in order.
    * ``details``: list of detail dicts, in order.
    * ``summaries``: list of summary strings, in order — useful
      visible evidence in the UI even though evaluators do not
      score free-form summary text.
    """
    events = case.input.get("events") or []
    kinds: list[str] = []
    details: list[dict[str, Any]] = []
    summaries: list[str] = []
    for event_type, data in events:
        translated = translate_pipeline_event(event_type, dict(data or {}))
        kinds.append(translated.kind)
        details.append(translated.detail)
        summaries.append(translated.summary)
    return {
        "kinds": kinds,
        "details": details,
        "summaries": summaries,
        "event_count": len(events),
    }


# ── Evaluators ───────────────────────────────────────────────────────


class AdapterEventShapeEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Pin every translated event's kind + detail subset to expected.

    For each input event the case declares a target ``kind`` and a
    subset of ``detail`` keys that must be present with matching
    values. Extra keys in the actual detail are allowed — the
    translator is allowed to grow richer output over time without
    regressing this evaluator.
    """

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        metadata = evaluation_case.metadata or {}
        expected_kinds: list[str] = list(metadata.get("expected_kinds") or [])
        expected_subsets: list[dict[str, Any]] = list(
            metadata.get("expected_detail_subsets") or []
        )
        actual_kinds: list[str] = list(actual.get("kinds") or [])
        actual_details: list[dict[str, Any]] = list(actual.get("details") or [])

        mismatches: list[str] = []
        if len(actual_kinds) != len(expected_kinds):
            mismatches.append(
                f"event count: actual={len(actual_kinds)} "
                f"expected={len(expected_kinds)}"
            )
        for idx, (exp_kind, act_kind) in enumerate(zip(expected_kinds, actual_kinds)):
            if exp_kind != act_kind:
                mismatches.append(
                    f"event[{idx}].kind: actual={act_kind!r} expected={exp_kind!r}"
                )
        for idx, (exp_subset, act_detail) in enumerate(
            zip(expected_subsets, actual_details)
        ):
            for key, expected_value in exp_subset.items():
                actual_value = act_detail.get(key)
                if actual_value != expected_value:
                    mismatches.append(
                        f"event[{idx}].detail.{key}: "
                        f"actual={actual_value!r} "
                        f"expected={expected_value!r}"
                    )

        ok = not mismatches
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                reason=(
                    "all translated events match kind + detail subsets"
                    if ok
                    else "shape mismatches: " + "; ".join(mismatches)
                ),
                label="shape_match" if ok else "shape_mismatch",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


class StageSequenceEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Check stage-level brackets match the expected stage order.

    Walks the output kinds, extracts the sequence of
    ``pipeline.stage.<name>.start`` / ``pipeline.stage.<name>.end``
    brackets, and asserts that:

    * The sequence of start-kinds in order matches the case's
      ``expected_stage_sequence``.
    * Every start is followed by a matching end for the same stage
      before the next stage starts — no interleaving, no orphaned
      starts, no orphaned ends.
    """

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        metadata = evaluation_case.metadata or {}
        expected_sequence: list[str] = list(
            metadata.get("expected_stage_sequence") or []
        )
        actual_kinds: list[str] = list(actual.get("kinds") or [])

        # Cases that declare no stage sequence are opting out of the
        # bracket-integrity check — the adapter is a pure translator
        # and does not enforce orchestrator-level sequencing. Cases
        # that care about sequencing declare the expected stages
        # explicitly; everything else passes trivially.
        if not expected_sequence:
            return [
                EvaluationOutput(
                    score=1.0,
                    test_pass=True,
                    reason="no stage sequence declared for this case",
                    label="sequence_not_required",
                )
            ]

        # Extract stage brackets from the output. ``.start`` opens a
        # stage; both ``.end`` AND ``pipeline.stage_failed`` count as
        # valid closes — a failed stage is still a closed stage from
        # the UI ribbon's perspective.
        stage_events: list[tuple[str, str]] = []
        actual_details: list[dict[str, Any]] = list(actual.get("details") or [])
        for idx, kind in enumerate(actual_kinds):
            if kind.startswith("pipeline.stage.") and kind.endswith(".start"):
                stage = kind.removeprefix("pipeline.stage.").removesuffix(".start")
                stage_events.append((stage, "start"))
                continue
            if kind.startswith("pipeline.stage.") and kind.endswith(".end"):
                stage = kind.removeprefix("pipeline.stage.").removesuffix(".end")
                stage_events.append((stage, "end"))
                continue
            if kind == "pipeline.stage_failed":
                # stage name carried on the detail payload produced
                # by the translator, not on the kind string.
                failed_stage = (
                    actual_details[idx].get("stage")
                    if idx < len(actual_details)
                    else None
                )
                if failed_stage:
                    stage_events.append((str(failed_stage), "end"))

        started_order: list[str] = [
            stage for (stage, bracket) in stage_events if bracket == "start"
        ]
        mismatches: list[str] = []
        if started_order != expected_sequence:
            mismatches.append(
                f"stage start sequence: actual={started_order!r} "
                f"expected={expected_sequence!r}"
            )

        # Bracket integrity: walk the events, require start → close
        # for the same stage, reject interleaving.
        open_stack: list[str] = []
        for stage, bracket in stage_events:
            if bracket == "start":
                open_stack.append(stage)
                continue
            if not open_stack:
                mismatches.append(f"stage.{stage} close without matching start")
                continue
            top = open_stack.pop()
            if top != stage:
                mismatches.append(
                    f"stage.{stage} close does not match open start stage.{top}"
                )
        if open_stack:
            mismatches.append(f"unclosed stages at end of stream: {open_stack!r}")

        ok = not mismatches
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                reason=(
                    "stage sequence matches expected and brackets close"
                    if ok
                    else "sequence mismatches: " + "; ".join(mismatches)
                ),
                label="sequence_match" if ok else "sequence_mismatch",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


# ── Experiment factory ───────────────────────────────────────────────


def build_infra_pipeline_adapter_experiment() -> Experiment[
    dict[str, Any], dict[str, Any]
]:
    """Assemble the pipeline adapter :class:`Experiment`.

    Returns:
        An experiment covering the five canonical cases and the two
        deterministic evaluators. Ready for
        :meth:`Experiment.run_evaluations` and the playground
        ``/playground/components/{id}/evaluate`` endpoint.
    """
    return Experiment[dict[str, Any], dict[str, Any]](
        cases=infra_pipeline_adapter_cases(),
        evaluators=[
            AdapterEventShapeEvaluator(),
            StageSequenceEvaluator(),
        ],
    )


__all__ = [
    "AdapterEventShapeEvaluator",
    "INFRA_PIPELINE_ADAPTER_EVALUATOR_THRESHOLDS",
    "StageSequenceEvaluator",
    "build_infra_pipeline_adapter_experiment",
    "infra_pipeline_adapter_cases",
    "infra_pipeline_adapter_task",
]
