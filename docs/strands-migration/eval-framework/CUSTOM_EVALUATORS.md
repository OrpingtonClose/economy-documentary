# CUSTOM_EVALUATORS — the 7 evaluators we ship

These seven evaluators bridge the migration: each one wraps an existing
quality system in `server/` so the Strands rewrite inherits the lessons
from the ADK pipeline (audio invariants, scenario structural checks,
critique store, etc.).

All subclass `strands_evals.evaluators.evaluator.Evaluator[InputT, OutputT]`
and return `list[EvaluationOutput]`. File locations assume `server/strands_agents/evals/evaluators/`.

---

## 1. `ScenarioQualityEvaluator`

**Wraps:** `server/tools/scenario_evaluator_checks.py:run_all_structural_checks`.

**Why:** The PAG-run post-mortem showed the LLM evaluator happily rated
scenarios `GOOD` despite duration shortfall, rhetorical questions, missing
outro, and topic drift. These structural checks are deterministic and must
run before any LLM grading.

**Shape:**

```python
class ScenarioQualityEvaluator(Evaluator[str, dict]):
    """Runs all structural checks on the generated scenes list.

    Each CheckResult becomes one EvaluationOutput. Hard gate: any check with
    a POOR cap flips test_pass to False regardless of the aggregate score.
    """

    def evaluate(self, data: EvaluationData[str, dict]) -> list[EvaluationOutput]:
        scenes = (data.actual_output or {}).get("scenes") or []
        style_lock = (data.actual_output or {}).get("style_lock") or {}
        target = (data.metadata or {}).get("target_duration_sec", 420.0)
        from tools.scenario_evaluator_checks import run_all_structural_checks
        results = run_all_structural_checks(
            scenes=scenes,
            style_lock=style_lock,
            target_duration_sec=target,
        )
        outputs = []
        for r in results:
            score = 1.0 if r.passed else 0.0
            outputs.append(EvaluationOutput(
                evaluator=self.__class__.__name__,
                case_name=data.name,
                score=score,
                label=r.severity_cap,          # "POOR", "FAIR", "GOOD", "EXCELLENT"
                reason=r.message,
                test_pass=r.severity_cap != "POOR",
                metadata={"check": r.name},
            ))
        return outputs
```

**Scoring:** fraction of checks passing. **Hard gate:** any `POOR` cap → test fails.

---

## 2. `AudioInvariantEvaluator`

**Wraps:** `server/critique/audio_invariants.py` (seven invariant checks:
LUFS, peak limiter, clicks, plosives, voice continuity, character voice
consistency, hiss floor).

**Shape:**

```python
class AudioInvariantEvaluator(Evaluator[dict, dict]):
    """Runs all 7 audio invariants. Hard gate: all must pass."""

    def evaluate(self, data: EvaluationData[dict, dict]) -> list[EvaluationOutput]:
        wav_paths = (data.actual_output or {}).get("wav_paths") or []
        scenes = (data.actual_output or {}).get("scenes") or []
        from critique.audio_invariants import run_all_invariants
        results = run_all_invariants(wav_paths=wav_paths, scenes=scenes)
        outputs = []
        for r in results:
            outputs.append(EvaluationOutput(
                evaluator=self.__class__.__name__,
                case_name=data.name,
                score=1.0 if r.verdict == "PASS" else 0.0,
                label=r.verdict,
                reason=r.message,
                test_pass=r.verdict == "PASS",
                metadata={"invariant": r.name, "measurement": r.measurement},
            ))
        return outputs
```

**Hard gate:** every invariant must pass. This is the same bar the current
`stylistic_qa_agent` enforces — we port it wholesale.

---

## 3. `VisualCoherenceEvaluator`

