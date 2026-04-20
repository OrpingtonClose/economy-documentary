# EVAL_ARCHITECTURE — how we use strands-evals

Every component ships with an `Experiment` (from
[`strands_evals.experiment`](https://github.com/OrpingtonClose/strands-evals/blob/main/src/strands_evals/experiment.py)).
This document is the exhaustive reference for how the harness is wired.

---

## 1. Core primitives

From `strands_evals`:

| Primitive | File | Role |
|-----------|------|------|
| `Case[InputT, OutputT]` | [`case.py`](https://github.com/OrpingtonClose/strands-evals/blob/main/src/strands_evals/case.py) | Single test case: `input`, `expected_output`, `expected_trajectory`, `expected_interactions`, `expected_environment_state`, `expected_assertion`, `metadata`. Auto-generates `session_id`. |
| `Experiment[InputT, OutputT]` | [`experiment.py`](https://github.com/OrpingtonClose/strands-evals/blob/main/src/strands_evals/experiment.py) | Collection of `Case`s + `list[Evaluator]`. `run_evaluations(task)` executes the task on every case and returns `list[EvaluationReport]`. |
| `Evaluator[InputT, OutputT]` | [`evaluators/evaluator.py`](https://github.com/OrpingtonClose/strands-evals/blob/main/src/strands_evals/evaluators/evaluator.py) | Abstract base. Implementations return `list[EvaluationOutput]`. |
| `EvaluationReport` | [`types/evaluation_report.py`](https://github.com/OrpingtonClose/strands-evals/blob/main/src/strands_evals/types/evaluation_report.py) | `overall_score`, `scores`, `test_passes`, `cases`, `reasons`. |
| `EvaluationDataStore` | [`evaluation_data_store.py`](https://github.com/OrpingtonClose/strands-evals/blob/main/src/strands_evals/evaluation_data_store.py) | Cache task outputs per case so evaluator iteration doesn't re-run expensive agents. |
| `Experiment.to_file` / `Experiment.from_file` | `experiment.py` | JSON serialization. We commit one JSON file per component so cases are code-reviewable. |

### Task contract

Every `Experiment` runs against a `task: Callable[[Case], OutputT | dict]`.
The task either returns a raw output or a dict of the shape

```python
{"output": ..., "trajectory": [...], "interactions": [...], "environment_state": {...}}
```

For a Strands agent wrapper it looks like:

```python
async def task(case: Case) -> dict:
    agent = build_scenario_agent()
    result = await agent.invoke_async(case.input, invocation_state=case.metadata)
    return {
        "output": result.message,
        "trajectory": [tool_use.name for tool_use in result.tool_uses],
    }
```

Sync tasks are auto-dispatched to a thread (`experiment.py:201–203`), async
tasks are awaited directly.

---

## 2. Built-in evaluators we use

From [`strands_evals/evaluators/`](https://github.com/OrpingtonClose/strands-evals/tree/main/src/strands_evals/evaluators):

| Evaluator | File | Used by components |
|-----------|------|--------------------|
| `OutputEvaluator` | `output_evaluator.py` | 01, 03, 06, 07, 10, 13 |
| `TrajectoryEvaluator` | `trajectory_evaluator.py` | 01, 05, 09, 10 |
| `GoalSuccessRateEvaluator` | `goal_success_rate_evaluator.py` | 10, 14 |
| `CoherenceEvaluator` | `coherence_evaluator.py` | 01, 08 |
| `FaithfulnessEvaluator` | `faithfulness_evaluator.py` | 01, 06 |
| `ToolSelectionAccuracyEvaluator` | `tool_selection_accuracy_evaluator.py` | 07, 10 |
| `ToolParameterAccuracyEvaluator` | `tool_parameter_accuracy_evaluator.py` | 10 |
| `HelpfulnessEvaluator` | `helpfulness_evaluator.py` | 13 |
| `InteractionsEvaluator` | `interactions_evaluator.py` | 14 (pipeline-level) |
| `Contains`, `Equals`, `StartsWith`, `StateEquals`, `ToolCalled` | `evaluators/deterministic/` | Guardrails in every experiment |

The deterministic evaluators live at
[`evaluators/deterministic/`](https://github.com/OrpingtonClose/strands-evals/tree/main/src/strands_evals/evaluators/deterministic)
and are imported directly from `strands_evals.experiment` per
`experiment.py:21`.

---

## 3. Simulation layer

`strands_evals.simulation` provides:

- `ToolSimulator` — LLM-backed mock tools that share state via
  `StateRegistry`. One simulator can back multiple tools (e.g. a single
  "video_pipeline" simulator backs `dispatch_video_job`, `check_job_status`,
  `check_worker_health`). See
  [`simulation/tool_simulator.py`](https://github.com/OrpingtonClose/strands-evals/blob/main/src/strands_evals/simulation/tool_simulator.py).
- `ActorSimulator` — multi-turn user simulation with goal tracking, useful
  for the escalation supervisor (`ActorSimulator.from_case_for_user_simulator`,
  [`simulation/actor_simulator.py`](https://github.com/OrpingtonClose/strands-evals/blob/main/src/strands_evals/simulation/actor_simulator.py)).

See [`SIMULATION.md`](./SIMULATION.md) for the configurations we use.

---

## 4. Generation layer

`strands_evals.generators.ExperimentGenerator` auto-generates `Case`s from
a natural-language context description. We use this to bootstrap cases for
components where we don't yet have a golden set (e.g. coherence evaluator:
"generate 10 cases of `(visual_concepts, expected_rating)`").

Generated cases are **always** reviewed by a human and committed. Generation
is not a runtime dependency.

---

## 5. Providers / telemetry

- [`providers/langfuse_provider.py`](https://github.com/OrpingtonClose/strands-evals/blob/main/src/strands_evals/providers)
  fetches session traces from Langfuse for post-hoc evaluation of real
  production runs.
- `StrandsEvalsTelemetry` wires OTel into the eval run itself so experiment
  execution shows up in the same traces as the agent under test
  (`experiment.py:9, 26–27`).

Wire Langfuse to the Phoenix deployment the team already runs at
`phoenix.deep-search.uk` — the eval run traces should land next to
production traces.

---

## 6. Directory layout for per-component evals

```
server/strands_agents/
├── scenario_agent.py
├── scenario_refiner.py
├── audio_tool.py
├── ...
└── evals/
    ├── evaluators/                     # the 7 custom evaluators
    │   ├── scenario_quality_evaluator.py
    │   ├── audio_invariant_evaluator.py
    │   ├── visual_coherence_evaluator.py
    │   ├── timeline_compliance_evaluator.py
    │   ├── contract_compliance_evaluator.py
    │   ├── escalation_decision_evaluator.py
    │   └── critique_store_evaluator.py
    ├── experiments/                    # Experiment.to_file() output
    │   ├── scenario_experiment.json
    │   ├── timing_experiment.json
    │   └── ...
    └── simulators/
        ├── gpu_worker_simulator.py
        ├── tts_worker_simulator.py
        └── escalation_actor_simulator.py
```

All `Experiment` JSONs are loaded at test time via `Experiment.from_file`
so that humans can review + PR case-level changes without touching Python.
