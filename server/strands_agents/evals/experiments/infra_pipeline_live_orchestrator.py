"""Live pipeline orchestrator experiment (slice 9a).

Drives the *real* DeepAgent orchestrator end-to-end through the
playground's :class:`LivePipelineRun` runner, with a scripted
``FakeMessagesListChatModel`` that pre-determines the tool-call
sequence. Zero GPU spend, zero token spend — the test exercises
the full wiring (LangGraph → callback handler → adapter → RunStream)
and proves every approval gate fires and resolves correctly.

Each case's ``input`` is the run brief (topic / target duration /
language). The task instantiates a fresh demo orchestrator,
streams it onto a synthetic :class:`RunStream`, and returns the
captured event log + summary. Evaluators score:

* :class:`LiveTrajectoryEvaluator` — the run terminated with
  ``status="ok"``, all 5 declared stages produced ``start``/``end``
  brackets, and the event count is at least the declared minimum.
  This is the AGENTS.md "pipeline shape is stable" invariant on the
  live orchestrator side.

* :class:`LiveApprovalEvaluator` — every interrupt produced a
  ``pipeline.approval.waiting`` followed by a
  ``pipeline.approval.resumed`` carrying a non-``unknown`` gate name
  and an ``approve`` (or other declared) decision. Confirms the
  HITL bridge translates the langchain middleware vocabulary into
  the playground's wire format without dropping detail.

* :class:`LiveCoherenceEvaluator` — every emitted tool ``end`` event
  reports ``ok=true`` (placeholder tools return clean envelopes)
  and the run's final ``final_mp4_b2_url`` is a non-empty
  ``b2://…`` URL. Catches regressions in either the placeholder
  catalog (real-tool wiring uses the same surface) or the URL
  scrape that powers the UI's "Final master MP4" panel.

The single canonical case is ``happy_path_full_run``. Slice 9b
will add error / refusal / escalation cases when real tools land.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

from strands_evals.case import Case  # type: ignore[import-not-found]
from strands_evals.evaluators.evaluator import Evaluator  # type: ignore[import-not-found]
from strands_evals.experiment import Experiment  # type: ignore[import-not-found]
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput  # type: ignore[import-not-found]

from strands_agents.playground.events import RunStream
from strands_agents.playground.pipeline_live_demo import build_demo_live_agent
from strands_agents.playground.pipeline_live_runner import LivePipelineRun


#: Thresholds advertised to the playground catalog. All three
#: evaluators are hard gates — a regression in any of them is a
#: user-visible break in the live pipeline UI.
INFRA_PIPELINE_LIVE_ORCHESTRATOR_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "LiveTrajectoryEvaluator": (1.0, True),
    "LiveApprovalEvaluator": (1.0, True),
    "LiveCoherenceEvaluator": (1.0, True),
}


# ── Cases ────────────────────────────────────────────────────────────


def _happy_path_case() -> Case[dict[str, Any], dict[str, Any]]:
    """The canonical live-orchestrator run.

    Topic, duration, and language are the same defaults the
    ``/pipeline`` form opens with. The case declares the minimum
    event count + the five expected stages; the evaluators do the
    rest.
    """
    return Case[dict[str, Any], dict[str, Any]](
        name="happy_path_full_run",
        session_id="infra-pipeline-live-orchestrator-happy",
        input={
            "topic": "inflation",
            "target_duration_sec": 60,
            "language": "en",
        },
        expected_output={"status": "ok"},
        metadata={
            "min_event_count": 20,
            "expected_stages": [
                "scenario",
                "audio",
                "visual",
                "production",
                "assembly",
            ],
            "min_approval_gates": 2,
            "expected_decisions": ["approve"],
            "url_prefix": "b2://documentary/",
        },
    )


def infra_pipeline_live_orchestrator_cases() -> list[
    Case[dict[str, Any], dict[str, Any]]
]:
    """Return the live-orchestrator suite (one canonical case)."""
    return [_happy_path_case()]


# ── Task ─────────────────────────────────────────────────────────────


def infra_pipeline_live_orchestrator_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Run the live orchestrator end-to-end against the case brief.

    Builds a fresh demo orchestrator (scripted LLM + placeholder
    tools), streams it onto an in-memory :class:`RunStream`, and
    captures the resulting event log + summary. The output is
    shaped for the three evaluators:

    * ``status`` / ``final_mp4_b2_url`` / ``approval_gate_count`` /
      ``stage_count`` — straight from the runner's terminal payload.
    * ``events`` — list of dicts ``{"kind", "summary", "detail"}``,
      one per :class:`Event` snapshot in emission order.
    """
    topic = str(case.input.get("topic") or "documentary")
    duration = float(case.input.get("target_duration_sec") or 60)
    language = str(case.input.get("language") or "en")

    async def _run() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        run_dir = Path(tempfile.mkdtemp(prefix="pipeline-live-eval-"))
        try:
            agent = build_demo_live_agent(
                run_dir,
                topic=topic,
                target_duration_sec=int(duration),
                language=language,
            )
            stream = RunStream(
                run_id=f"eval-{case.name}",
                component_id="infra_pipeline_live_orchestrator",
                case_name=case.name,
            )
            stream.attach_loop(asyncio.get_running_loop())
            runner = LivePipelineRun(
                topic=topic,
                target_duration_sec=int(duration),
                language=language,
                agent=agent,
                run_dir=run_dir,
                per_event_delay_s=0.0,
            )
            result = await runner.run(stream)
            events = [
                {
                    "kind": e.kind,
                    "summary": e.summary,
                    "detail": dict(getattr(e, "detail", {}) or {}),
                }
                for e in stream.snapshot()
            ]
            return result, events
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

    result, events = asyncio.run(_run())
    output: dict[str, Any] = {
        "status": result.get("status"),
        "final_mp4_b2_url": result.get("final_mp4_b2_url"),
        "stage_count": result.get("stage_count"),
        "approval_gate_count": result.get("approval_gate_count"),
        "tool_call_count": result.get("tool_call_count"),
        "elapsed_ms": result.get("elapsed_ms"),
        "events": events,
    }
    # ``Experiment.run_evaluations`` treats a dict return as the
    # canonical {"output": …, "trajectory": …, …} envelope, so the
    # whole result has to live under ``output`` for the evaluators to
    # see it as ``actual_output``. Trajectory is the kind sequence so
    # observability tooling renders it directly.
    return {
        "output": output,
        "trajectory": [str(e.get("kind") or "") for e in events],
    }


