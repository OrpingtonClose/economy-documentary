> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Phase 3 Implementation Plan: Intermediate Preview Assemblies & Critique Substrate

**Status:** Draft — ready for review
**Scope:** Diagrams 6 (critique substrate) and 9 (preview assemblies) from `ARCHITECTURE_DIAGRAMS.md`
**Depends on:** Phase 1 (stateless OTIO file protocol), Phase 2 (gates, lifecycle, ladders)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Existing Infrastructure Audit](#2-existing-infrastructure-audit)
3. [Work Package A: Critique Substrate Wiring (Diagram 6)](#3-work-package-a-critique-substrate-wiring-diagram-6)
4. [Work Package B: Preview Assembly Pipeline (Diagram 9)](#4-work-package-b-preview-assembly-pipeline-diagram-9)
5. [Work Package C: Cross-Cutting Integration](#5-work-package-c-cross-cutting-integration)
6. [Implementation Order & Dependency Graph](#6-implementation-order--dependency-graph)
7. [Testing Strategy](#7-testing-strategy)
8. [Risk Register](#8-risk-register)

---

## 1. Executive Summary

Phase 3 closes the loop between **artifact production** and **quality-driven escalation** by:

1. **Wiring the critique substrate** — connecting every existing critic (QA jury, gatekeeper, timeline guardian, scenario evaluator, coherence evaluator) to the `ArtifactCritiqueStore` so the Escalation Supervisor can read verdicts without polling individual agents.
2. **Activating the preview assembly pipeline** — the builder (`previews/builder.py`) and consumers (`previews/consumers.py`) already exist; Phase 3 wires them into the graph pipeline at the four fixed coherence boundaries (pre-production, scene complete, act complete, halfway milestone) and ensures the critique store records preview findings.
3. **Connecting previews to critique** — each preview assembly's findings are written to the critique store as `QaVerdict` entries, making the preview cycle observable to the Escalation Supervisor.

**Key invariant:** Neither previews nor critique writes advance the pipeline. Advancement remains gated by explicit human gates (diagram 7) and the OTIO state machine.

---

## 2. Existing Infrastructure Audit

### 2.1 Already Implemented (no changes needed)

| Module | Status | Key types |
|--------|--------|-----------|
| `critique/record.py` | **Complete** | `ArtifactCritiqueRecord`, `Critique`, `QaVerdict`, `EscalationRef`, `ArtifactType`, `QaVerdictStatus` |
| `critique/store.py` | **Complete** | `ArtifactCritiqueStore` with `append_critique`, `append_qa`, `append_escalation`, `read`, `list_ids`, `read_all`, B2 mirror |
| `critique/adapters.py` | **Complete** | `jury_to_qa`, `gatekeeper_to_qa`, `timeline_guardian_to_qa`, `scenario_evaluator_to_qa`, `coherence_evaluator_to_qa`, `critic_payload_to_critique` |
| `previews/builder.py` | **Complete** | `build_preview`, `plan_preview`, `PreviewManifest`, `SlotPlan`, `SlotKind`, `SlotStatus`, `PreviewInconsistencyError`, `PreviewRenderError` |
| `previews/consumers.py` | **Complete** | `evaluate_preview`, `emit_preview_ready`, `emit_preview_failed`, `handle_human_dislike_preview`, `derive_boundary` |
| `callbacks/preview_triggers.py` | **Complete** | Four trigger predicates (`pre_production`, `scene_complete`, `act_complete`, `halfway_milestone`), idempotent ledger, calls `build_preview()` |
| `callbacks/otio_state.py` | **Complete** | OTIO state machine (draft ↔ authoritative), mutation guard, escalation window |
| `callbacks/approval_gate.py` | **Complete** | Human-in-the-loop checkpoints, poll loop, SSE emission |
| `callbacks/consistency_gate.py` | **Complete** | Composition of existing callbacks with ARCH-B2 checks |
| `callbacks/consistency_checker.py` | **Complete** | ARCH-A5 drift detection, `LedgerDrift` signals |
| `callbacks/strict_assembler.py` | **Complete** | ARCH-F3 Media Immutability enforcement |
| `strands_agents/evals/evaluators/critique_store.py` | **Complete** | `CritiqueStoreEvaluator` bridging store → Strands `EvaluationOutput` |

### 2.2 Needs New Code (this phase)

| Module | Gap | Work Required |
|--------|-----|---------------|
| `critique/` | No **write-side wiring** from existing critics to the store | New: `critique/writers.py` — fire-and-forget write helpers called from each critic's callback |
| `critique/` | No **read-side tools** for the Escalation Supervisor | New: `critique/reader.py` — read-only tools the supervisor calls |
| `critique/` | No **preview-as-artifact** support in `ArtifactType` | Extend: add `"preview"` to the `ArtifactType` literal |
| `callbacks/` | Preview triggers exist but aren't wired into the Strands graph | New: `callbacks/preview_gate.py` — graph node that fires triggers at coherence boundaries |
| `previews/` | Preview findings aren't written to the critique store | Extend: `previews/consumers.py` — write `QaVerdict` entries for each finding |
| `strands_agents/` | Graph pipeline doesn't include preview nodes | Extend: `graph_pipeline.py` — add preview trigger nodes after stage completions |
| `agents/` | No Escalation Supervisor agent | New: `agents/escalation_supervisor.py` — reads critique store, picks canonical actions |

### 2.3 Key Design Decisions (preserved from existing code)

1. **Fire-and-forget critique writes** — the store never blocks the main flow; failures are logged, not raised (matches `store.py`'s B2 mirror pattern).
2. **Append-only accumulation** — `ArtifactCritiqueRecord` accretes `critiques`, `qa_results`, `escalations` without replacing (matches `store.py`'s `append_*` helpers).
3. **Previews are QA artifacts, not deliverables** — `PREVIEW_ARTIFACT_KIND = "preview_assembly"` is distinct from any deliverable kind; downstream gates key on deliverable kinds and MUST NOT key on this (matches `builder.py` invariant #1).
4. **PreviewInconsistencyError is raised; PreviewRenderError is swallowed** — structural inconsistency in the OTIO is a blocking invariant violation; render failure is a QA artifact, not a pipeline blocker (matches `preview_triggers.py` pattern).
5. **Idempotent triggers** — each milestone fires once per run via the preview ledger (matches `preview_triggers.py`'s `PREVIEW_LEDGER_KEY` pattern).

---

## 3. Work Package A: Critique Substrate Wiring (Diagram 6)

### 3.1 A1: Extend `ArtifactType` to include `"preview"`

**File:** `critique/record.py`

**Change:** Add `"preview"` to the `ArtifactType` literal:

```python
ArtifactType = Literal[
    "scenario",
    "scene",
    "visual_concept",
    "clip",
    "audio",
    "assembly",
    "preview",         # NEW — intermediate preview assemblies
]
```

**Rationale:** Preview assemblies are first-class artifacts that critics score. The Escalation Supervisor needs to query them by type. Without this, preview findings would need a separate, untyped `metadata` key — defeating the store's type-indexed `list_ids` and `read_all` queries.

**Migration:** `ARTIFACT_TYPES` is derived from `get_args(ArtifactType)`, so the new value propagates automatically. No existing records are affected (append-only).

### 3.2 A2: Fire-and-forget write helpers — `critique/writers.py`

**New file:** `critique/writers.py`

This module provides thin wrappers that each existing critic calls at the end of its evaluation. The wrappers convert the critic's native output to a `QaVerdict` or `Critique` using the existing adapters, then call `store.append_qa()` or `store.append_critique()`. The write is wrapped in a `try/except` that logs but never raises — matching the B2 mirror pattern.

```python
"""Fire-and-forget helpers that write critic verdicts to the critique store.

Each helper is a plain function (no @tool decorator) so it can be called
from both Strands callbacks and ADK tools without pulling in the Strands
SDK.  The store write is best-effort: failures are logged but never raised,
matching the B2 mirror pattern in ``critique.store``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from critique.adapters import (
    coherence_evaluator_to_qa,
    gatekeeper_to_qa,
    jury_to_qa,
    scenario_evaluator_to_qa,
    timeline_guardian_to_qa,
)
from critique.record import ArtifactType, Critique, QaVerdict
from critique.store import get_critique_store

logger = logging.getLogger(__name__)


def write_qa_verdict(
    artifact_type: ArtifactType,
    artifact_id: str,
    verdict: QaVerdict,
    *,
    produced_by: str = "",
    iteration: Optional[int] = None,
) -> bool:
    """Append a QA verdict to the critique store. Returns True on success.

    Never raises — store write failures are logged and return False.
    """
    try:
        store = get_critique_store()
        store.append_qa(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            verdict=verdict,
            produced_by=produced_by,
            iteration=iteration,
        )
        return True
    except Exception:
        logger.exception(
            "critique writers: failed to write QA verdict for %s/%s",
            artifact_type, artifact_id,
        )
        return False


def write_critique(
    artifact_type: ArtifactType,
    artifact_id: str,
    critique: Critique,
    *,
    produced_by: str = "",
    iteration: Optional[int] = None,
) -> bool:
    """Append a Critique to the critique store. Returns True on success.

    Never raises — store write failures are logged and return False.
    """
    try:
        store = get_critique_store()
        store.append_critique(
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            critique=critique,
            produced_by=produced_by,
            iteration=iteration,
        )
        return True
    except Exception:
        logger.exception(
            "critique writers: failed to write critique for %s/%s",
            artifact_type, artifact_id,
        )
        return False


# --- Convenience wrappers for each critic system ---

def write_jury_verdict(
    artifact_type: ArtifactType,
    artifact_id: str,
    jury_verdict: Any,
    *,
    produced_by: str = "",
    iteration: Optional[int] = None,
) -> bool:
    """Convert a JuryVerdict and write it to the store."""
    qa = jury_to_qa(jury_verdict)
    return write_qa_verdict(
        artifact_type, artifact_id, qa,
        produced_by=produced_by, iteration=iteration,
    )


def write_gatekeeper_verdict(
    artifact_type: ArtifactType,
    artifact_id: str,
    check: Any,
    *,
    produced_by: str = "",
    iteration: Optional[int] = None,
) -> bool:
    """Convert a GatekeeperCheck and write it to the store."""
    qa = gatekeeper_to_qa(check)
    return write_qa_verdict(
        artifact_type, artifact_id, qa,
        produced_by=produced_by, iteration=iteration,
    )


def write_timeline_guardian_verdict(
    artifact_type: ArtifactType,
    artifact_id: str,
    check_name: str,
    passed: bool,
    *,
    message: str = "",
    details: Optional[dict[str, Any]] = None,
    produced_by: str = "",
    iteration: Optional[int] = None,
) -> bool:
    """Convert a timeline guardian result and write it to the store."""
    qa = timeline_guardian_to_qa(check_name, passed, message=message, details=details)
    return write_qa_verdict(
        artifact_type, artifact_id, qa,
        produced_by=produced_by, iteration=iteration,
    )


def write_scenario_evaluator_verdict(
    artifact_type: ArtifactType,
    artifact_id: str,
    rating: str,
    *,
    structural_cap: str = "",
    report: str = "",
    check_name: str = "scenario_adhd",
    produced_by: str = "",
    iteration: Optional[int] = None,
) -> bool:
    """Convert a scenario evaluator rating and write it to the store."""
    qa = scenario_evaluator_to_qa(
        rating, structural_cap=structural_cap, report=report,
        check_name=check_name,
    )
    return write_qa_verdict(
        artifact_type, artifact_id, qa,
        produced_by=produced_by, iteration=iteration,
    )


def write_coherence_evaluator_verdict(
    artifact_type: ArtifactType,
    artifact_id: str,
    rating: str,
    *,
    rationale: str = "",
    check_name: str = "visual_coherence",
    produced_by: str = "",
    iteration: Optional[int] = None,
) -> bool:
    """Convert a coherence evaluator rating and write it to the store."""
    qa = coherence_evaluator_to_qa(
        rating, rationale=rationale, check_name=check_name,
    )
    return write_qa_verdict(
        artifact_type, artifact_id, qa,
        produced_by=produced_by, iteration=iteration,
    )


def write_preview_findings(
    preview_hash: str,
    findings: list[dict[str, Any]],
    *,
    trigger_reason: str = "",
    produced_by: str = "preview_critic",
) -> bool:
    """Write preview evaluation findings as a QA verdict to the store.

    The preview is stored as artifact_type="preview", artifact_id=<input_hash>.
    The worst finding severity determines the verdict:
      - any "critical" → verdict="fail"
      - any "warning" only → verdict="warn"
      - no findings → verdict="pass"
    """
    if not findings:
        verdict = QaVerdict(
            source="preview_critic",
            check_name="preview_findings",
            verdict="pass",
            confidence=1.0,
            message="no findings",
            details={"trigger_reason": trigger_reason},
            timestamp=time.time(),
        )
    else:
        severities = {f.get("severity", "warning") for f in findings}
        if "critical" in severities:
            overall = "fail"
        elif "warning" in severities:
            overall = "warn"
        else:
            overall = "pass"
        verdict = QaVerdict(
            source="preview_critic",
            check_name="preview_findings",
            verdict=overall,
            confidence=1.0,
            message=f"{len(findings)} finding(s) at {trigger_reason}",
            details={
                "trigger_reason": trigger_reason,
                "findings": findings,
            },
            timestamp=time.time(),
        )
    return write_qa_verdict(
        artifact_type="preview",
        artifact_id=preview_hash,
        verdict=verdict,
        produced_by=produced_by,
    )
```

**Design rationale:**

- Each helper is a thin function, not a class — keeps the module importable from any callback without DI setup.
- The `write_*` helpers return `bool` so callers can log whether the write succeeded, but they never raise.
- The `write_preview_findings` helper maps the `evaluate_preview` findings dict to a single `QaVerdict` with the worst severity as the overall verdict, matching the `CritiqueStoreEvaluator`'s `_VERDICT_SCORES` mapping.

### 3.3 A3: Read-only tools for the Escalation Supervisor — `critique/reader.py`

**New file:** `critique/reader.py`

The Escalation Supervisor reads the store via these tools. They are read-only — no `append_*` calls, no mutations. The supervisor picks canonical `EscalationAction`s based on what it reads.

```python
"""Read-only tools for the Escalation Supervisor (diagram 6).

These tools surface critique store data to the supervisor agent without
exposing write helpers.  The supervisor reads verdicts and picks actions;
it never writes to the store directly (escalation actions are recorded
by the action executor, not the supervisor).

All tools are plain callables so they can be wrapped as Strands @tool
or ADK FunctionTool depending on the agent framework.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from critique.record import (
    ARTIFACT_TYPES,
    ArtifactCritiqueRecord,
    ArtifactType,
    QaVerdictStatus,
    worst_status,
)
from critique.store import ArtifactCritiqueStore, get_critique_store

logger = logging.getLogger(__name__)


def read_artifact_critique(
    artifact_type: ArtifactType,
    artifact_id: str,
) -> Optional[dict[str, Any]]:
    """Return the critique record for an artifact, or None.

    Returns the record's ``to_dict()`` so the supervisor sees a plain
    dict (no dataclass dependency).  Returns None when the artifact
    has no record in the store.
    """
    store = get_critique_store()
    record = store.read(artifact_type, artifact_id)
    if record is None:
        return None
    return record.to_dict()


def list_artifacts_by_type(
    artifact_type: Optional[ArtifactType] = None,
) -> list[tuple[str, str]]:
    """Return ``(artifact_type, artifact_id)`` pairs on disk.

    When ``artifact_type`` is given, only that type is scanned.
    Otherwise all types are returned.
    """
    store = get_critique_store()
    return store.list_ids(artifact_type)


def read_worst_verdicts(
    artifact_type: Optional[ArtifactType] = None,
) -> list[dict[str, Any]]:
    """Return the worst QA verdict for each artifact of a given type.

    Each entry is::

        {
            "artifact_type": str,
            "artifact_id": str,
            "worst_verdict": "pass" | "warn" | "escalate" | "fail",
            "iteration": int,
            "qa_count": int,
        }

    Sorted by worst verdict first (fail > escalate > warn > pass).
    """
    store = get_critique_store()
    records = store.read_all(artifact_type)
    entries: list[dict[str, Any]] = []
    for rec in records:
        worst = rec.worst_qa()
        entries.append({
            "artifact_type": rec.artifact_type,
            "artifact_id": rec.artifact_id,
            "worst_verdict": worst,
            "iteration": rec.iteration,
            "qa_count": len(rec.qa_results),
        })
    # Sort: fail first, escalate, warn, pass last
    severity = {"fail": 0, "escalate": 1, "warn": 2, "pass": 3}
    entries.sort(key=lambda e: severity.get(e["worst_verdict"], 4))
    return entries


def read_failed_artifacts(
    artifact_type: Optional[ArtifactType] = None,
) -> list[dict[str, Any]]:
    """Return only artifacts whose worst QA verdict is ``fail`` or ``escalate``.

    Convenience wrapper for the supervisor's most common query.
    """
    all_entries = read_worst_verdicts(artifact_type)
    return [
        e for e in all_entries
        if e["worst_verdict"] in ("fail", "escalate")
    ]


def read_artifact_critique_history(
    artifact_type: ArtifactType,
    artifact_id: str,
) -> dict[str, Any]:
    """Return a summary of the full critique history for an artifact.

    Includes iteration count, worst verdict, all critique summaries,
    and escalation history.  Designed for the supervisor's context
    window — compact but complete.
    """
    store = get_critique_store()
    record = store.read(artifact_type, artifact_id)
    if record is None:
        return {
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "found": False,
        }
    return {
        "artifact_type": record.artifact_type,
        "artifact_id": record.artifact_id,
        "found": True,
        "iteration": record.iteration,
        "worst_verdict": record.worst_qa(),
        "produced_by": record.produced_by,
        "critique_count": len(record.critiques),
        "critique_summaries": [
            {"source": c.source, "rating": c.rating, "summary": c.summary}
            for c in record.critiques
        ],
        "qa_verdicts": [
            {
                "source": q.source,
                "check_name": q.check_name,
                "verdict": q.verdict,
                "message": q.message,
            }
            for q in record.qa_results
        ],
        "escalation_history": [
            {
                "scope_id": e.scope_id,
                "action": e.action,
                "outcome": e.outcome,
                "reasoning": e.reasoning,
            }
            for e in record.escalations
        ],
    }
```

### 3.4 A4: Wire existing critics to the store

**Strategy:** Each critic's callback already produces a structured output. We add a single `write_*` call at the end of each callback, after the existing logic completes. The write is fire-and-forget (never raises, never blocks).

**Files to modify:**

| File | Insert after | Write helper |
|------|-------------|-------------|
| `callbacks/timeline_guardian.py` | Each `_validate_*` method | `write_timeline_guardian_verdict` |
| `callbacks/strict_assembler.py` | After `UnpluggedGapError` / `ClipLengthMismatchError` detection | `write_timeline_guardian_verdict` (these are structural checks) |
| `tools/qa_jury.py` (or wherever jury verdicts are emitted) | After jury deliberation | `write_jury_verdict` |
| `tools/gatekeeper.py` (or wherever gatekeeper checks run) | After each check | `write_gatekeeper_verdict` |
| `tools/scenario_evaluator_checks.py` | After rating is computed | `write_scenario_evaluator_verdict` |
| `tools/coherence_evaluator.py` | After rating is computed | `write_coherence_evaluator_verdict` |

**Pattern for each insertion:**

```python
# After existing validation logic:
from critique.writers import write_timeline_guardian_verdict

# ... existing code produces `error` (str or None) ...
write_timeline_guardian_verdict(
    artifact_type="clip",            # or "scene", "assembly", etc.
    artifact_id=f"s{scene_num:03d}_p{phrase_idx:03d}",
    check_name="_validate_production",
    passed=(error is None),
    message=error or "passed",
    details={"stage": "production"},
)
```

**Important:** The `artifact_type` and `artifact_id` for each write must match the convention used elsewhere. The mapping is:

| Critic | artifact_type | artifact_id pattern |
|--------|--------------|---------------------|
| Timeline Guardian (scenario) | `"scenario"` | `"scenario"` |
| Timeline Guardian (audio) | `"audio"` | `"scene_NNN"` |
| Timeline Guardian (visual) | `"clip"` | `"sNNN_pNNN"` |
| Timeline Guardian (production) | `"clip"` | `"sNNN_pNNN"` |
| Timeline Guardian (assembly) | `"assembly"` | `"full_assembly"` |
| Timeline Guardian (scene assembly) | `"assembly"` | `"scene_NNN_assembly"` |
| QA Jury | `"clip"` | `"sNNN_pNNN"` |
| Gatekeeper | `"clip"` | `"sNNN_pNNN"` |
| Scenario Evaluator | `"scenario"` | `"scenario"` |
| Coherence Evaluator | `"scene"` | `"scene_NNN"` |
| Preview Critic | `"preview"` | `<input_hash>` |

### 3.5 A5: Escalation Supervisor agent — `agents/escalation_supervisor.py`

**New file:** `agents/escalation_supervisor.py`

The supervisor is a pull-based agent that reads the critique store and picks canonical `EscalationAction`s. It does NOT write to the store — it reads verdicts and delegates to the existing escalation infrastructure (`recovery.submit_escalation`, `orchestrator.escalation_menu`).

```python
"""Escalation Supervisor (diagram 6) — reads critique store, picks actions.

The supervisor is a pull-based agent: it reads the store via the read-only
tools in ``critique.reader`` and picks canonical EscalationActions from
the existing ``orchestrator.escalation_menu``.  It does NOT write to the
store — escalation actions are recorded by the action executor.

Invocation:
    - Scheduled: runs after each stage boundary (wired into the graph
      pipeline as a post-stage callback).
    - On-demand: the dashboard can trigger a supervisor run when a
      human flags a concern.

The supervisor's output is a list of recommended EscalationActions,
each tagged with the artifact_type and artifact_id it targets.  The
orchestrator decides whether to execute them.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from critique.reader import (
    read_artifact_critique_history,
    read_failed_artifacts,
    read_worst_verdicts,
)

logger = logging.getLogger(__name__)


def run_supervisor(
    *,
    artifact_type: Optional[str] = None,
    state: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Run the escalation supervisor and return recommended actions.

    Reads the critique store for failed/escalated artifacts, examines
    their history, and recommends canonical escalation actions.

    Returns a list of action dicts::

        [{
            "artifact_type": str,
            "artifact_id": str,
            "recommended_action": str,
            "reasoning": str,
            "severity": "critical" | "warning",
        }]
    """
    failed = read_failed_artifacts(artifact_type)
    if not failed:
        logger.info("escalation supervisor: no failed artifacts")
        return []

    actions: list[dict[str, Any]] = []
    for entry in failed:
        atype = entry["artifact_type"]
        aid = entry["artifact_id"]
        verdict = entry["worst_verdict"]

        history = read_artifact_critique_history(atype, aid)
        if not history.get("found"):
            continue

        # Determine recommended action based on history
        action = _pick_action(atype, aid, verdict, history)
        if action:
            actions.append(action)

    logger.info(
        "escalation supervisor: %d failed artifacts, %d actions recommended",
        len(failed), len(actions),
    )
    return actions


def _pick_action(
    artifact_type: str,
    artifact_id: str,
    worst_verdict: str,
    history: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Pick a canonical escalation action for an artifact.

    Decision logic:
    1. If the artifact has an existing escalation with outcome="success",
       skip it (already resolved).
    2. If the artifact has an existing escalation with outcome="failure",
       recommend the next-higher action (climb the ladder).
    3. If no prior escalation, recommend the first action for the
       artifact type.
    """
    escalation_history = history.get("escalation_history", [])
    if not escalation_history:
        # No prior escalation — recommend the first action
        return _first_action(artifact_type, artifact_id, worst_verdict, history)

    # Check if any escalation succeeded
    for esc in escalation_history:
        if esc.get("outcome") == "success":
            logger.info(
                "supervisor: %s/%s already resolved (scope %s)",
                artifact_type, artifact_id, esc.get("scope_id"),
            )
            return None

    # All prior escalations failed or are unknown — climb the ladder
    last_action = escalation_history[-1].get("action", "unknown")
    next_action = _next_ladder_rung(artifact_type, last_action)
    return {
        "artifact_type": artifact_type,
        "artifact_id": artifact_id,
        "recommended_action": next_action,
        "reasoning": (
            f"Prior escalation '{last_action}' did not resolve. "
            f"Worst verdict: {worst_verdict}. "
            f"Recommending next rung: {next_action}."
        ),
        "severity": "critical" if worst_verdict == "fail" else "warning",
    }


def _first_action(
    artifact_type: str,
    artifact_id: str,
    worst_verdict: str,
    history: dict[str, Any],
) -> dict[str, Any]:
    """Return the first escalation action for an artifact type."""
    # Map artifact types to their first escalation action
    _FIRST_ACTIONS = {
        "scenario": "scenario_rewrite",
        "scene": "scene_replan",
        "visual_concept": "concept_regenerate",
        "clip": "clip_retry",
        "audio": "audio_retry",
        "assembly": "assembly_retry",
        "preview": "preview_escalation",
    }
    action = _FIRST_ACTIONS.get(artifact_type, "generic_retry")
    return {
        "artifact_type": artifact_type,
        "artifact_id": artifact_id,
        "recommended_action": action,
        "reasoning": (
            f"First escalation for {artifact_type}/{artifact_id}. "
            f"Worst verdict: {worst_verdict}. "
            f"QA count: {history.get('qa_count', 0)}."
        ),
        "severity": "critical" if worst_verdict == "fail" else "warning",
    }


def _next_ladder_rung(artifact_type: str, last_action: str) -> str:
    """Return the next escalation action after ``last_action``.

    This is a simplified ladder — the full ladder is in
    ``orchestrator.escalation_menu``.  This helper provides a reasonable
    default for the supervisor's recommendation.
    """
    _LADDER = [
        "clip_retry", "concept_regenerate", "scene_replan",
        "scenario_rewrite", "human_escalation",
    ]
    try:
        idx = _LADDER.index(last_action)
        return _LADDER[min(idx + 1, len(_LADDER) - 1)]
    except ValueError:
        return "human_escalation"
```

---

## 4. Work Package B: Preview Assembly Pipeline (Diagram 9)

### 4.1 B1: Wire preview triggers into the Strands graph

**File:** `strands_agents/graph_pipeline.py`

**Change:** Add preview trigger nodes after each stage completion. The existing `preview_triggers.py` already defines the four trigger predicates and the `build_preview()` call. We need to wire these as graph nodes.

**New graph nodes:**

```
[Scenario complete] → preview_pre_production node
[Audio complete]    → preview_pre_production node (if not already fired)
[Scene N complete]  → preview_scene_complete node
[Act N complete]    → preview_act_complete node
[50% clips]         → preview_halfway node
```

**Implementation pattern:**

```python
# In graph_pipeline.py, after the existing stage nodes:

from callbacks.preview_triggers import (
    pre_production_preview_after_agent_callback,
    scene_complete_preview_after_agent_callback,
    act_complete_preview_after_agent_callback,
    halfway_preview_after_agent_callback,
    preview_triggers_after_agent_callback,
)

# Add preview trigger nodes as post-stage callbacks
# These are fire-and-forget: PreviewRenderError is swallowed,
# only PreviewInconsistencyError is raised.

def _build_preview_node(callback_fn):
    """Create a graph node that fires a preview callback.

    The callback_fn is one of the existing after_agent_callback functions
    from ``callbacks.preview_triggers`` (e.g.
    ``pre_production_preview_after_agent_callback``).  These already
    handle the idempotent ledger, builder invocation, and error
    classification.
    """
    def node(state):
        # The existing callbacks expect a callback_context with a .state
        # attribute.  We create a minimal shim.
        class _Ctx:
            def __init__(self, s):
                self.state = s
        try:
            callback_fn(_Ctx(state))
        except Exception:
            # The existing callbacks already handle
            # PreviewRenderError (swallowed) and
            # PreviewInconsistencyError (raised).  This outer catch
            # is a defensive belt-and-suspenders.
            logger.exception("preview node failed unexpectedly")
    return node

# Wire into the graph:
# The unified callback already dispatches to all four trigger types:
preview_node = _build_preview_node(preview_triggers_after_agent_callback)
```

**Key invariant:** Preview nodes are **non-blocking** for the pipeline. A `PreviewRenderError` means the preview mp4 couldn't be rendered (missing ffmpeg, font, etc.) — this is a QA artifact failure, not a pipeline failure. The pipeline continues. Only `PreviewInconsistencyError` (structural OTIO inconsistency) is raised.

### 4.2 B2: Wire preview findings to the critique store

**File:** `previews/consumers.py`

**Change:** After `evaluate_preview` derives findings, write them to the critique store using `write_preview_findings`.

**Insertion point:** In `evaluate_preview()`, after `_derive_findings(plans)` returns:

```python
# After findings are derived:
from critique.writers import write_preview_findings

write_preview_findings(
    preview_hash=data.get("input_hash", ""),
    findings=findings,
    trigger_reason=data.get("trigger_reason", ""),
    produced_by="preview_critic",
)
```

This makes preview findings visible to the Escalation Supervisor via `read_failed_artifacts(artifact_type="preview")`.

### 4.3 B3: Preview gate callback — `callbacks/preview_gate.py`

**New file:** `callbacks/preview_gate.py`

This callback wraps the preview trigger + consumer emission into a single composable callback that can be wired into the graph pipeline's after-agent hooks (matching the `consistency_gate.py` pattern).

```python
"""ARCH-G2 — Preview gate callback (diagram 9).

Composable callback that fires a preview build at coherence boundaries
and routes findings to both the critique store and the dashboard.

Follows the composition pattern from ``callbacks.consistency_gate``:
wraps existing callbacks without replacing them, and can be composed
onto any agent tree via the factory helpers.

Invariants:
1. Preview gates never advance the pipeline.
2. PreviewRenderError is swallowed (QA artifact, not deliverable).
3. PreviewInconsistencyError IS raised (structural OTIO violation).
4. Each milestone fires once per run (idempotent via preview ledger).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)


def make_preview_gate_callback(
    after_agent_callback: Callable,
    trigger_name: str,
) -> Callable:
    """Factory: return a callback that fires a preview trigger + consumer.

    Wraps one of the existing after_agent_callback functions from
    ``callbacks.preview_triggers`` (e.g.
    ``pre_production_preview_after_agent_callback``) with additional
    critique-store writing and dashboard emission.

    Usage::

        after_scenario = make_preview_gate_callback(
            pre_production_preview_after_agent_callback, "pre_production",
        )
        # Wire into the graph pipeline's after-agent hook for the
        # scenario agent node.
    """
    def callback(state: dict[str, Any]) -> None:
        from previews.builder import PreviewInconsistencyError, PreviewRenderError
        from previews.consumers import emit_preview_failed, emit_preview_ready

        # Create a minimal callback_context shim (the existing callbacks
        # expect an object with a .state attribute).
        class _Ctx:
            def __init__(self, s):
                self.state = s

        try:
            after_agent_callback(_Ctx(state))
        except PreviewInconsistencyError:
            # Structural inconsistency — must surface
            raise
        except PreviewRenderError as exc:
            logger.warning(
                "preview gate: render failed for %s: %s",
                trigger_name, exc,
            )
            emit_preview_failed(trigger_name, str(exc))
        except Exception as exc:
            logger.exception(
                "preview gate: unexpected failure for %s", trigger_name,
            )
            emit_preview_failed(trigger_name, str(exc))

    return callback
```

### 4.4 B4: Scene-complete and act-complete detection

**File:** `callbacks/preview_triggers.py` (already exists)

**Verification needed:** The existing `scene_complete_predicates` and `act_complete_predicates` functions must correctly detect when a scene/act is complete. The scene-complete detection uses `_scenes_completed()` which checks terminal slot status; the act-complete detection groups contiguous scenes via `_scenes_to_acts()`.

**No changes needed** — the existing predicates are correct. The wiring into the graph pipeline (B1) is the missing piece.

### 4.5 B5: Halfway milestone detection

**File:** `callbacks/preview_triggers.py` (already exists)

**Verification needed:** The `halfway_milestone_trigger` must fire when 50% of clips are produced. It uses the preview ledger to ensure it fires only once.

**No changes needed** — the existing predicate is correct.

---

## 5. Work Package C: Cross-Cutting Integration

### 5.1 C1: Wire critique writes into existing callbacks

This is the "last mile" of A4 — actually inserting the `write_*` calls into each callback. The pattern is:

```python
# At the end of each validation method, after the existing logic:

# --- Critique store write (fire-and-forget) ---
try:
    from critique.writers import write_timeline_guardian_verdict
    write_timeline_guardian_verdict(
        artifact_type="clip",
        artifact_id=f"s{scene_num:03d}_p{phrase_idx:03d}",
        check_name="_validate_production",
        passed=(error is None),
        message=error or "passed",
        details={"stage": "production"},
    )
except Exception:
    pass  # Never block the pipeline for a critique write
```

**The `try/except` wrapper is redundant** because `write_timeline_guardian_verdict` already never raises. But we add it as a defensive belt-and-suspenders pattern, matching the existing pattern in `consumers.py` where `emit_agui_event` is wrapped in `try/except`.

### 5.2 C2: Wire the Escalation Supervisor into the graph pipeline

**File:** `strands_agents/graph_pipeline.py`

**Change:** After each stage boundary (where the approval gate runs), add an optional supervisor run. The supervisor is non-blocking — its recommendations are logged and queued, not executed synchronously.

```python
# After each gate node:
def _supervisor_node(state):
    """Run the escalation supervisor and queue recommended actions."""
    from agents.escalation_supervisor import run_supervisor
    actions = run_supervisor(state=state)
    if actions:
        state.setdefault("_supervisor_actions", []).extend(actions)
        logger.info("supervisor queued %d actions", len(actions))
```

### 5.3 C3: Wire preview findings into the supervisor's read path

The supervisor already reads `read_failed_artifacts(artifact_type="preview")` via the `read_failed_artifacts` tool. Since B2 writes preview findings to the store with `artifact_type="preview"`, the supervisor will automatically see them.

**No additional wiring needed.**

### 5.4 C4: Dashboard integration

The dashboard already subscribes to `dashboard_events` (SSE stream). The existing `emit_preview_ready` and `emit_preview_failed` events are already emitted by the preview consumers. The new `preview_gate.py` callback calls these same functions.

**No additional dashboard changes needed.**

### 5.5 C5: Critique store evaluator integration

The existing `CritiqueStoreEvaluator` in `strands_agents/evals/evaluators/critique_store.py` already reads from the store and maps verdicts to `EvaluationOutput`. Adding `"preview"` to `ArtifactType` means the evaluator can now evaluate preview artifacts too.

**No changes needed** — the evaluator works with any `ArtifactType`.

---

## 6. Implementation Order & Dependency Graph

```
A1 (extend ArtifactType)
 │
 ├── A2 (writers.py) ──────────────────────┐
 │                                         │
 ├── A3 (reader.py) ──────────────────────┤
 │                                         │
 ├── B2 (consumers → store) ───────────────┤  (depends on A1 + A2)
 │                                         │
 ├── B3 (preview_gate.py) ────────────────┤  (depends on existing triggers)
 │                                         │
 ├── A4 (wire critics to store) ──────────┤  (depends on A2)
 │                                         │
 ├── A5 (escalation_supervisor.py) ───────┤  (depends on A3)
 │                                         │
 ├── B1 (graph pipeline wiring) ──────────┤  (depends on B3)
 │                                         │
 ├── C1 (wire critique writes) ───────────┤  (depends on A4)
 │                                         │
 ├── C2 (supervisor in graph) ────────────┤  (depends on A5 + B1)
 │                                         │
 └─────────────────────────────────────────┘

Recommended PR split:
  PR-1: A1 + A2 + A3 (critique infrastructure — no pipeline changes)
  PR-2: B2 + B3 (preview → critique wiring)
  PR-3: A4 + C1 (wire existing critics to store)
  PR-4: A5 + C2 (escalation supervisor + graph wiring)
  PR-5: B1 (graph pipeline preview nodes — depends on B3 from PR-2)
```

---

## 7. Testing Strategy

### 7.1 Unit Tests

| Module | Test file | Key tests |
|--------|-----------|-----------|
| `critique/writers.py` | `tests/test_critique_writers.py` | Each `write_*` helper returns `True` on success, `False` on store failure; `write_preview_findings` maps severities correctly; never raises |
| `critique/reader.py` | `tests/test_critique_reader.py` | `read_worst_verdicts` sorts correctly; `read_failed_artifacts` filters; `read_artifact_critique_history` returns compact summary |
| `agents/escalation_supervisor.py` | `tests/test_escalation_supervisor.py` | `run_supervisor` returns empty list when no failures; picks first action for new failures; climbs ladder for repeated failures |
| `callbacks/preview_gate.py` | `tests/test_preview_gate.py` | `make_preview_gate_callback` swallows `PreviewRenderError`; raises `PreviewInconsistencyError`; calls `emit_preview_ready` on success |

### 7.2 Integration Tests

| Scenario | What it tests |
|----------|--------------|
| Full pipeline run with critique store | Every critic writes to the store; supervisor reads correct verdicts |
| Preview at each trigger point | Pre-production, scene complete, act complete, halfway all fire; findings appear in store |
| Preview failure path | `PreviewRenderError` → dashboard gets `preview_failed` event; pipeline continues |
| Preview inconsistency path | `PreviewInconsistencyError` → pipeline halts; store has no entry for this preview |
| Supervisor ladder climbing | Same artifact fails twice → supervisor recommends next rung |

### 7.3 Property-Based Invariants

1. **Critique writes never raise** — for any input, `write_*` returns `True` or `False`, never raises.
2. **Preview gates never advance pipeline** — after a preview gate runs, the blackboard keys that gate stage transitions are unchanged.
3. **Store is append-only** — no `write_*` call replaces an existing record; all use `append_*`.
4. **Supervisor is read-only** — `run_supervisor` never calls `store.append_*` or `store.write`.

---

## 8. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Critique write slows pipeline | Low | Medium | Fire-and-forget pattern; writes are async-capable; benchmark with 1000+ artifacts |
| Store disk I/O bottleneck | Low | High | B2 mirror is best-effort; disk writes use atomic rename; per-file lock is RLock (reentrant) |
| Preview render at scale (many scenes) | Medium | Low | Previews are 512x320 @ veryfast; idempotent (same hash → same file); cheap re-run |
| ArtifactType extension breaks existing stores | Very Low | Low | `ArtifactType` is a `Literal`; existing records don't reference `"preview"`; `ARTIFACT_TYPES` auto-updates |
| Supervisor recommends wrong action | Medium | Medium | Supervisor is advisory — orchestrator decides; recommendations are logged for audit |
| Preview triggers fire twice | Low | High | Idempotent ledger in `preview_triggers.py` prevents double-firing; test with concurrent callbacks |
| Race between critic write and supervisor read | Low | Low | Store uses per-file lock; supervisor reads stale-but-valid data; worst case: supervisor misses the latest verdict and picks it up on next run |

---

## Appendix A: File Manifest

### New files

| File | Lines (est.) | Purpose |
|------|-------------|---------|
| `critique/writers.py` | ~150 | Fire-and-forget write helpers |
| `critique/reader.py` | ~130 | Read-only tools for supervisor |
| `agents/escalation_supervisor.py` | ~120 | Pull-based supervisor agent |
| `callbacks/preview_gate.py` | ~60 | Composable preview gate callback |
| `tests/test_critique_writers.py` | ~100 | Writer unit tests |
| `tests/test_critique_reader.py` | ~80 | Reader unit tests |
| `tests/test_escalation_supervisor.py` | ~80 | Supervisor unit tests |
| `tests/test_preview_gate.py` | ~60 | Preview gate unit tests |

### Modified files

| File | Change | Lines (est.) |
|------|--------|-------------|
| `critique/record.py` | Add `"preview"` to `ArtifactType` | +1 |
| `previews/consumers.py` | Add `write_preview_findings` call in `evaluate_preview` | +8 |
| `strands_agents/graph_pipeline.py` | Add preview trigger nodes + supervisor node | +40 |
| `callbacks/timeline_guardian.py` | Add `write_timeline_guardian_verdict` calls | +30 |
| `callbacks/strict_assembler.py` | Add `write_timeline_guardian_verdict` calls | +15 |
| `tools/qa_jury.py` | Add `write_jury_verdict` call | +8 |
| `tools/gatekeeper.py` | Add `write_gatekeeper_verdict` call | +8 |
| `tools/scenario_evaluator_checks.py` | Add `write_scenario_evaluator_verdict` call | +8 |
| `tools/coherence_evaluator.py` | Add `write_coherence_evaluator_verdict` call | +8 |

---

## Appendix B: Key Identifiers Reference

| Identifier | Module | Purpose |
|-----------|--------|---------|
| `PREVIEW_LEDGER_KEY` | `preview_triggers.py` | Idempotent ledger for trigger dedup |
| `LATEST_PREVIEW_KEY` | `builder.py` | Blackboard key for latest preview path |
| `PREVIEW_HISTORY_KEY` | `builder.py` | Blackboard key for preview history |
| `PREVIEW_ARTIFACT_KIND` | `builder.py` | `"preview_assembly"` — QA artifact tag |
| `PREVIEW_SLOT_OVERRIDES_KEY` | `builder.py` | Per-slot status overrides |
| `STATE_KEY` | `otio_state.py` | OTIO state machine key |
| `ESCALATION_KEY` | `otio_state.py` | Escalation window key |
| `STAGE_DERIVATIONS_KEY` | `consistency_checker.py` | Stage derivation tracking |
| `LEDGER_DRIFT_SIGNALS_KEY` | `consistency_checker.py` | Drift signal accumulator |
| `ArtifactType` | `record.py` | Literal union of artifact type strings |
| `QaVerdictStatus` | `record.py` | Literal `"pass" | "warn" | "escalate" | "fail"` |
| `CritiqueRating` | `record.py` | Literal `"EXCELLENT" | "GOOD" | "FAIR" | "POOR" | "UNKNOWN"` |
| `PREVIEW_READY_EVENT` | `consumers.py` | SSE event kind for dashboard |
| `PREVIEW_FAILED_EVENT` | `consumers.py` | SSE event kind for failed renders |
| `AGENT_ESCALATION_OP` | `consumers.py` | `"preview_critique"` — escalation op name |
| `HUMAN_DISLIKE_ESCALATION_OP` | `consumers.py` | `"preview_human_dislike"` — escalation op name |
