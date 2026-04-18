"""Mirror helpers: write existing evaluator outputs into the critique store.

PR-3 wiring-layer.  The pipeline already emits QA verdicts through
:mod:`tools.qa_jury`, :mod:`gatekeeper`, :mod:`callbacks.timeline_guardian`,
:mod:`tools.scenario_evaluator_checks` and the visual
``coherence_evaluator``.  This module provides **one-line mirror helpers**
that convert each native shape (via :mod:`critique.adapters`) and append
the resulting :class:`QaVerdict` to the artifact's
:class:`ArtifactCritiqueRecord` via :class:`ArtifactCritiqueStore`.

Design rules
------------

* **Never raise.**  Every helper swallows store / adapter errors and logs
  them at WARNING level.  A critique-store outage must not take down the
  live pipeline.
* **Zero dependency on ADK** so callers (callbacks, orchestrator,
  clip_helpers) can import without pulling the model stack.
* **Idempotent from the caller's POV.**  The store's ``append_qa`` is
  append-biased — multiple mirror calls for the same (artifact, check)
  append multiple verdicts rather than dedup'ing; callers that want
  dedup can pass ``replace_same_check=True`` and the helper will scrub
  prior verdicts with the same ``source`` + ``check_name`` before
  appending.
* **Store injection is optional.**  When ``store`` is ``None`` the helper
  resolves :func:`critique.store.get_critique_store`; tests pass an
  explicit store.

The adapters this module wraps live in :mod:`critique.adapters`; see that
module for the native-shape -> :class:`QaVerdict` translations.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from critique.adapters import (
    coherence_evaluator_to_qa,
    gatekeeper_to_qa,
    jury_to_qa,
    scenario_evaluator_to_qa,
    timeline_guardian_to_qa,
)
from critique.record import (
    ArtifactCritiqueRecord,
    ArtifactType,
    QaVerdict,
)
from critique.store import ArtifactCritiqueStore, get_critique_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _resolve_store(store: Optional[ArtifactCritiqueStore]) -> Optional[ArtifactCritiqueStore]:
    """Return the injected store, falling back to the module singleton.

    Failures to resolve the default store are logged and return ``None``
    so callers (which always invoke helpers defensively) silently skip
    mirroring without raising.
    """

    if store is not None:
        return store
    try:
        return get_critique_store()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("critique store unavailable, skipping mirror: %s", exc)
        return None


def _append_verdict_safely(
    store: ArtifactCritiqueStore,
    artifact_type: ArtifactType,
    artifact_id: str,
    verdict: QaVerdict,
    *,
    replace_same_check: bool,
    produced_by: str,
    iteration: Optional[int],
) -> Optional[ArtifactCritiqueRecord]:
    """Append ``verdict`` to the record, optionally scrubbing duplicates.

    When ``replace_same_check`` is True, any existing verdict with the
    same ``source`` + ``check_name`` is removed before the append so the
    record only carries the freshest outcome for that gate.
    """

    try:
        if replace_same_check:
            existing = store.read(artifact_type, artifact_id)
            if existing is not None:
                before = len(existing.qa_results)
                existing.qa_results = [
                    v
                    for v in existing.qa_results
                    if not (v.source == verdict.source and v.check_name == verdict.check_name)
                ]
                if len(existing.qa_results) != before:
                    store.write(existing)
        return store.append_qa(
            artifact_type,
            artifact_id,
            verdict,
            produced_by=produced_by,
            iteration=iteration,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "critique store: failed to mirror %s/%s %s/%s: %s",
            artifact_type,
            artifact_id,
            verdict.source,
            verdict.check_name,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Public mirror helpers
# ---------------------------------------------------------------------------

def mirror_jury_verdict(
    jury_verdict: Any,
    *,
    artifact_type: ArtifactType,
    artifact_id: str,
    store: Optional[ArtifactCritiqueStore] = None,
    check_name: str = "",
    source: str = "qa_jury",
    produced_by: str = "",
    iteration: Optional[int] = None,
    replace_same_check: bool = False,
) -> Optional[ArtifactCritiqueRecord]:
    """Mirror a :class:`tools.qa_jury.JuryVerdict` into the critique store."""

    resolved = _resolve_store(store)
    if resolved is None:
        return None
    try:
        verdict = jury_to_qa(jury_verdict, check_name=check_name, source=source)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("jury_to_qa adapter failed for %s/%s: %s", artifact_type, artifact_id, exc)
        return None
    return _append_verdict_safely(
        resolved,
        artifact_type,
        artifact_id,
        verdict,
        replace_same_check=replace_same_check,
        produced_by=produced_by,
        iteration=iteration,
    )


def mirror_gatekeeper_check(
    check: Any,
    *,
    artifact_type: ArtifactType,
    artifact_id: str,
    store: Optional[ArtifactCritiqueStore] = None,
    produced_by: str = "",
    iteration: Optional[int] = None,
    replace_same_check: bool = False,
) -> Optional[ArtifactCritiqueRecord]:
    """Mirror a single :class:`gatekeeper.GatekeeperCheck` into the store."""

    resolved = _resolve_store(store)
    if resolved is None:
        return None
    try:
        verdict = gatekeeper_to_qa(check)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "gatekeeper_to_qa adapter failed for %s/%s: %s", artifact_type, artifact_id, exc,
        )
        return None
    return _append_verdict_safely(
        resolved,
        artifact_type,
        artifact_id,
        verdict,
        replace_same_check=replace_same_check,
        produced_by=produced_by,
        iteration=iteration,
    )


def mirror_gatekeeper_checks(
    checks: Iterable[Any],
    *,
    artifact_type: ArtifactType,
    artifact_id: str,
    store: Optional[ArtifactCritiqueStore] = None,
    produced_by: str = "",
    iteration: Optional[int] = None,
    replace_same_check: bool = False,
) -> list[ArtifactCritiqueRecord]:
    """Mirror a batch of gatekeeper checks for the same artifact.

    Returns the subset of records actually persisted so callers can
    observe partial success without the helper raising.
    """

    out: list[ArtifactCritiqueRecord] = []
    for check in checks:
        rec = mirror_gatekeeper_check(
            check,
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            store=store,
            produced_by=produced_by,
            iteration=iteration,
            replace_same_check=replace_same_check,
        )
        if rec is not None:
            out.append(rec)
    return out


def mirror_timeline_guardian_result(
    check_name: str,
    passed: bool,
    *,
    artifact_type: ArtifactType,
    artifact_id: str,
    store: Optional[ArtifactCritiqueStore] = None,
    message: str = "",
    details: Optional[dict[str, Any]] = None,
    produced_by: str = "",
    iteration: Optional[int] = None,
    replace_same_check: bool = False,
) -> Optional[ArtifactCritiqueRecord]:
    """Mirror a :mod:`callbacks.timeline_guardian` result into the store."""

    resolved = _resolve_store(store)
    if resolved is None:
        return None
    try:
        verdict = timeline_guardian_to_qa(
            check_name,
            passed,
            message=message,
            details=details,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "timeline_guardian_to_qa adapter failed for %s/%s: %s",
            artifact_type,
            artifact_id,
            exc,
        )
        return None
    return _append_verdict_safely(
        resolved,
        artifact_type,
        artifact_id,
        verdict,
        replace_same_check=replace_same_check,
        produced_by=produced_by,
        iteration=iteration,
    )


def mirror_scenario_evaluator_result(
    rating: str,
    *,
    artifact_type: ArtifactType,
    artifact_id: str,
    store: Optional[ArtifactCritiqueStore] = None,
    structural_cap: str = "",
    report: str = "",
    check_name: str = "scenario_adhd",
    produced_by: str = "",
    iteration: Optional[int] = None,
    replace_same_check: bool = True,
) -> Optional[ArtifactCritiqueRecord]:
    """Mirror a scenario evaluator rating into the store.

    ``replace_same_check`` defaults to True because the scenario
    evaluator runs inside a LoopAgent and re-emits a fresh rating on
    every iteration; we only want the latest verdict in the record.
    Per-iteration history is still observable via the
    :class:`Critique` tail written by critic squads (PR-3).
    """

    resolved = _resolve_store(store)
    if resolved is None:
        return None
    try:
        verdict = scenario_evaluator_to_qa(
            rating,
            structural_cap=structural_cap,
            report=report,
            check_name=check_name,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "scenario_evaluator_to_qa adapter failed for %s/%s: %s",
            artifact_type,
            artifact_id,
            exc,
        )
        return None
    return _append_verdict_safely(
        resolved,
        artifact_type,
        artifact_id,
        verdict,
        replace_same_check=replace_same_check,
        produced_by=produced_by,
        iteration=iteration,
    )


def mirror_coherence_evaluator_result(
    rating: str,
    *,
    artifact_type: ArtifactType,
    artifact_id: str,
    store: Optional[ArtifactCritiqueStore] = None,
    rationale: str = "",
    check_name: str = "visual_coherence",
    produced_by: str = "",
    iteration: Optional[int] = None,
    replace_same_check: bool = True,
) -> Optional[ArtifactCritiqueRecord]:
    """Mirror a visual ``coherence_evaluator`` rating into the store.

    Same reasoning as :func:`mirror_scenario_evaluator_result` for the
    default ``replace_same_check=True``.
    """

    resolved = _resolve_store(store)
    if resolved is None:
        return None
    try:
        verdict = coherence_evaluator_to_qa(
            rating,
            rationale=rationale,
            check_name=check_name,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "coherence_evaluator_to_qa adapter failed for %s/%s: %s",
            artifact_type,
            artifact_id,
            exc,
        )
        return None
    return _append_verdict_safely(
        resolved,
        artifact_type,
        artifact_id,
        verdict,
        replace_same_check=replace_same_check,
        produced_by=produced_by,
        iteration=iteration,
    )


__all__ = [
    "mirror_coherence_evaluator_result",
    "mirror_gatekeeper_check",
    "mirror_gatekeeper_checks",
    "mirror_jury_verdict",
    "mirror_scenario_evaluator_result",
    "mirror_timeline_guardian_result",
]
