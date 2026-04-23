"""Scenario-agent experiment factory.

Assembles the :class:`Experiment` the strands-evals runner consumes for
``docs/strands-migration/components/01-scenario-agent.md``. Five cases
(happy path, long-form, edge-short, edge-long, failure) plus the
five-evaluator stack from ``eval-framework/CUSTOM_EVALUATORS.md``.

The ``task`` callable passed to :meth:`Experiment.run_evaluations` is
supplied by whoever drives the run (CI, a shadow runner, a notebook)
so this module stays free of LLM calls during experiment construction.
The playground dispatches through :func:`scenario_task`, which does
run the agent live against a declared LiteLLM model.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from strands_evals.case import Case
from strands_evals.evaluators.coherence_evaluator import CoherenceEvaluator
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.evaluators.faithfulness_evaluator import FaithfulnessEvaluator
from strands_evals.evaluators.trajectory_evaluator import TrajectoryEvaluator
from strands_evals.experiment import Experiment

from contracts import SCENARIO_CONTRACT
from strands_agents.evals.evaluators import (
    ContractComplianceEvaluator,
    ScenarioQualityEvaluator,
)

logger = logging.getLogger(__name__)


#: Minimum score per evaluator — mirrors ``eval-framework/THRESHOLDS.md``.
#: ``True`` in the second element means the threshold is a hard gate; a
#: soft gate (``False``) logs a regression without failing the run.
SCENARIO_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "ContractComplianceEvaluator": (1.0, True),
    "ScenarioQualityEvaluator": (0.7, True),
    "TrajectoryEvaluator": (0.8, True),
    "CoherenceEvaluator": (0.75, False),
    "FaithfulnessEvaluator": (0.7, False),
}


#: Rubric handed to :class:`TrajectoryEvaluator`. The judge prompt
#: consumes this as the success criterion for the tool-call sequence.
SCENARIO_TRAJECTORY_RUBRIC = (
    "The scenario agent must always call generate_scenario first, then "
    "evaluate_scenario, optionally alternating with refine_scenario+"
    "evaluate_scenario until the rating is GOOD or EXCELLENT, and "
    "finally call create_timeline exactly once. Reject trajectories "
    "that skip evaluation, call create_timeline before a passing "
    "evaluation, or invoke tools outside this set."
)

#: Trajectory descriptions shown to the judge LLM. Keyed by tool name.
SCENARIO_TRAJECTORY_DESCRIPTION = {
    "generate_scenario": "Produce the initial scenes list plus visual_style and style_lock.",
    "evaluate_scenario": "Run structural checks and return rating + issues.",
    "refine_scenario": "Adjust scenes based on evaluator feedback.",
    "create_timeline": "Emit the OTIO timeline once scenes are approved.",
}


def scenario_cases() -> list[Case[str, dict[str, Any]]]:
    """Return the five canonical scenario-agent test cases.

    Every case's ``metadata`` carries ``target_duration_sec`` plus the
    knobs :class:`ScenarioQualityEvaluator` forwards to
    :func:`run_all_structural_checks`.
    """
    return [
        Case[str, dict[str, Any]](
            name="economics_basics",
            session_id="scenario-case-001",
            input=(
                "Produce a 5-scene, 5-minute explainer documentary "
                "about inflation suitable for a curious non-economist."
            ),
            expected_trajectory=[
                "generate_scenario",
                "evaluate_scenario",
                "refine_scenario",
                "evaluate_scenario",
                "create_timeline",
            ],
            metadata={
                "target_duration_sec": 300.0,
                "minimum_rating": "GOOD",
            },
        ),
        Case[str, dict[str, Any]](
            name="complex_monetary_policy",
            session_id="scenario-case-002",
            input=(
                "Produce a 10-scene, 10-minute deep dive on the transmission "
                "mechanism of monetary policy across deposit rates, credit "
                "supply, exchange rates, and household expectations."
            ),
            expected_trajectory=[
                "generate_scenario",
                "evaluate_scenario",
                "refine_scenario",
                "evaluate_scenario",
                "create_timeline",
            ],
            metadata={
                "target_duration_sec": 600.0,
                "minimum_rating": "GOOD",
                "per_scene_duration_tolerance": 0.15,
            },
        ),
        Case[str, dict[str, Any]](
            name="edge_single_scene",
            session_id="scenario-case-003",
            input=(
                "Produce a 1-scene, 1-minute micro-documentary on the "
                "gold standard."
            ),
            expected_trajectory=[
                "generate_scenario",
                "evaluate_scenario",
                "create_timeline",
            ],
            metadata={
                "target_duration_sec": 60.0,
                "minimum_rating": "FAIR",
            },
        ),
        Case[str, dict[str, Any]](
            name="edge_max_scenes",
            session_id="scenario-case-004",
            input=(
                "Produce a 15-scene, 15-minute historical survey of inflation "
                "episodes across the 20th century."
            ),
            expected_trajectory=[
                "generate_scenario",
                "evaluate_scenario",
                "refine_scenario",
                "evaluate_scenario",
                "create_timeline",
            ],
            metadata={
                "target_duration_sec": 900.0,
                "minimum_rating": "GOOD",
            },
        ),
        Case[str, dict[str, Any]](
            name="failure_empty_topic",
            session_id="scenario-case-005",
            input="",
            expected_trajectory=["generate_scenario"],
            metadata={
                "target_duration_sec": 300.0,
                "expect_contract_violation": True,
            },
        ),
    ]


def scenario_evaluators() -> list[Evaluator[str, dict[str, Any]]]:
    """Return the evaluator stack applied to every scenario case.

    Order matters only for readability — all evaluators run and every
    returned :class:`EvaluationOutput` contributes to the aggregate
    report. The hard gates (contract, structural quality, trajectory)
    come first.
    """
    return [
        ContractComplianceEvaluator(SCENARIO_CONTRACT),
        ScenarioQualityEvaluator(),
        TrajectoryEvaluator(
            rubric=SCENARIO_TRAJECTORY_RUBRIC,
            trajectory_description=SCENARIO_TRAJECTORY_DESCRIPTION,
        ),
        CoherenceEvaluator(),
        FaithfulnessEvaluator(),
    ]


def build_scenario_experiment() -> Experiment[str, dict[str, Any]]:
    """Construct the :class:`Experiment` for Component 01."""
    return Experiment(cases=scenario_cases(), evaluators=scenario_evaluators())


#: Environment variable that selects which declared model to drive
#: ``scenario_task`` against. When unset, the task picks the first
#: declared model whose provider has credentials in the environment
#: (see :mod:`strands_agents.playground.reachability`).
SCENARIO_PLAYGROUND_MODEL_ENV: str = "SCENARIO_PLAYGROUND_MODEL"

#: Ordered list of provider → env var(s) the task inspects when the
#: explicit override is absent. Mirrors the probe's credentials table
#: but kept decoupled so the task doesn't reach into playground
#: internals.
_PROVIDER_ENV: dict[str, tuple[str, ...]] = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "moonshot": ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
}

#: Default model order when nothing else selects one. Matches the
#: ``c01`` declared-model list in
#: :mod:`strands_agents.playground.registry`: gemini first, then
#: openai, then moonshot.
_DEFAULT_MODEL_ORDER: tuple[str, ...] = (
    "gemini/gemini-3-pro-preview",
    "openai/gpt-4o",
    "moonshot/kimi-k2",
)


def _provider_has_credentials(provider: str) -> bool:
    env_vars = _PROVIDER_ENV.get(provider, ())
    return any(os.environ.get(v) for v in env_vars)


def _pick_model() -> str:
    """Return the model id ``scenario_task`` will drive.

    Respects ``SCENARIO_PLAYGROUND_MODEL`` when set; otherwise walks
    :data:`_DEFAULT_MODEL_ORDER` and returns the first model whose
    provider has credentials in the environment. If none match, falls
    back to the canonical default so the resulting LiteLLM call will
    surface the real missing-credentials error instead of silently
    skipping.
    """
    override = os.environ.get(SCENARIO_PLAYGROUND_MODEL_ENV)
    if override:
        return override
    for model_id in _DEFAULT_MODEL_ORDER:
        provider = model_id.split("/", 1)[0]
        if _provider_has_credentials(provider):
            return model_id
    return _DEFAULT_MODEL_ORDER[0]


def _infer_num_scenes(
    topic: str,
    target_duration_sec: float,
    metadata_hint: Any,
) -> int:
    """Best-effort scene-count inference for the initial generator call.

    Priority: metadata override → the number literal in a phrase like
    ``"5-scene"`` in the topic → ``ceil(target/45)`` (matches the
    scenario generator's instruction docstring). Clamped to the
    plausible ``[1, 40]`` band so a hallucinated number in the topic
    can't blow the generator up.
    """
    try:
        if metadata_hint is not None:
            n = int(metadata_hint)
            if 1 <= n <= 40:
                return n
    except (TypeError, ValueError):
        pass
    import re

    match = re.search(r"(\d+)\s*-\s*scene", (topic or "").lower())
    if match:
        n = int(match.group(1))
        if 1 <= n <= 40:
            return n
    # Fall back to the scenario agent's own planning heuristic:
    # ceil(target_seconds / 45).
    import math

    inferred = max(1, math.ceil(float(target_duration_sec) / 45.0))
    return min(inferred, 40)


def _extract_output_and_trajectory(
    messages: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Walk agent messages, returning accumulated output + tool trajectory.

    The scenario agent's four tools each return a structured dict.
    Strands ``@tool`` serialises the return via ``json.dumps`` into a
    ``toolResult.content[].text`` block, so we can parse each tool
    result back to its original shape and merge into a single output
    dict keyed by the tool semantics:

    * ``generate_scenario`` / ``refine_scenario`` → top-level
      ``scenes``, ``visual_style``, ``style_lock``.
    * ``evaluate_scenario`` → appended to ``evaluator_reports`` list.
    * ``create_timeline`` → top-level ``timeline``.

    The trajectory is the ordered list of ``toolUse.name`` values
    observed, including duplicates when the agent loops.
    """
    trajectory: list[str] = []
    output: dict[str, Any] = {}
    evaluator_reports: list[dict[str, Any]] = []
    use_id_to_name: dict[str, str] = {}

    for msg in messages:
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            use = block.get("toolUse")
            if isinstance(use, dict):
                name = use.get("name")
                use_id = use.get("toolUseId")
                if isinstance(name, str):
                    trajectory.append(name)
                    if isinstance(use_id, str):
                        use_id_to_name[use_id] = name
                continue
            result = block.get("toolResult")
            if not isinstance(result, dict):
                continue
            name = use_id_to_name.get(result.get("toolUseId", ""), "")
            parsed = _parse_tool_result_payload(result)
            if parsed is None:
                continue
            if name in ("generate_scenario", "refine_scenario"):
                for key in ("scenes", "visual_style", "style_lock"):
                    if key in parsed:
                        output[key] = parsed[key]
            elif name == "evaluate_scenario":
                evaluator_reports.append(parsed)
            elif name == "create_timeline":
                output["timeline"] = parsed

    if evaluator_reports:
        output["evaluator_reports"] = evaluator_reports
    return output, trajectory


def _parse_tool_result_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the dict payload from a Strands tool-result block."""
    for content in result.get("content") or []:
        if not isinstance(content, dict):
            continue
        if isinstance(content.get("json"), dict):
            return content["json"]
        text = content.get("text")
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def scenario_task(case: Case[str, dict[str, Any]]) -> dict[str, Any]:
    """Run the real Strands scenario agent against a declared model.

    Previously this was a replay stub that returned ``case.expected_output``
    (always ``None`` for the scenario corpus, hence the empty
    ``output: {}`` the playground's Run pane was rendering). It now:

    1. Picks a declared model (override env var or credentials-present default).
    2. Registers litellm-backed generator + refiner helpers against that model.
    3. Builds :func:`build_scenario_agent` with a matching LiteLLMModel.
    4. Invokes the agent with the case input plus the target duration.
    5. Walks ``agent.messages`` to extract the real tool trajectory and
       the accumulated scenes / visual_style / style_lock / timeline.

    Model-unreachable cases are handled upstream in the playground's
    ``/run`` endpoint — this task runs only after the reachability
    probe has gone green for every declared model.

    Shape matches the other component tasks: ``{output, trajectory, metadata}``.
    """
    # Local imports so importing this module for its experiment
    # factory (e.g. in pytest) doesn't require Strands at import time.
    from strands.models.litellm import LiteLLMModel

    from strands_agents.scenario_agent import (
        build_scenario_agent,
        clear_scenario_helpers,
        set_scenario_helpers,
    )
    from strands_agents.scenario_llm import make_generator, make_refiner

    metadata = case.metadata or {}
    try:
        target_duration_sec = float(metadata.get("target_duration_sec", 300.0))
    except (TypeError, ValueError):
        target_duration_sec = 300.0

    topic = case.input or ""
    num_scenes_hint = _infer_num_scenes(
        topic, target_duration_sec, metadata.get("num_scenes_hint")
    )

    model_id = _pick_model()
    logger.info(
        "scenario_task case=%s model=%s target_duration_sec=%.0f num_scenes_hint=%d",
        case.name,
        model_id,
        target_duration_sec,
        num_scenes_hint,
    )

    prompt = (
        f"{topic}\n\n"
        f"Target total duration: {target_duration_sec:.0f} seconds. "
        f"Plan for roughly {num_scenes_hint} scenes."
    )

    tokens = set_scenario_helpers(
        generator=make_generator(model_id=model_id),
        refiner=make_refiner(model_id=model_id),
    )
    try:
        # Contract enforcement reads ``agent.state['scenes']`` which the
        # scenario tools don't populate (they return JSON to the LLM, not
        # to the state blackboard). The playground rebuilds the output
        # envelope from ``agent.messages`` itself, so we disable the
        # postcondition hook here. The full pipeline leaves it on.
        agent = build_scenario_agent(
            model=LiteLLMModel(model_id=model_id),
            enforce_contract=False,
        )
        agent(prompt)
        messages = list(agent.messages or [])
    finally:
        clear_scenario_helpers(tokens)

    output, trajectory = _extract_output_and_trajectory(messages)
    return {
        "output": output,
        "trajectory": trajectory,
        "metadata": {
            "mode": "live",
            "model": model_id,
            "case": case.name,
            "target_duration_sec": target_duration_sec,
        },
    }
