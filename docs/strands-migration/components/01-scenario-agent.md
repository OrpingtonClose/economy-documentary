# 01 — scenario-agent

Canonical template. Every other component doc follows the same section
layout.

---

## Intent

Produce a `scenes: list[dict]` (and its companion `visual_style`,
`style_lock`) from `(topic, corpus_path, target_duration_sec)`. The agent
must loop internally (generate → evaluate → refine) until the generated
scenes pass both structural and LLM-evaluator gates, then return.

---

## Current implementation

[`server/agents/scenario_director.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/agents/scenario_director.py)
(608 lines). A `LoopAgent("scenario_director", max_iterations=3)` composed
of two agents:

- `scenario_generator` (Agent + `create_timeline_tool` + 6 callbacks). A
  ~170-line `_GENERATOR_INSTRUCTION` covering scene schema, `style_lock`,
  `hook_spec`, `outro_spec`, `pronunciation_hints`, SSML, duration
  targets, language modes.
- `scenario_evaluator` (Agent + `exit_loop` + structural checks from
  `server/tools/scenario_evaluator_checks.py` + 6 callbacks).

Problems it exposes:

- Prompt is so long the generator sometimes drops fields.
- Evaluator runs *after* the generator completes, so a structural failure
  costs a full generation before it's caught.
- The two agents communicate via the blackboard; no structured handoff.

---

## Strands implementation

Single file: `server/strands_agents/scenario_agent.py`. One `Agent` with
four tools. The LLM decides when to call `evaluate_scenario` and when to
call `refine_scenario`; there is no external loop.

### System prompt

≤ 1 000 tokens. Detailed schema and style-lock vocabulary lives in a
Strands skill / plugin loaded on demand — not inline in the prompt. Prompt
body covers:

- Role ("you are the Scenario Director…").
- The four-tool protocol: always start with `generate_scenario`; after
  each generation call `evaluate_scenario`; if `evaluate_scenario` returns
  `rating in {POOR, FAIR}` or any `issues`, call `refine_scenario` with the
  feedback; when `rating in {GOOD, EXCELLENT}` and no issues remain, call
  `create_timeline` and return.
- Hard constraints (from current `_GENERATOR_INSTRUCTION` lines 56–200):
  pick one `dominant_style`, emit `hook_spec` on scene 1, `outro_spec` on
  final scene, total duration within ±10% of target, no rhetorical
  questions, SSML `<break>` tags for pacing.

Implementation note: migrate the detailed schema into a
`strands.vended_plugins.skills.Skill` loaded via the agent's skill
registry. The skill fires when the LLM asks for the schema.

### Tools (4)

```python
@tool
def generate_scenario(
    topic: str, num_scenes: int, style: str, language: str,
) -> dict:
    """Produce an initial scenes list + visual_style + style_lock.

    Returns {"scenes": [...], "visual_style": {...}, "style_lock": {...}}.
    """

@tool
def evaluate_scenario(
    scenes: list[dict], style_lock: dict, target_duration_sec: float,
) -> dict:
    """Run structural + LLM evaluation.

    Calls run_all_structural_checks from scenario_evaluator_checks.py then
    a small LLM judge on anything the checks can't see.  Returns
    {"rating": "EXCELLENT|GOOD|FAIR|POOR", "issues": [...], "suggestions": [...]}.
    """

@tool
def refine_scenario(scenes: list[dict], feedback: dict) -> dict:
    """Adjust scenes based on evaluator feedback.

    Returns {"scenes": [...]} with the same cardinality; only field values change.
    """

@tool(context=True)
async def create_timeline(context, scenes: list[dict]) -> dict:
    """Produce an OTIO timeline from scenes, write to disk, stamp state.

    Returns {"timeline_path": str, "total_duration_sec": float}.  Writes
    scenes, visual_style, style_lock, timeline_path to invocation_state.
    """
```

### Hooks

```python
from .hooks.contracts import ContractEnforcer
from .hooks.revision_tagger import RevisionTagger

hooks = [
    ContractEnforcer(SCENARIO_CONTRACT),
    RevisionTagger(artifact_type="scenario"),
]
```

- `ContractEnforcer`: runs `validate_preconditions(SCENARIO_CONTRACT, …)`
  on `BeforeInvocationEvent`, `validate_postconditions(…)` on
  `AfterInvocationEvent`.
- `RevisionTagger`: ports
  [`server/callbacks/artifact_revision_tag.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/callbacks/artifact_revision_tag.py);
  stamps each produced artifact with the preference-ledger revision.