**LLM-as-judge.** Rubric: assess visual concept coherence across scenes
(style consistency, camera variety, narrative-visual alignment). Rating
scale is the `CritiqueRating` vocabulary from
[`server/critique/record.py:55`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/critique/record.py#L55):
`EXCELLENT | GOOD | FAIR | POOR | UNKNOWN`.

**Shape:**

```python
class VisualCoherenceEvaluator(Evaluator[dict, dict]):
    """LLM-as-judge on visual concept coherence."""

    _score_mapping = {"EXCELLENT": 1.0, "GOOD": 0.75, "FAIR": 0.5, "POOR": 0.0, "UNKNOWN": 0.0}

    def __init__(self, judge_model: str | None = None):
        self._judge_model = judge_model or os.environ.get("STRANDS_SYNTHESIS_MODEL", "openai/gpt-4o")

    async def evaluate_async(self, data: EvaluationData[dict, dict]) -> list[EvaluationOutput]:
        concepts = (data.actual_output or {}).get("visual_concepts") or []
        style_lock = (data.actual_output or {}).get("style_lock") or {}
        prompt = _RUBRIC_PROMPT.format(concepts=json.dumps(concepts), style_lock=style_lock)
        rating, reason = await _judge(self._judge_model, prompt)  # returns ("GOOD", "camera variety low")
        return [EvaluationOutput(
            evaluator=self.__class__.__name__,
            case_name=data.name,
            score=self._score_mapping[rating],
            label=rating,
            reason=reason,
            test_pass=self._score_mapping[rating] >= 0.75,
        )]
```

---

## 4. `TimelineComplianceEvaluator`

**Deterministic.** Wraps `validate_otio_compliance` logic from
[`server/tools/validation_tools.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/tools/validation_tools.py).

**Checks:**

- Scene order matches `scenes[]`.
- No gaps between clips on the video track.
- Clip durations within `±2 s` of target scene duration.
- All clips assigned to the correct OTIO track (video vs. narration).

**Shape:**

```python
class TimelineComplianceEvaluator(Evaluator[str, dict]):
    """Deterministic OTIO timeline compliance."""

    def evaluate(self, data: EvaluationData[str, dict]) -> list[EvaluationOutput]:
        timeline_path = (data.actual_output or {}).get("timeline_path")
        from tools.validation_tools import validate_otio_compliance
        report = validate_otio_compliance(timeline_path)  # returns {"gaps": [...], "duration_errors": [...], "track_errors": [...]}
        checks = [
            ("no_gaps", not report["gaps"], f"{len(report['gaps'])} gap(s)"),
            ("duration_bounds", not report["duration_errors"], f"{len(report['duration_errors'])} off-target scene(s)"),
            ("track_assignment", not report["track_errors"], f"{len(report['track_errors'])} track mis-assignment(s)"),
            ("scene_order", report["order_ok"], "scenes out of order" if not report["order_ok"] else "order ok"),
        ]
        return [
            EvaluationOutput(
                evaluator=self.__class__.__name__,
                case_name=data.name,
                score=1.0 if ok else 0.0,
                label="PASS" if ok else "FAIL",
                reason=msg,
                test_pass=ok,
                metadata={"check": name},
            )
            for name, ok, msg in checks
        ]
```

**Hard gate:** all four checks must pass.

---

## 5. `ContractComplianceEvaluator`

**Deterministic.** Takes a `StageContract` (from
[`server/contracts.py:258`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/contracts.py#L258))
and a state dict. Mirrors `validate_preconditions` / `validate_postconditions`
but emits `EvaluationOutput` instead of raising.

**Shape:**

```python
class ContractComplianceEvaluator(Evaluator[dict, dict]):
    """Verify a StageContract's pre- and post-conditions."""

    def __init__(self, contract: StageContract):
        self._contract = contract

    def evaluate(self, data: EvaluationData[dict, dict]) -> list[EvaluationOutput]:
        state = (data.actual_output or {}).get("state") or {}
        checks = [
            ("required_state", _has_real_values(state, self._contract.required_state)),
            ("produced_state", _has_real_values(state, self._contract.produced_state)),
            ("produced_artifacts", _all_artifacts_exist(self._contract.produced_artifacts)),
            ("services_healthy", _all_services_healthy(self._contract.required_services)),
        ]
        return [
            EvaluationOutput(
                evaluator=self.__class__.__name__,
                case_name=data.name,
                score=1.0 if ok else 0.0,
                label="PASS" if ok else "FAIL",
                reason=f"{name}: {'ok' if ok else 'violation'}",
                test_pass=ok,
                metadata={"check": name, "contract": self._contract.name},
            )
            for name, ok in checks
        ]
```

Hard gate on every component whose contract is non-empty.

---

## 6. `EscalationDecisionEvaluator`

**LLM-as-judge.** Scores whether a recovery action (`fix`, `retry`, `skip`,
`escalate`, `abort`) was appropriate given the diagnostic context.

**Rubric:**

| Score | Meaning |
|-------|---------|
| 1.0 | Correct decision given the evidence |
| 0.5 | Reasonable but suboptimal (e.g. `retry` where `fix` would have been better) |
| 0.0 | Harmful (e.g. `retry` for a deterministic failure, or `abort` when a simple fix exists) |

**Shape:**

```python
class EscalationDecisionEvaluator(Evaluator[dict, dict]):
    """LLM-as-judge on recovery action correctness."""

    _score_mapping = {"CORRECT": 1.0, "REASONABLE": 0.5, "HARMFUL": 0.0}

    async def evaluate_async(self, data: EvaluationData[dict, dict]) -> list[EvaluationOutput]:
        diagnostic = (data.metadata or {}).get("diagnostic")
        decision = (data.actual_output or {}).get("action")
        prompt = _ESCALATION_RUBRIC.format(diagnostic=diagnostic, decision=decision)
        verdict, reason = await _judge(self._judge_model, prompt)
        return [EvaluationOutput(
            evaluator=self.__class__.__name__,
            case_name=data.name,
            score=self._score_mapping[verdict],
            label=verdict,
            reason=reason,
            test_pass=self._score_mapping[verdict] >= 0.5,
        )]
```

Used exclusively by components `10` (production supervisor) and `13`
(escalation supervisor).

---

## 7. `CritiqueStoreEvaluator`

**Bridge evaluator.** Reads `ArtifactCritiqueRecord` from
[`server/critique/store.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/critique/store.py)
(the existing QA verdict aggregator) and converts `worst_qa()` to an
`EvaluationOutput`.

Maps `QaVerdictStatus` ([`record.py:46`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/critique/record.py#L46)):

| `QaVerdictStatus` | score |
|-------------------|-------|
| `pass` | 1.0 |
| `warn` | 0.75 |
| `escalate` | 0.5 |
| `fail` | 0.0 |

**Shape:**

```python
class CritiqueStoreEvaluator(Evaluator[str, dict]):
    """Read ArtifactCritiqueRecord from the critique store and score it."""

    _score_mapping = {"pass": 1.0, "warn": 0.75, "escalate": 0.5, "fail": 0.0}

    def __init__(self, artifact_type: str):
        self._artifact_type = artifact_type

    def evaluate(self, data: EvaluationData[str, dict]) -> list[EvaluationOutput]:
        from critique.store import CritiqueStore
        store = CritiqueStore.current()
        artifact_id = (data.actual_output or {}).get("artifact_id")
        record = store.get(self._artifact_type, artifact_id)
        verdict = record.worst_qa() if record else "fail"
        return [EvaluationOutput(
            evaluator=self.__class__.__name__,
            case_name=data.name,
            score=self._score_mapping[verdict],
            label=verdict.upper(),
            reason=record.summary() if record else "no record",
            test_pass=verdict in ("pass", "warn"),
        )]
```

Usable by any component that emits an artifact the critique store tracks.

---

## Composition rule

Every component's evaluator stack starts with two deterministic
evaluators (`ContractComplianceEvaluator`, one of the structural
evaluators above) and **then** adds LLM-as-judge evaluators. This
prevents a harness from ever reporting `PASS` on output that violates a
hard contract.
