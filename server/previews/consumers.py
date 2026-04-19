"""ARCH-G3 — Preview consumers (diagram 9 + 10).

Two consumer lanes for preview assemblies:

1. **Agent lane** — :func:`evaluate_preview` is a plain callable a
   coherence critic agent can call as a :mod:`google.adk` tool.  It
   reads the latest preview manifest, derives structured findings
   (missing-slot counts, failed-slot reasons, in-progress rungs,
   honest-placeholder ratios), and routes any concerns to the
   **content ladder** via :func:`recovery.submit_escalation`.  The
   lane **never** mutates an OTIO artifact directly — escalation is
   the single sanctioned path.

2. **Human lane** — :func:`emit_preview_ready` pushes an SSE event on
   the ``dashboard_events`` channel so workstream-H dashboards can
   render a playable preview alongside a short reasoning digest.
   Dashboard-side dislike events (``human_dislike_preview``) are
   consumed by :func:`handle_human_dislike_preview` which escalates
   to **proactive L4** via the same :func:`submit_escalation` path.

Both lanes honour the ARCH-G1 invariants: previews are QA artifacts,
not deliverables; no lane advances the pipeline, clears artifact
tags, or mutates the OTIO timeline.

ADK idioms (meta #122):

- The optional :class:`~agents.preview_critic.PreviewCriticAgent`
  subclass wraps :func:`evaluate_preview` as a tool.  Instantiation
  is deferred so tests can exercise the callable without pulling in
  a live model.
- Dashboard SSE emission uses :func:`agui.emit_agui_event`, the
  existing workstream-H channel.
- Escalation submissions use :func:`recovery.submit_escalation` so
  existing dashboards (AG-UI) render them unchanged.

Spec references:

- Issue #155 (ARCH-G3 Consumers)
- Parent #129, meta #122
- ``docs/ARCHITECTURE_DIAGRAMS.md`` diagrams 9 + 10
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import time
from typing import Any, Mapping, Optional

from previews.builder import (
    LATEST_PREVIEW_KEY,
    PREVIEW_ARTIFACT_KIND,
    PREVIEW_HISTORY_KEY,
    PreviewManifest,
    SlotKind,
    SlotPlan,
    SlotStatus,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants / dashboard contract
# ---------------------------------------------------------------------------

#: SSE event kind emitted to the dashboard each time a preview is built.
PREVIEW_READY_EVENT = "preview_ready"

#: SSE event kind emitted to the dashboard when a preview render fails
#: (UI-06a, issue #208).  ``preview_failed`` is the parallel of
#: ``preview_ready`` for failure paths — never silently degrade: every
#: failed render must surface on the dashboard so the user sees it.
PREVIEW_FAILED_EVENT = "preview_failed"

#: Dashboard → backend event kind when a human rejects a preview.
HUMAN_DISLIKE_EVENT = "human_dislike_preview"

#: Escalation ``operation_name`` for agent-lane findings.  The
#: content-ladder router keys on this prefix; keep it stable.
AGENT_ESCALATION_OP = "preview_critique"

#: Escalation ``operation_name`` for human-lane dislike events.
HUMAN_DISLIKE_ESCALATION_OP = "preview_human_dislike"

#: Escalation level tag included in ``diagnosis`` so dashboards can
#: distinguish agent (content-ladder) vs human (proactive L4) lanes.
ESCALATION_LEVEL_L4 = "L4"
ESCALATION_LEVEL_CONTENT = "content_ladder"


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------


def _load_manifest(preview_path: str) -> dict:
    """Load the ``<preview>.manifest.json`` sidecar.

    The manifest is the source of truth for agent evaluation — the
    preview builder guarantees it is present and matches the on-disk
    preview bytes.  Callers who only have the mp4 path (as
    dashboards often do) use this helper to locate its sidecar.
    """
    candidate = preview_path
    if candidate.endswith(".mp4"):
        candidate = candidate[: -len(".mp4")] + ".manifest.json"
    if not os.path.exists(candidate):
        raise FileNotFoundError(
            f"preview manifest not found alongside {preview_path!r}"
        )
    with open(candidate) as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"preview manifest at {candidate!r} is not a JSON object")
    if data.get("kind") != PREVIEW_ARTIFACT_KIND:
        raise ValueError(
            f"manifest at {candidate!r} has kind={data.get('kind')!r}, "
            f"expected {PREVIEW_ARTIFACT_KIND!r}"
        )
    return data


def _slot_plans_from_manifest(data: Mapping[str, Any]) -> list[SlotPlan]:
    raw = data.get("slots") or []
    plans: list[SlotPlan] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        try:
            plans.append(
                SlotPlan(
                    track=str(entry.get("track") or ""),
                    kind=SlotKind(entry.get("kind") or SlotKind.VIDEO.value),
                    index=int(entry.get("index") or 0),
                    slot_key=str(entry.get("slot_key") or ""),
                    status=SlotStatus(entry.get("status") or SlotStatus.MISSING.value),
                    duration_sec=float(entry.get("duration_sec") or 0.0),
                    media_path=entry.get("media_path") or None,
                    scene_num=entry.get("scene_num"),
                    scripted_text=entry.get("scripted_text"),
                    eta_text=entry.get("eta_text"),
                    rung_text=entry.get("rung_text"),
                    failure_reason=entry.get("failure_reason"),
                )
            )
        except (TypeError, ValueError):
            logger.warning("preview consumer: malformed slot entry %r", entry)
    return plans


# ---------------------------------------------------------------------------
# Reasoning digest — a short human-readable summary
# ---------------------------------------------------------------------------


def _reasoning_digest(data: Mapping[str, Any], plans: list[SlotPlan]) -> str:
    """Compose a short digest for the dashboard / escalation.

    Deterministic over the manifest contents — no LLM.
    """
    counts = data.get("counts") or {}
    total = len(plans)
    delivered = int(counts.get(SlotStatus.DELIVERED.value, 0))
    missing = int(counts.get(SlotStatus.MISSING.value, 0))
    failed = int(counts.get(SlotStatus.FAILED.value, 0))
    in_progress = int(counts.get(SlotStatus.IN_PROGRESS.value, 0))
    total_dur = float(data.get("total_duration_sec") or 0.0)
    trigger = str(data.get("trigger_reason") or "unknown")

    lines = [
        f"trigger={trigger}",
        f"slots={total} (delivered={delivered}, missing={missing}, "
        f"failed={failed}, in_progress={in_progress})",
        f"runtime={total_dur:.2f}s",
    ]
    fails = [p for p in plans if p.status == SlotStatus.FAILED]
    if fails:
        fail_summary = ", ".join(
            f"{p.slot_key}({p.failure_reason or 'no reason'})"
            for p in fails[:3]
        )
        lines.append(f"failed_slots: {fail_summary}")
    return " | ".join(lines)


# ---------------------------------------------------------------------------
# Agent lane — evaluate_preview
# ---------------------------------------------------------------------------


def evaluate_preview(
    state: Mapping[str, Any],
    preview_path: Optional[str] = None,
) -> dict:
    """Evaluate the most recent preview and produce structured findings.

    This is the **agent lane** entry point: a plain Python callable
    suitable for attaching to an ADK ``Agent.tools`` list.  It is
    deterministic (no LLM calls) — the LLM is expected to ingest the
    returned dict, not the mp4 itself, so findings are reproducible.

    Args:
        state: Read-only blackboard snapshot.  ``preview_path``
            defaults to ``state[LATEST_PREVIEW_KEY]`` when omitted.
        preview_path: Explicit path to the preview mp4 (tests).

    Returns:
        A dict with::

            {
                "preview_path": str,
                "manifest_path": str,
                "input_hash": str,
                "trigger_reason": str,
                "digest": str,
                "findings": [
                    {"slot_key": str, "kind": str, "status": str,
                     "severity": "warning" | "critical",
                     "reason": str}
                ],
                "escalated": bool,
                "escalation_id": Optional[str],
                "escalation_level": "content_ladder" | None,
            }

    Side effects: when the findings list is non-empty, the function
    submits **one** aggregated escalation request via
    :func:`recovery.submit_escalation` tagged as ``content_ladder``
    level.  It does **not** mutate OTIO, the approval gate, or any
    artifact.
    """
    path = preview_path or state.get(LATEST_PREVIEW_KEY)
    if not path:
        raise ValueError(
            "evaluate_preview: no preview_path provided and "
            f"{LATEST_PREVIEW_KEY!r} not set on state"
        )
    data = _load_manifest(str(path))
    plans = _slot_plans_from_manifest(data)
    digest = _reasoning_digest(data, plans)
    findings = _derive_findings(plans)

    out = {
        "preview_path": data.get("preview_path") or str(path),
        "manifest_path": data.get("manifest_path") or "",
        "input_hash": data.get("input_hash") or "",
        "trigger_reason": data.get("trigger_reason") or "",
        "digest": digest,
        "findings": findings,
        "escalated": False,
        "escalation_id": None,
        "escalation_level": None,
    }

    if findings:
        escalation_id = _submit_agent_lane_escalation(data, digest, findings)
        if escalation_id is not None:
            out["escalated"] = True
            out["escalation_id"] = escalation_id
            out["escalation_level"] = ESCALATION_LEVEL_CONTENT

    return out


def _derive_findings(plans: list[SlotPlan]) -> list[dict]:
    """Classify slots into actionable findings for the content ladder."""
    findings: list[dict] = []
    for p in plans:
        if p.status == SlotStatus.FAILED:
            findings.append({
                "slot_key": p.slot_key,
                "kind": p.kind.value,
                "status": p.status.value,
                "severity": "critical",
                "reason": p.failure_reason or "failed (no reason recorded)",
            })
        elif p.status == SlotStatus.IN_PROGRESS:
            findings.append({
                "slot_key": p.slot_key,
                "kind": p.kind.value,
                "status": p.status.value,
                "severity": "warning",
                "reason": p.rung_text or "in progress (no rung label)",
            })
        elif p.status == SlotStatus.MISSING:
            findings.append({
                "slot_key": p.slot_key,
                "kind": p.kind.value,
                "status": p.status.value,
                "severity": "warning",
                "reason": p.eta_text or "missing (no ETA)",
            })
    return findings


def _submit_agent_lane_escalation(
    manifest: Mapping[str, Any],
    digest: str,
    findings: list[dict],
) -> Optional[str]:
    try:
        from recovery import (  # type: ignore
            HumanEscalationRequest,
            _next_escalation_id,
            submit_escalation,
        )
    except ImportError:
        logger.warning(
            "preview consumer: recovery module not importable; "
            "agent-lane escalation skipped."
        )
        return None

    critical = [f for f in findings if f.get("severity") == "critical"]
    severity = "critical" if critical else "warning"
    escalation_id = _next_escalation_id()
    preview_path = manifest.get("preview_path") or ""
    trigger = manifest.get("trigger_reason") or ""

    req = HumanEscalationRequest(
        id=escalation_id,
        operation_name=f"{AGENT_ESCALATION_OP}:{trigger}",
        error_chain=[{
            "level": ESCALATION_LEVEL_CONTENT,
            "source": "preview_critic",
            "preview_path": preview_path,
            "digest": digest,
            "timestamp": time.time(),
        }],
        diagnosis={
            "root_cause": (
                f"{len(findings)} preview finding(s) at {trigger}: {digest}"
            ),
            "confidence": "derived",
            "level": ESCALATION_LEVEL_CONTENT,
            "findings": findings,
            "preview_path": preview_path,
            "input_hash": manifest.get("input_hash") or "",
        },
        proposed_actions=[{
            "action_id": "content_ladder_review",
            "description": (
                "Route to content ladder (scenario director / rewriter) "
                "for the flagged slots."
            ),
            "risk_level": "low",
        }],
        severity=severity,
        timestamp=time.time(),
    )
    submit_escalation(req)
    return escalation_id


# ---------------------------------------------------------------------------
# Human lane — SSE emission + dislike handler
# ---------------------------------------------------------------------------


#: Known canonical boundary labels surfaced to the UI (UI-06a #208).
#: The UI uses these to position the ▶ marker on the timeline.
BOUNDARY_NARRATION_LOCKED = "narration_locked"
BOUNDARY_HALFWAY = "halfway"
BOUNDARY_FINAL = "final"

_SCENE_TRIGGER_RE = re.compile(r"^scene_(\d+)_complete$")
_ACT_TRIGGER_RE = re.compile(r"^act_(\d+)_complete$")


def derive_boundary(trigger_reason: str) -> str:
    """Map a builder ``trigger_reason`` to a UI-facing boundary label.

    The UI renders preview markers at canonical boundaries; the builder
    uses slightly more descriptive reasons (``pre_production``,
    ``scene_N_complete``, …).  This helper normalises to the labels
    documented in UI-06 (#191, #208):

    - ``pre_production`` → :data:`BOUNDARY_NARRATION_LOCKED`
    - ``halfway`` → :data:`BOUNDARY_HALFWAY`
    - ``final`` → :data:`BOUNDARY_FINAL`
    - ``scene_N_complete`` → ``scene_N_complete`` (unchanged)
    - ``act_N_complete`` → ``act_N_complete`` (unchanged)

    Unknown trigger reasons pass through verbatim so the UI can still
    render them; emitting an event with a recognisable label is always
    preferable to dropping the event (no silent degradation).
    """
    t = (trigger_reason or "").strip()
    if not t:
        return ""
    if t == "pre_production":
        return BOUNDARY_NARRATION_LOCKED
    if t in ("halfway", "halfway_milestone"):
        return BOUNDARY_HALFWAY
    if t in ("final", "final_complete", "pipeline_complete"):
        return BOUNDARY_FINAL
    if _SCENE_TRIGGER_RE.match(t) or _ACT_TRIGGER_RE.match(t):
        return t
    return t


def _public_preview_url(preview_path: str) -> str:
    """Return a URL the dashboard can fetch for the preview mp4.

    Previews render to :data:`~previews.builder.DEFAULT_PREVIEW_DIR`
    (typically ``/tmp/documentary-pipeline/previews``).  The agui
    router exposes ``GET /agui/preview/<filename>`` which streams the
    file from that directory — we return a backend-relative URL so the
    frontend (on a different origin) prepends its ``BACKEND_URL``.

    If the preview is not under the preview directory (tests may use a
    tmp path), fall back to ``file://`` so the consumer always has
    *some* URL to show rather than an empty string.  The dashboard
    interprets empty ``file_url`` as "preview bytes unavailable", which
    is a legitimate observable state — but we prefer to emit the real
    path when we have one.
    """
    if not preview_path:
        return ""
    # Late import to avoid a module-level dependency cycle; the builder
    # lives under server/ and imports from here via the triggers.
    try:
        from previews.builder import DEFAULT_PREVIEW_DIR  # type: ignore
    except ImportError:
        return preview_path
    try:
        abs_path = os.path.abspath(preview_path)
        abs_dir = os.path.abspath(DEFAULT_PREVIEW_DIR)
    except (TypeError, ValueError):
        return preview_path
    if abs_path.startswith(abs_dir + os.sep):
        rel = os.path.relpath(abs_path, abs_dir)
        # URL components are always forward-slashed; POSIX style wins.
        return f"/agui/preview/{rel.replace(os.sep, '/')}"
    return abs_path


def _iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def emit_preview_ready(manifest: PreviewManifest) -> None:
    """Emit a ``preview_ready`` SSE event for the dashboard.

    Dashboard (workstream H) subscribes to ``dashboard_events`` and
    renders the preview mp4 inline on the OTIO timeline.  The event
    payload carries the preview path, trigger reason, and a short
    reasoning digest so the dashboard can show a one-line summary
    without re-reading the manifest.

    Payload shape (UI-06a #208)::

        {
          "type": "preview_ready",
          "boundary": "<narration_locked|scene_N_complete|halfway|final|...>",
          "duration_sec": float,
          "file_url": str,        # URL the dashboard can fetch
          "rendered_at": str,     # ISO-8601 UTC timestamp
          # plus legacy fields preserved for existing consumers:
          "preview_path": str,
          "manifest_path": str,
          "input_hash": str,
          "trigger_reason": str,
          "total_duration_sec": float,
          "counts": dict,
          "digest": str,
          "kind": str,
        }
    """
    try:
        from agui import emit_agui_event  # type: ignore
    except ImportError:
        logger.warning(
            "preview consumer: agui module not importable; "
            "dashboard SSE emission skipped."
        )
        return

    plans = list(manifest.slots)
    # PreviewManifest.to_dict is the on-wire JSON the dashboard expects.
    data = manifest.to_dict()
    digest = _reasoning_digest(data, plans)

    boundary = derive_boundary(manifest.trigger_reason)
    file_url = _public_preview_url(manifest.preview_path)
    if manifest.built_at:
        try:
            rendered_at = _dt.datetime.fromtimestamp(
                manifest.built_at, tz=_dt.timezone.utc
            ).isoformat(timespec="seconds")
        except (TypeError, ValueError, OSError):
            rendered_at = _iso_now()
    else:
        rendered_at = _iso_now()

    payload = {
        # UI-06a contract fields (issue #208):
        "boundary": boundary,
        "duration_sec": float(manifest.total_duration_sec or 0.0),
        "file_url": file_url,
        "rendered_at": rendered_at,
        # Legacy fields kept for existing ARCH-G3 consumers:
        "preview_path": manifest.preview_path,
        "manifest_path": manifest.manifest_path,
        "input_hash": manifest.input_hash,
        "trigger_reason": manifest.trigger_reason,
        "total_duration_sec": float(manifest.total_duration_sec or 0.0),
        "counts": dict(manifest.counts),
        "digest": digest,
        "kind": manifest.kind,
    }

    try:
        emit_agui_event(PREVIEW_READY_EVENT, payload)
    except Exception:  # noqa: BLE001 — dashboard emit is best-effort
        logger.exception("preview consumer: emit_agui_event failed")

    # UI-01 (#186): emit a chat-narrator turn for the same event so the
    # CopilotKit stream carries a plain-English one-liner.  Best-effort;
    # a missing narrator module never stalls preview delivery.
    try:
        from agents.chat_narrator import emit_narrator_event  # type: ignore
        emit_narrator_event(
            "preview_ready",
            fields={
                "boundary": boundary,
                "duration_sec": manifest.total_duration_sec,
                "preview_path": manifest.preview_path,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("chat_narrator preview_ready emission failed: %s", exc)


def emit_preview_failed(
    trigger_reason: str,
    error: str,
    *,
    preview_path: str = "",
    input_hash: str = "",
    rendered_at: Optional[str] = None,
) -> None:
    """Emit a ``preview_failed`` SSE event for the dashboard.

    Parallel of :func:`emit_preview_ready` for render failures.  The
    pipeline must never silently swallow a preview failure — even when
    the builder itself is not safe to re-run (missing ffmpeg / font),
    the dashboard needs to know a scheduled preview did not materialise
    so the ▶ marker can surface the failure to the user.

    The ``rendered_at`` timestamp is the UTC ISO-8601 time the failure
    was observed; if not supplied, it defaults to "now".
    """
    try:
        from agui import emit_agui_event  # type: ignore
    except ImportError:
        logger.warning(
            "preview consumer: agui module not importable; "
            "preview_failed SSE emission skipped."
        )
        return

    boundary = derive_boundary(trigger_reason)
    payload = {
        "boundary": boundary,
        "trigger_reason": trigger_reason or "",
        "error": str(error or ""),
        "rendered_at": rendered_at or _iso_now(),
        "preview_path": preview_path or "",
        "input_hash": input_hash or "",
        "file_url": _public_preview_url(preview_path) if preview_path else "",
    }

    try:
        emit_agui_event(PREVIEW_FAILED_EVENT, payload)
    except Exception:  # noqa: BLE001 — dashboard emit is best-effort
        logger.exception("preview consumer: emit_agui_event failed (failed path)")


def handle_human_dislike_preview(
    event: Mapping[str, Any],
    state: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Handle a dashboard ``human_dislike_preview`` event.

    Escalates to **proactive L4** (human escalation, marked with
    ``level=L4``) via :func:`recovery.submit_escalation`.  Returns
    the new escalation id so the dashboard can poll its status.
    """
    preview_path = str(event.get("preview_path") or "")
    reason = str(event.get("reason") or "human dislike (no reason)")
    reviewer = str(event.get("reviewer") or event.get("user") or "dashboard")
    trigger = str(event.get("trigger_reason") or "")

    try:
        from recovery import (  # type: ignore
            HumanEscalationRequest,
            _next_escalation_id,
            submit_escalation,
        )
    except ImportError:
        logger.warning(
            "preview consumer: recovery module not importable; "
            "human-dislike escalation skipped."
        )
        return None

    escalation_id = _next_escalation_id()

    # Attach the manifest digest when the file is available so the
    # escalation carries enough context for a human to triage without
    # loading the mp4.
    manifest_digest = ""
    manifest_hash = ""
    if preview_path:
        try:
            manifest = _load_manifest(preview_path)
            plans = _slot_plans_from_manifest(manifest)
            manifest_digest = _reasoning_digest(manifest, plans)
            manifest_hash = str(manifest.get("input_hash") or "")
        except (FileNotFoundError, ValueError):
            manifest_digest = "manifest unavailable"

    req = HumanEscalationRequest(
        id=escalation_id,
        operation_name=f"{HUMAN_DISLIKE_ESCALATION_OP}:{trigger}" if trigger
        else HUMAN_DISLIKE_ESCALATION_OP,
        error_chain=[{
            "level": ESCALATION_LEVEL_L4,
            "source": "dashboard_human_dislike",
            "reviewer": reviewer,
            "preview_path": preview_path,
            "timestamp": time.time(),
        }],
        diagnosis={
            "root_cause": f"Human dislike of preview: {reason}",
            "confidence": "human_reported",
            "level": ESCALATION_LEVEL_L4,
            "reviewer": reviewer,
            "preview_path": preview_path,
            "input_hash": manifest_hash,
            "digest": manifest_digest,
        },
        proposed_actions=[
            {
                "action_id": "regenerate_flagged_scenes",
                "description": (
                    "Regenerate scenes the reviewer flagged (content ladder "
                    "re-prompt)."
                ),
                "risk_level": "medium",
            },
            {
                "action_id": "scenario_director_reconsider",
                "description": (
                    "Route to Scenario Director for re-planning of later "
                    "scenes."
                ),
                "risk_level": "low",
            },
        ],
        severity="critical",
        timestamp=time.time(),
    )
    submit_escalation(req)

    if state is not None and isinstance(state, dict):
        history = state.get(PREVIEW_HISTORY_KEY)
        if isinstance(history, list) and history:
            history[-1] = {
                **history[-1],
                "human_dislike_escalation_id": escalation_id,
            }

    return escalation_id


__all__ = [
    "AGENT_ESCALATION_OP",
    "BOUNDARY_FINAL",
    "BOUNDARY_HALFWAY",
    "BOUNDARY_NARRATION_LOCKED",
    "ESCALATION_LEVEL_CONTENT",
    "ESCALATION_LEVEL_L4",
    "HUMAN_DISLIKE_ESCALATION_OP",
    "HUMAN_DISLIKE_EVENT",
    "PREVIEW_FAILED_EVENT",
    "PREVIEW_READY_EVENT",
    "derive_boundary",
    "emit_preview_failed",
    "emit_preview_ready",
    "evaluate_preview",
    "handle_human_dislike_preview",
]