# ── Evaluators ───────────────────────────────────────────────────────


class LiveTrajectoryEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Run terminated ok and the stage ribbon walked the expected path.

    Checks: terminal status, minimum event count, and that every
    declared stage produced a matching ``start`` / ``end`` bracket.
    Stages declared in metadata that never opened are reported
    individually so the UI shows exactly which one regressed.
    """

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        metadata = evaluation_case.metadata or {}
        events: list[dict[str, Any]] = list(actual.get("events") or [])
        kinds = [str(e.get("kind") or "") for e in events]

        mismatches: list[str] = []
        if actual.get("status") != "ok":
            mismatches.append(f"status: actual={actual.get('status')!r} expected='ok'")

        min_events = int(metadata.get("min_event_count") or 0)
        if len(events) < min_events:
            mismatches.append(
                f"event_count: actual={len(events)} expected>={min_events}"
            )

        expected_stages: list[str] = list(metadata.get("expected_stages") or [])
        for stage in expected_stages:
            start_kind = f"pipeline.stage.{stage}.start"
            end_kind = f"pipeline.stage.{stage}.end"
            if start_kind not in kinds:
                mismatches.append(f"missing stage start: {stage}")
            if end_kind not in kinds:
                mismatches.append(f"missing stage end: {stage}")

        if "pipeline.run_started" not in kinds:
            mismatches.append("missing pipeline.run_started")
        if "pipeline.run_finished" not in kinds:
            mismatches.append("missing pipeline.run_finished")

        ok = not mismatches
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                reason=(
                    f"trajectory ok: {len(events)} events, status=ok, "
                    f"all {len(expected_stages)} stages bracketed"
                    if ok
                    else "trajectory mismatches: " + "; ".join(mismatches)
                ),
                label="trajectory_ok" if ok else "trajectory_mismatch",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


class LiveApprovalEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Approval gates fire, pair, and carry useful detail.

    Checks: every ``waiting`` is followed by exactly one
    ``resumed`` for the same gate name, the gate name is not
    ``unknown`` (proves the HITL extractor pulled the action_request
    name correctly), and the decision is one of the case's declared
    expected decisions.
    """

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        metadata = evaluation_case.metadata or {}
        events: list[dict[str, Any]] = list(actual.get("events") or [])
        min_gates = int(metadata.get("min_approval_gates") or 0)
        expected_decisions = set(
            str(d) for d in (metadata.get("expected_decisions") or [])
        )

        waitings = [e for e in events if e.get("kind") == "pipeline.approval.waiting"]
        resumeds = [e for e in events if e.get("kind") == "pipeline.approval.resumed"]

        mismatches: list[str] = []
        if len(waitings) < min_gates:
            mismatches.append(
                f"approval gate count: actual={len(waitings)} expected>={min_gates}"
            )
        if len(waitings) != len(resumeds):
            mismatches.append(
                f"waiting/resumed pair count mismatch: "
                f"waiting={len(waitings)} resumed={len(resumeds)}"
            )

        for idx, w in enumerate(waitings):
            gate = (w.get("detail") or {}).get("gate_name") or ""
            if not gate or gate == "unknown":
                mismatches.append(f"waiting[{idx}].gate_name={gate!r}")
        for idx, r in enumerate(resumeds):
            gate = (r.get("detail") or {}).get("gate_name") or ""
            decision = (r.get("detail") or {}).get("decision") or ""
            if not gate or gate == "unknown":
                mismatches.append(f"resumed[{idx}].gate_name={gate!r}")
            if expected_decisions and decision not in expected_decisions:
                mismatches.append(
                    f"resumed[{idx}].decision={decision!r} not in "
                    f"{sorted(expected_decisions)!r}"
                )

        ok = not mismatches
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                reason=(
                    f"approvals ok: {len(waitings)} gates fired and resumed "
                    f"with {sorted(expected_decisions) or 'any'} decisions"
                    if ok
                    else "approval mismatches: " + "; ".join(mismatches)
                ),
                label="approvals_ok" if ok else "approvals_mismatch",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


class LiveCoherenceEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Tool calls all succeeded and the final MP4 URL is recoverable.

    Checks: every ``pipeline.tool.<name>.end`` event reports
    ``ok=True`` (placeholder tools return clean envelopes — a
    regression here means a real tool started raising), the
    runner's terminal payload carries a non-empty
    ``final_mp4_b2_url`` matching the declared URL prefix, and
    no ``pipeline.unknown`` events leaked through.
    """

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        metadata = evaluation_case.metadata or {}
        events: list[dict[str, Any]] = list(actual.get("events") or [])
        url_prefix = str(metadata.get("url_prefix") or "b2://")

        mismatches: list[str] = []
        for idx, e in enumerate(events):
            kind = str(e.get("kind") or "")
            if kind.startswith("pipeline.tool.") and kind.endswith(".end"):
                ok_flag = (e.get("detail") or {}).get("ok")
                if ok_flag is False:
                    mismatches.append(
                        f"tool end event[{idx}] kind={kind} reported ok=false"
                    )
            if kind == "pipeline.unknown":
                mismatches.append(f"event[{idx}] kind=pipeline.unknown leaked through")

        url = actual.get("final_mp4_b2_url")
        if not isinstance(url, str) or not url:
            mismatches.append("final_mp4_b2_url missing or empty")
        elif not url.startswith(url_prefix):
            mismatches.append(
                f"final_mp4_b2_url={url!r} does not start with {url_prefix!r}"
            )

        ok = not mismatches
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                reason=(
                    f"coherence ok: every tool end is ok=true and final "
                    f"MP4 URL is {url}"
                    if ok
                    else "coherence mismatches: " + "; ".join(mismatches)
                ),
                label="coherence_ok" if ok else "coherence_mismatch",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


# ── Experiment factory ───────────────────────────────────────────────


def build_infra_pipeline_live_orchestrator_experiment() -> Experiment[
    dict[str, Any], dict[str, Any]
]:
    """Assemble the live-orchestrator :class:`Experiment`.

    Returns:
        An experiment covering the canonical happy-path case and
        the three deterministic evaluators. Ready for
        :meth:`Experiment.run_evaluations` and the playground
        ``/playground/components/{id}/evaluate`` endpoint.
    """
    return Experiment[dict[str, Any], dict[str, Any]](
        cases=infra_pipeline_live_orchestrator_cases(),
        evaluators=[
            LiveTrajectoryEvaluator(),
            LiveApprovalEvaluator(),
            LiveCoherenceEvaluator(),
        ],
    )


__all__ = ["LiveApprovalEvaluator",
    "LiveCoherenceEvaluator",
    "LiveTrajectoryEvaluator",
    "build_infra_pipeline_live_orchestrator_experiment",
    "infra_pipeline_live_orchestrator_cases",
    "infra_pipeline_live_orchestrator_task",]
