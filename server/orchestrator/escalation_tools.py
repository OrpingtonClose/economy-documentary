"""Read-tools for the pull-based escalation agent.

The pull-based escalation redesign flips the old "pack everything into an
``EscalationContext``" pattern on its head: the agent receives an
:class:`orchestrator.escalation_scope.EscalationScope` and a toolbox of
read-only lookup functions.  It decides which to call.

This module provides those lookup functions as **plain Python
callables** so the module itself stays dependency-free of ADK and
google-genai.  PR-2 wraps each one in a ``google.adk.tools.FunctionTool``
for the supervisor agent; for now they can be called directly from
tests, telemetry and ad-hoc diagnostics.

Each function returns a JSON-serialisable ``dict`` or ``list[dict]`` so
the return values slot straight into LLM tool-call responses.  All of
them swallow upstream errors and return empty structures on failure:
the escalation agent is allowed to proceed with partial information.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Optional

from critique.record import ARTIFACT_TYPES, ArtifactType
from critique.store import ArtifactCritiqueStore, get_critique_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Critique store reads
# ---------------------------------------------------------------------------

def _resolve_store(store: Optional[ArtifactCritiqueStore]) -> ArtifactCritiqueStore:
    return store if store is not None else get_critique_store()


def read_artifact_critique_history(
    artifact_id: str,
    artifact_type: Optional[str] = None,
    *,
    store: Optional[ArtifactCritiqueStore] = None,
) -> list[dict[str, Any]]:
    """Return all critique entries for ``artifact_id``.

    When ``artifact_type`` is given the store is queried directly.
    Otherwise the store is scanned across all artifact types — more
    expensive but lets the agent look up an artifact when only the ID is
    known.

    Returns a list of :class:`critique.record.Critique` dicts ordered
    by their ``timestamp`` field.
    """

    s = _resolve_store(store)
    records = _records_for(artifact_id, artifact_type, s)
    out: list[dict[str, Any]] = []
    for rec in records:
        for crit in rec.critiques:
            entry = crit.to_dict()
            entry["artifact_type"] = rec.artifact_type
            entry["artifact_id"] = rec.artifact_id
            entry["iteration"] = rec.iteration
            out.append(entry)
    out.sort(key=lambda e: e.get("timestamp", 0.0))
    return out


def read_qa_verdicts(
    artifact_id: str,
    artifact_type: Optional[str] = None,
    *,
    store: Optional[ArtifactCritiqueStore] = None,
) -> list[dict[str, Any]]:
    """Return all QA verdicts for ``artifact_id`` (sorted by timestamp)."""

    s = _resolve_store(store)
    records = _records_for(artifact_id, artifact_type, s)
    out: list[dict[str, Any]] = []
    for rec in records:
        for qa in rec.qa_results:
            entry = qa.to_dict()
            entry["artifact_type"] = rec.artifact_type
            entry["artifact_id"] = rec.artifact_id
            out.append(entry)
    out.sort(key=lambda e: e.get("timestamp", 0.0))
    return out


def read_escalation_history(
    artifact_id: Optional[str] = None,
    artifact_type: Optional[str] = None,
    *,
    store: Optional[ArtifactCritiqueStore] = None,
) -> list[dict[str, Any]]:
    """Return past escalations.

    If ``artifact_id`` is given, limit to that artifact; otherwise
    return every escalation in the store across every artifact.  The
    global view is capped at the 50 most recent entries so the tool
    response stays small for the LLM.
    """

    s = _resolve_store(store)
    if artifact_id is not None:
        records = _records_for(artifact_id, artifact_type, s)
    else:
        records = s.read_all()

    out: list[dict[str, Any]] = []
    for rec in records:
        for esc in rec.escalations:
            entry = esc.to_dict()
            entry["artifact_type"] = rec.artifact_type
            entry["artifact_id"] = rec.artifact_id
            out.append(entry)
    out.sort(key=lambda e: e.get("timestamp", 0.0))
    if artifact_id is None:
        out = out[-50:]
    return out


def read_artifact_record(
    artifact_id: str,
    artifact_type: Optional[str] = None,
    *,
    store: Optional[ArtifactCritiqueStore] = None,
) -> Optional[dict[str, Any]]:
    """Return the full :class:`ArtifactCritiqueRecord` as a dict, or None.

    Convenience for agents that would rather make one tool call than
    three.  When ``artifact_type`` is omitted the store is scanned and
    the first matching record is returned; if multiple records share the
    same id across types, prefer calling with an explicit type.
    """

    s = _resolve_store(store)
    records = _records_for(artifact_id, artifact_type, s)
    if not records:
        return None
    return records[0].to_dict()


def _records_for(
    artifact_id: str,
    artifact_type: Optional[str],
    store: ArtifactCritiqueStore,
) -> list:
    if artifact_type is not None:
        if artifact_type not in ARTIFACT_TYPES:
            logger.debug(
                "escalation_tools: unknown artifact_type %r; returning empty",
                artifact_type,
            )
            return []
        record = store.read(artifact_type, artifact_id)  # type: ignore[arg-type]
        return [record] if record is not None else []

    out = []
    for at in ARTIFACT_TYPES:
        rec = store.read(at, artifact_id)  # type: ignore[arg-type]
        if rec is not None:
            out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Infra-level reads (worker health, stage timing, full status snapshot)
# ---------------------------------------------------------------------------

# Callers (primarily tests) can inject a stand-in infra agent factory
# via :func:`set_infra_agent_factory`.  The default resolves the live
# singleton lazily so importing this module in a test environment
# without the full infra stack doesn't crash.
_InfraAgentFactory = Callable[[], Any]
_infra_agent_factory: Optional[_InfraAgentFactory] = None


def set_infra_agent_factory(factory: Optional[_InfraAgentFactory]) -> None:
    """Override the infra-agent resolver (primarily for tests)."""

    global _infra_agent_factory
    _infra_agent_factory = factory


def _resolve_infra_agent() -> Optional[Any]:
    if _infra_agent_factory is not None:
        try:
            return _infra_agent_factory()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("escalation_tools: infra factory failed: %s", exc)
            return None
    try:
        from infra_agent import get_infra_agent  # type: ignore

        return get_infra_agent()
    except Exception as exc:
        logger.debug("escalation_tools: no infra agent available: %s", exc)
        return None


def read_infra_status_snapshot() -> dict[str, Any]:
    """Return the infra-agent status snapshot.

    Shape matches :meth:`infra_agent.InfraAgent.get_status`: pause flag,
    per-worker health, current stage timing, recent escalations.  Empty
    dict when the infra agent is not running (e.g. tests).
    """

    agent = _resolve_infra_agent()
    if agent is None:
        return {}
    try:
        status = agent.get_status()
        if isinstance(status, dict):
            return status
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("escalation_tools: get_status failed: %s", exc)
    return {}


def read_worker_health(role: Optional[str] = None) -> list[dict[str, Any]]:
    """Return per-worker health entries, optionally filtered by role.

    ``role`` is matched case-insensitively against :class:`infra_agent.WorkerRole`
    values (``"tts"`` / ``"video"``).
    """

    snapshot = read_infra_status_snapshot()
    workers = snapshot.get("workers") or []
    if role is None:
        return list(workers)
    needle = role.strip().lower()
    return [w for w in workers if str(w.get("role", "")).lower() == needle]


def read_stage_timing() -> dict[str, Any]:
    """Return the current stage timing entry, or an empty dict."""

    snapshot = read_infra_status_snapshot()
    stage = snapshot.get("current_stage")
    if isinstance(stage, dict):
        return stage
    return {}


def read_infra_escalation_log(limit: int = 20) -> list[dict[str, Any]]:
    """Return the infra-agent's recent escalation events (worker/pipeline level).

    Distinct from :func:`read_escalation_history`, which returns
    supervisor actions taken on artifacts.  The infra log surfaces
    worker flaps, stage timeouts, pipeline pauses.
    """

    snapshot = read_infra_status_snapshot()
    events = snapshot.get("recent_escalations") or []
    if not isinstance(events, list):
        return []
    if limit is None or limit <= 0:
        return list(events)
    return list(events[-limit:])


# ---------------------------------------------------------------------------
# Cost / provisioner reads
# ---------------------------------------------------------------------------

_ProvisionerFactory = Callable[[], Any]
_provisioner_factory: Optional[_ProvisionerFactory] = None


def set_provisioner_factory(factory: Optional[_ProvisionerFactory]) -> None:
    """Override the provisioner resolver (primarily for tests)."""

    global _provisioner_factory
    _provisioner_factory = factory


def _resolve_provisioner() -> Optional[Any]:
    if _provisioner_factory is not None:
        try:
            return _provisioner_factory()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("escalation_tools: provisioner factory failed: %s", exc)
            return None
    try:
        from worker_provisioner import get_provisioner  # type: ignore

        return get_provisioner()
    except Exception as exc:
        logger.debug("escalation_tools: no provisioner available: %s", exc)
        return None


def read_vast_cost_snapshot() -> dict[str, Any]:
    """Return a best-effort cost snapshot from the worker provisioner.

    The provisioner is polled via duck-typed attribute access so this
    module stays independent of the provisioner's exact surface.  All
    reported fields are optional — callers must handle absent keys.

    Keys returned when available:

    * ``per_hour_usd``     -- aggregate hourly burn across live specs.
    * ``instances``        -- number of currently-tracked specs.
    * ``spec_breakdown``   -- ``[{role, max_price}, ...]`` per spec.
    * ``budget_remaining`` -- from ``VAST_BUDGET_REMAINING`` env if set.
    * ``collected_at``     -- wall-clock time of the snapshot.
    """

    out: dict[str, Any] = {"collected_at": time.time()}
    prov = _resolve_provisioner()
    specs: list[Any] = []
    if prov is not None:
        specs = list(getattr(prov, "_specs", []) or [])

    per_hour = 0.0
    breakdown: list[dict[str, Any]] = []
    for spec in specs:
        role = str(getattr(spec, "role", "") or "")
        max_price = getattr(spec, "max_price", None)
        try:
            price = float(max_price) if max_price is not None else 0.0
        except (TypeError, ValueError):
            price = 0.0
        per_hour += price
        breakdown.append({"role": role, "max_price": price})

    if specs:
        out["per_hour_usd"] = round(per_hour, 4)
        out["instances"] = len(specs)
        out["spec_breakdown"] = breakdown

    budget_env = os.environ.get("VAST_BUDGET_REMAINING", "").strip()
    if budget_env:
        try:
            out["budget_remaining"] = float(budget_env)
        except ValueError:
            pass

    return out


# ---------------------------------------------------------------------------
# Timeline state reads
# ---------------------------------------------------------------------------

def read_timeline_state(
    *,
    state_getter: Optional[Callable[[], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Return a compact summary of the current pipeline timeline state.

    PR-1 keeps this intentionally minimal: the caller (pipeline or test)
    passes a ``state_getter`` returning the ADK session state dict, and
    this function returns a trimmed view safe to put in an LLM prompt
    (just scene count, per-scene durations, overall gaps) rather than
    the raw OTIO timeline.  When no getter is supplied the function
    falls back to reading the B2 checkpoint if available.
    """

    raw: dict[str, Any] = {}
    if state_getter is not None:
        try:
            raw = dict(state_getter() or {})
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("escalation_tools: state_getter failed: %s", exc)
            raw = {}

    if not raw:
        raw = _read_pipeline_state_from_b2()

    scenes = raw.get("scenes")
    if not isinstance(scenes, list):
        scenes = []
    summary = {
        "scene_count": len(scenes),
        "user_prompt": raw.get("user_prompt", ""),
        "has_visual_concepts": bool(raw.get("visual_concepts")),
        "has_assembly": bool(raw.get("assembly")),
    }

    per_scene: list[dict[str, Any]] = []
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        per_scene.append({
            "scene_num": scene.get("scene_num"),
            "duration_sec": scene.get("duration_sec"),
            "narration_duration_sec": scene.get("narration_duration_sec"),
            "status": scene.get("status", ""),
        })
    summary["scenes"] = per_scene
    return summary