### Conversation manager

`SlidingWindowConversationManager(window_size=20)`. Twenty messages is
enough for 3-4 generate→evaluate→refine cycles without losing the
original topic description.

### Why a single agent (not a `LoopAgent`)

The generate/evaluate/refine cycle is an *internal reasoning step* for
the scenario director, not a pipeline stage. Hoisting it to a graph
makes the cycle count part of the graph spec, which is what the
anti-complexity-creep playbook tells us to avoid. If, after shipping,
evals show the LLM loops > 5 times on > 10% of cases, revisit.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    ContractComplianceEvaluator(SCENARIO_CONTRACT),   # deterministic, hard gate
    ScenarioQualityEvaluator(),                       # deterministic, hard gate
    TrajectoryEvaluator(),                            # deterministic, hard gate
    CoherenceEvaluator(),                             # LLM-as-judge
    FaithfulnessEvaluator(),                          # LLM-as-judge
]
```

### Test cases (minimum 5)

| Case name | Input | Expected trajectory | Expected rating |
|-----------|-------|--------------------|----|
| `economics_basics` | `"5-scene documentary about inflation"`, target 300 s | `[generate_scenario, evaluate_scenario, refine_scenario, evaluate_scenario, create_timeline]` | ≥ GOOD |
| `complex_monetary_policy` | `"10-scene deep dive on monetary policy transmission"`, target 600 s | generate→eval→refine→eval→create | ≥ GOOD; per-scene duration within ±15% |
| `edge_single_scene` | `"1-scene documentary about the gold standard"`, target 60 s | generate→eval→create | ≥ FAIR; tests boundary |
| `edge_max_scenes` | `"15-scene survey of 20th-century inflation"`, target 900 s | generate→eval→refine→eval→create | ≥ GOOD; no token-limit truncation |
| `failure_empty_topic` | `""`, target 300 s | generate fails structural check | test_pass=False; `ContractViolation` with `stage="scenario"` |

### Simulators

None. This agent is pure LLM — no external workers.

### Thresholds (from [`THRESHOLDS.md`](../eval-framework/THRESHOLDS.md))

- `ContractComplianceEvaluator` ≥ 1.00 (hard gate)
- `ScenarioQualityEvaluator` ≥ 0.70 (hard gate)
- `TrajectoryEvaluator` ≥ 0.80 (hard gate)
- `CoherenceEvaluator` ≥ 0.75 (soft)
- `FaithfulnessEvaluator` ≥ 0.70 (soft)

---

## File layout

```
server/strands_agents/
├── scenario_agent.py                       # ~250 LOC: Agent + 4 tools + hooks wiring
├── hooks/
│   ├── contracts.py                        # ContractEnforcer (shared)
│   └── revision_tagger.py                  # RevisionTagger (shared)
└── evals/
    ├── evaluators/
    │   ├── scenario_quality_evaluator.py
    │   └── contract_compliance_evaluator.py
    └── experiments/
        └── scenario_experiment.json        # Experiment.to_file output
```

---

## Acceptance criteria

- [ ] Agent runs standalone: `python -m server.strands_agents.scenario_agent "7 minute documentary about inflation"` produces `scenes.json`, `visual_style.json`, `style_lock.json`, `timeline.otio` in a tmpdir.
- [ ] All 4 `@tool` functions unit-tested under `tests/strands_agents/test_scenario_agent_tools.py`.
- [ ] `run_all_structural_checks` passes on the generated output for all 5 test cases (excluding `failure_empty_topic`).
- [ ] `Experiment.from_file("…/scenario_experiment.json")` run locally produces `test_pass=True` on all non-failure cases.
- [ ] CI (`strands-evals.yml`) green on a PR touching only `server/strands_agents/scenario_agent.py`.
- [ ] Phoenix trace shows the `generate → evaluate → refine → evaluate → create_timeline` tool sequence for `economics_basics`.
- [ ] `RevisionTagger` stamps the preference-ledger revision on `scenes.json` (visible in the trace `after_tool` attributes).
- [ ] `ContractEnforcer` raises `ContractViolation` on `failure_empty_topic` — test asserts this via `pytest.raises`.
- [ ] Component doc updated with the final paths, any deviations, and commit SHA of the landed PR.