def _read_pipeline_state_from_b2() -> dict[str, Any]:
    """Best-effort read of the B2 pipeline_state.json checkpoint."""

    try:
        from tools import b2_checkpoint as _b2  # type: ignore
    except Exception:
        return {}

    downloader = getattr(_b2, "download_json", None)
    if downloader is None:
        return {}
    try:
        data = downloader("state/pipeline_state.json")
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("escalation_tools: B2 pipeline_state fetch failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Public tool registry
# ---------------------------------------------------------------------------

#: Ordered list of ``(tool_name, callable)`` pairs — the canonical set of
#: read-tools exposed to the escalation agent.  PR-2 turns each pair into
#: a ``google.adk.tools.FunctionTool``.
ESCALATION_READ_TOOLS: tuple[tuple[str, Callable[..., Any]], ...] = (
    ("read_artifact_critique_history", read_artifact_critique_history),
    ("read_qa_verdicts", read_qa_verdicts),
    ("read_escalation_history", read_escalation_history),
    ("read_artifact_record", read_artifact_record),
    ("read_worker_health", read_worker_health),
    ("read_stage_timing", read_stage_timing),
    ("read_infra_status_snapshot", read_infra_status_snapshot),
    ("read_infra_escalation_log", read_infra_escalation_log),
    ("read_vast_cost_snapshot", read_vast_cost_snapshot),
    ("read_timeline_state", read_timeline_state),
)


__all__ = [
    "ESCALATION_READ_TOOLS",
    "read_artifact_critique_history",
    "read_artifact_record",
    "read_escalation_history",
    "read_infra_escalation_log",
    "read_infra_status_snapshot",
    "read_qa_verdicts",
    "read_stage_timing",
    "read_timeline_state",
    "read_vast_cost_snapshot",
    "read_worker_health",
    "set_infra_agent_factory",
    "set_provisioner_factory",
]
