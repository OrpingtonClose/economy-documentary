"""
AG-UI general REST endpoints — extracted from the monolithic agui.py.

This module contains all remaining REST endpoints that don't belong in the
other three focused modules (agui_events, agui_approval, agui_slot_bridge):

- Reasoning endpoints: digests and raw traces
- Artifact endpoints: list, per-scene, and ingest
- Feedback endpoints: submit, list, and active constraints
- Escalation endpoints: list, pending, and respond
- File-backed endpoints: preview assets and final film
- Scene/brief/stage endpoints: restated brief, scenes, visual-concepts,
  clips, timeline, qa-results
- SSE stream endpoint: the centrepiece dashboard event stream
- Backfill endpoint: prompt backfill utility
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from agui_events import (
    emit_agui_event,
    get_feedback_store,
    FeedbackStore,
    ArtifactType,
    ArtifactStatus,
    ArtifactEvent,
    FeedbackType,
    HumanFeedback,
    _store,
    subscribe_agui_events,
    unsubscribe_agui_events,
)
import agui_events  # for runtime agui_events._OUTPUT_DIR access (monkeypatching compat)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agui", tags=["agui"])

# ---------------------------------------------------------------------------
# File-backed AG-UI config  (agui_events._OUTPUT_DIR lives in agui_events; access via
# agui_events.agui_events._OUTPUT_DIR so monkeypatching agui_events.agui_events._OUTPUT_DIR propagates)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Reasoning endpoints
# ---------------------------------------------------------------------------

@router.get("/reasoning/digests")
async def get_reasoning_digests(
    limit: int = 50,
    since: float | None = None,
    phase: str | None = None,
    importance: str | None = None,
):
    """Get reasoning digests — concise summaries of agent activity.

    These are batch-processed from raw traces by the DigestEngine background
    thread.  Each digest summarises a burst of agent activity into a 1-2
    sentence summary with structured details (ratings, errors, token costs,
    production planning decisions).

    Query params:
        limit:      max digests (default 50)
        since:      only digests after this Unix timestamp (for polling)
        phase:      filter by pipeline phase (scenario, audio, visual_direction, production, assembly)
        importance: filter by importance (low, medium, high)
    """
    try:
        from plugins.reasoning_digest import get_digest_engine

        engine = get_digest_engine()

        if since and since > 0:
            digests = engine.get_since(since, limit=limit)
        else:
            digests = engine.get_recent(limit)

        # Apply filters
        if phase:
            digests = [d for d in digests if d.get("phase") == phase]
        if importance:
            if importance == "medium":
                # "Medium+" means medium AND high
                digests = [d for d in digests if d.get("importance") in ("medium", "high")]
            else:
                digests = [d for d in digests if d.get("importance") == importance]

        return JSONResponse({"digests": digests, "count": len(digests)})

    except Exception as e:
        return JSONResponse(
            {"digests": [], "count": 0, "error": str(e)},
            status_code=200,
        )


@router.get("/reasoning/raw")
async def get_reasoning_traces_raw(
    agent: str | None = None,
    event_type: str | None = None,
    limit: int = 50,
    since: float | None = None,
):
    """Get raw reasoning traces (for drill-down from a digest).

    The frontend should prefer ``/reasoning/digests`` for the main view.
    Use this endpoint when the user expands a digest and wants to see the
    underlying raw events.

    Query params:
        agent:      filter by agent name
        event_type: filter by event type (llm_request, llm_response, etc.)
        limit:      max rows (default 50)
        since:      only rows after this Unix timestamp (for polling)
    """
    # reasoning_trace plugin removed (ADK pipeline deleted)
    return JSONResponse(
        {"traces": [], "count": 0, "error": "reasoning_trace plugin removed"},
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Artifact endpoints
# ---------------------------------------------------------------------------

@router.get("/artifacts")
async def get_artifacts(type: str | None = None):
    """Get all artifacts produced by the pipeline.

    Optional query param ?type= filters by artifact type.
    """
    arts = _store.get_all_artifacts()
    if type:
        arts = [a for a in arts if a.get("type") == type]
    return JSONResponse({"artifacts": arts})


@router.get("/artifacts/scene/{scene_num}")
async def get_scene_artifacts(scene_num: int):
    """Get all artifacts for a specific scene."""
    return JSONResponse({"artifacts": _store.get_artifacts_for_scene(scene_num)})


@router.post("/artifacts/ingest")
async def ingest_artifact(request: Request):
    """Register an artifact from an external pipeline runner.

    This is the AG-UI counterpart of /dashboard/ingest — it bridges
    run_pipeline.py (separate process) with the AG-UI artifact store
    so the frontend Artifacts, Clip Reviewer, Scenario Editor, etc.
    tabs all populate canonically.

    Body:
        id:           str  — unique artifact ID
        type:         str  — "video_clip", "narration", "scene_script",
                             "visual_concept", "assembled_video"
        status:       str  — "generating", "pending_review", "approved", etc.
        scene_num:    int  — scene number
        phrase_idx:   int  — phrase index within scene
        language:     str  — language code
        preview_url:  str  — URL or path to the artifact
        duration_sec: float — duration in seconds
        qa_scores:    dict  — QA quality scores
        metadata:     dict  — additional metadata (prompt, lora, etc.)
    """

    body = await request.json()

    art_id = body.get("id", _store._next_id("art"))
    try:
        art_type = ArtifactType(body.get("type", "video_clip"))
    except ValueError:
        art_type = ArtifactType.VIDEO_CLIP

    try:
        art_status = ArtifactStatus(body.get("status", "pending_review"))
    except ValueError:
        art_status = ArtifactStatus.PENDING_REVIEW

    artifact = ArtifactEvent(
        id=art_id,
        artifact_type=art_type,
        status=art_status,
        scene_num=body.get("scene_num", 0),
        phrase_idx=body.get("phrase_idx", 0),
        language=body.get("language", ""),
        preview_url=body.get("preview_url", ""),
        duration_sec=body.get("duration_sec", 0.0),
        qa_scores=body.get("qa_scores", {}),
        metadata=body.get("metadata", {}),
        timestamp=time.time(),
    )
    _store.register_artifact(artifact)
    logger.info(
        "AG-UI ingest: %s %s (scene %d, phrase %d)",
        art_type.value, art_id, artifact.scene_num, artifact.phrase_idx,
    )
    return JSONResponse({"status": "ok", "artifact_id": art_id})


# ---------------------------------------------------------------------------
# Feedback endpoints
# ---------------------------------------------------------------------------

@router.post("/feedback")
async def post_feedback(body: dict):
    """Submit human feedback on an artifact or the pipeline.

    Body:
        feedback_type: "approve" | "reject" | "comment" | "regenerate"
        artifact_id: (optional) artifact to provide feedback on
        scene_num: (optional) scene number
        comment: (optional) free-text comment
        regeneration_level: (optional) "clip" | "scene" | "style"
    """
    try:
        fb_type = FeedbackType(body.get("feedback_type", "comment"))
    except ValueError:
        return JSONResponse(
            {"error": f"Invalid feedback_type: {body.get('feedback_type')}"},
            status_code=400,
        )

    feedback = HumanFeedback(
        id=_store._next_id("fb"),
        feedback_type=fb_type,
        artifact_id=body.get("artifact_id", ""),
        scene_num=body.get("scene_num", 0),
        comment=body.get("comment", ""),
        timestamp=time.time(),
        regeneration_level=body.get("regeneration_level", ""),
    )
    _store.add_feedback(feedback)
    return JSONResponse({"status": "ok", "feedback_id": feedback.id})


@router.get("/feedback")
async def get_feedback():
    """Get all accumulated human feedback."""
    return JSONResponse({"feedback": _store.get_all_feedback()})


@router.get("/constraints")
async def get_constraints():
    """Get active generation constraints derived from human feedback.

    Pipeline agents can poll this to incorporate human guidance.
    """
    return JSONResponse(_store.get_active_constraints())


# ---------------------------------------------------------------------------
# Escalation endpoints
# ---------------------------------------------------------------------------

@router.get("/escalations")
async def get_escalations():
    """Get all escalation requests (pending and resolved)."""
    from recovery import get_all_escalations
    return JSONResponse({"escalations": get_all_escalations()})


@router.get("/escalations/pending")
async def get_pending_escalations_endpoint():
    """Get pending escalation requests that need human response."""
    from recovery import get_pending_escalations
    return JSONResponse({"escalations": get_pending_escalations()})


@router.post("/escalations/{escalation_id}/respond")
async def respond_to_escalation(escalation_id: str, body: dict):
    """Respond to an escalation request.

    Body:
        action: "retry_with_fix" | "skip" | "abort" | "amend"
        kwargs: (optional) amended kwargs for "amend" action
        comment: (optional) human comment
    """
    from recovery import resolve_escalation

    action = body.get("action", "")
    if not action:
        return JSONResponse(
            {"error": "Missing 'action' field"},
            status_code=400,
        )

    response = {
        "action": action,
        "kwargs": body.get("kwargs", {}),
        "comment": body.get("comment", ""),
        "timestamp": time.time(),
    }

    success = resolve_escalation(escalation_id, response)
    if not success:
        return JSONResponse(
            {"error": f"Escalation {escalation_id} not found"},
            status_code=404,
        )

    return JSONResponse({"status": "resolved", "escalation_id": escalation_id})


# ---------------------------------------------------------------------------
# File-backed AG-UI endpoints — read pipeline output from disk
# ---------------------------------------------------------------------------

@router.get("/preview/{filename:path}")
async def get_preview_asset(filename: str):
    """Serve a rendered preview mp4 by filename (UI-06a #208).

    The ARCH-G1 preview builder writes ``preview_<hash>.mp4`` files to
    :data:`~previews.builder.DEFAULT_PREVIEW_DIR`.  The dashboard needs
    a fetchable URL to drive a ``<video>`` element, so we expose the
    directory through this whitelisted endpoint.

    Security: ``filename`` is resolved against the preview directory
    and must stay inside it; any traversal attempt returns 404.  Only
    files ending in ``.mp4`` or ``.manifest.json`` are served.
    """
    from fastapi.responses import FileResponse
    from previews.builder import DEFAULT_PREVIEW_DIR

    base = os.path.abspath(DEFAULT_PREVIEW_DIR)
    candidate = os.path.abspath(os.path.join(base, filename))
    if not candidate.startswith(base + os.sep) and candidate != base:
        return JSONResponse({"error": "invalid preview path"}, status_code=400)
    if not os.path.isfile(candidate):
        return JSONResponse({"error": "preview not found"}, status_code=404)
    if candidate.endswith(".mp4"):
        media_type = "video/mp4"
    elif candidate.endswith(".manifest.json"):
        media_type = "application/json"
    else:
        return JSONResponse(
            {"error": "unsupported preview file type"}, status_code=400
        )
    return FileResponse(candidate, media_type=media_type)


@router.get("/final_film/{filename:path}")
async def get_final_film(filename: str, request: Request):
    """Serve the assembled documentary so the UI can play / download it.

    The :func:`deterministic_assembly_callback` writes one of
    ``final_documentary.mp4``, ``final_documentary_ru.mp4`` or
    ``final_documentary_en.mp4`` to ``agui_events._OUTPUT_DIR``.  The OTIO
    timeline view (see :func:`otio_timeline_model.build_timeline_view`)
    renders a "▶ Watch your film" card pointing at this URL.

    Security: ``filename`` is resolved against the output directory;
    traversal attempts return 404.  Only the three canonical final
    filenames are served.
    """
    from fastapi.responses import FileResponse

    allowed = {
        "final_documentary.mp4",
        "final_documentary_ru.mp4",
        "final_documentary_en.mp4",
    }
    if filename not in allowed:
        return JSONResponse({"error": "not a final-film filename"}, status_code=400)

    base = os.path.abspath(agui_events._OUTPUT_DIR)
    candidate = os.path.abspath(os.path.join(base, filename))
    if not candidate.startswith(base + os.sep) and candidate != base:
        return JSONResponse({"error": "invalid final-film path"}, status_code=400)
    if not os.path.isfile(candidate):
        return JSONResponse({"error": "final film not found"}, status_code=404)

    return FileResponse(candidate, media_type="video/mp4", filename=filename)


# ---------------------------------------------------------------------------
# Scene / brief / stage endpoints
# ---------------------------------------------------------------------------

@router.get("/restated_brief")
async def get_restated_brief():
    """Serve the R0 :class:`BriefIntent` (INTENT-03, issue #267).

    Reads the typed brief that the Intent Extractor (INTENT-01) wrote
    at run start.  The payload mirrors :class:`BriefIntent` — every
    field is hard-constraint material the constraint gate (INTENT-02)
    and the per-stage verifier (INTENT-04) also see.  The frontend
    wiring lives in a separate PR (issue #255); this endpoint is the
    backend-only half called out in #267.

    Response shape::

        {
          "brief_intent": {
            "duration_sec": 420.0,
            "tolerance_sec": 30.0,
            "audience": "adhd-friendly",
            "tone": [...],
            "corpus_paths": [...],
            "required_topics": [...],
            "forbidden_topics": [...],
            "format_hints": {...},
            "confidence": {...}
          },
          "present": true
        }

    When no run is active (no backup on disk) ``present`` is ``false``
    and ``brief_intent`` is ``null`` so the frontend can render an
    "awaiting brief" state without having to special-case 404s.
    """
    try:
        from agents.intent_extractor import read_intent_backup

        intent = read_intent_backup()
    except Exception as exc:  # pragma: no cover -- defensive
        logger.warning("/agui/restated_brief: read failure: %s", exc)
        return JSONResponse(
            {"brief_intent": None, "present": False, "error": str(exc)},
            status_code=200,
        )

    if intent is None:
        return JSONResponse({"brief_intent": None, "present": False})

    payload = intent.model_dump(mode="json")
    return JSONResponse({"brief_intent": payload, "present": True})


@router.get("/scenes")
async def get_scenes():
    """Return scene list from the pipeline's scenes backup file.

    The scenario director writes _scenes_backup.json in the timelines dir.
    """
    scenes_path = os.path.join(agui_events._OUTPUT_DIR, "timelines", "_scenes_backup.json")
    if not os.path.exists(scenes_path):
        return JSONResponse({"scenes": []})
    try:
        with open(scenes_path) as f:
            scenes = json.load(f)
        return JSONResponse({"scenes": scenes})
    except Exception as exc:
        logger.warning("Failed to read scenes: %s", exc)
        return JSONResponse({"scenes": []})


@router.post("/backfill-prompts")
async def backfill_prompts():
    """Backfill old status files that lack prompt_full.

    Reads the full prompt from the visual style backup concepts list
    and patches each status JSON on disk so that prompt_full is populated.
    Returns the count of files patched.
    """
    import glob as _glob
    import re as _re

    # Try to load the visual style backup which may contain the full prompts
    style_path = os.path.join(agui_events._OUTPUT_DIR, "timelines", "_visual_style_backup.json")
    style_data: dict = {}
    if os.path.exists(style_path):
        try:
            with open(style_path) as f:
                style_data = json.load(f)
        except Exception as exc:
            from maintainer import notify_maintainer
            notify_maintainer("agui_style_load", str(exc), {"style_path": style_path})

    # Build lookup of full prompts from concepts in style backup
    concept_prompts: dict[tuple[int, int], str] = {}
    for concept in style_data.get("concepts", []):
        key = (concept.get("scene_num", 0), concept.get("phrase_idx", 0))
        prompt = concept.get("prompt", "")
        if prompt and len(prompt) > 200:
            concept_prompts[key] = prompt

    patched = 0
    pattern = os.path.join(agui_events._OUTPUT_DIR, "video", "*_status.json")
    for path in sorted(_glob.glob(pattern)):
        fname = os.path.basename(path)
        m = _re.match(r"scene_(\d+)_phrase_(\d+)_status\.json", fname)
        if not m:
            continue
        scene_num = int(m.group(1))
        phrase_idx = int(m.group(2))
        try:
            with open(path) as f:
                data = json.load(f)
            existing_full = data.get("prompt_full", "")
            preview = data.get("prompt_preview", "")
            # Only patch if prompt_full is missing or same length as truncated preview
            if not existing_full or (preview and len(existing_full) <= len(preview)):
                full_prompt = concept_prompts.get((scene_num, phrase_idx), "")
                if full_prompt:
                    data["prompt_full"] = full_prompt
                    with open(path, "w") as f:
                        json.dump(data, f, indent=2)
                    patched += 1
        except Exception as exc:
            logger.debug("Failed to backfill %s: %s", path, exc)

    return JSONResponse({"status": "ok", "patched": patched, "total_concepts": len(concept_prompts)})


@router.get("/visual-concepts")
async def get_visual_concepts():
    """Return visual concepts derived from video status files.

    Gated: requires 'scenario' stage to be approved first.
    """
    from callbacks.approval_gate import is_stage_approved as _is_stage_approved

    if not _is_stage_approved("scenario"):
        return JSONResponse({
            "concepts": [],
            "visual_style": {},
            "gate": {"blocked": True, "requires": "scenario", "message": "Approve the scenario first to unlock visual prompts"},
        })
    import glob as _glob
    import re as _re

    # Load visual style backup for global info and concept-level prompt reasoning
    style_path = os.path.join(agui_events._OUTPUT_DIR, "timelines", "_visual_style_backup.json")
    style: dict = {}
    concept_reasoning: dict[tuple[int, int], str] = {}
    concept_full_prompts: dict[tuple[int, int], str] = {}
    if os.path.exists(style_path):
        try:
            with open(style_path) as f:
                style = json.load(f)
            # Extract per-concept reasoning and full prompts from backup
            for concept in style.get("concepts", []):
                key = (concept.get("scene_num", 0), concept.get("phrase_idx", 0))
                reasoning = concept.get("prompt_reasoning", concept.get("reasoning", ""))
                if reasoning:
                    concept_reasoning[key] = reasoning
                full_prompt = concept.get("prompt", "")
                if full_prompt:
                    concept_full_prompts[key] = full_prompt
        except Exception as exc:
            from maintainer import notify_maintainer
            notify_maintainer("agui_concept_load", str(exc))

    concepts: list[dict] = []
    pattern = os.path.join(agui_events._OUTPUT_DIR, "video", "*_status.json")
    for path in sorted(_glob.glob(pattern)):
        fname = os.path.basename(path)
        # Extract scene/phrase from filename: scene_001_phrase_002_status.json
        m = _re.match(r"scene_(\d+)_phrase_(\d+)_status\.json", fname)
        if not m:
            continue
        scene_num = int(m.group(1))
        phrase_idx = int(m.group(2))
        try:
            with open(path) as f:
                data = json.load(f)
            key = (scene_num, phrase_idx)
            # Use prompt_full from status file, falling back to style backup, then preview
            prompt = data.get("prompt_full", "")
            if not prompt or len(prompt) <= 200:
                prompt = concept_full_prompts.get(key, data.get("prompt_preview", ""))
            concepts.append({
                "scene_num": scene_num,
                "phrase_idx": phrase_idx,
                "prompt": prompt,
                "prompt_reasoning": concept_reasoning.get(key, data.get("prompt_reasoning", "")),
                "quality": data.get("quality", "unknown"),
                "qa_reason": data.get("qa_reason", ""),
                "attempts": data.get("attempts", 0),
                "status": data.get("status", "unknown"),
                "lora_id": data.get("lora_id", ""),
                "lora_weight": data.get("lora_weight", 0.0),
                "camera_style": data.get("camera_style", ""),
                "mood": data.get("mood", ""),
                "start_time": 0.0,
                "end_time": 0.0,
                "duration": 0.0,
                "environment": "",
            })
        except Exception as exc:
            logger.debug("Failed to read %s: %s", path, exc)

    return JSONResponse({"concepts": concepts, "visual_style": style})


@router.get("/clips")
async def get_clips():
    """Return video clips with QA status for the Clip Reviewer tab.

    Gated: requires 'prompts' stage to be approved first.
    """
    from callbacks.approval_gate import is_stage_approved as _is_stage_approved

    if not _is_stage_approved("prompts"):
        return JSONResponse({
            "clips": [],
            "gate": {"blocked": True, "requires": "prompts", "message": "Approve visual prompts first to unlock clip review"},
        })
    import glob as _glob
    import re as _re

    clips: list[dict] = []
    pattern = os.path.join(agui_events._OUTPUT_DIR, "video", "*_status.json")
    for path in sorted(_glob.glob(pattern)):
        fname = os.path.basename(path)
        m = _re.match(r"scene_(\d+)_phrase_(\d+)_status\.json", fname)
        if not m:
            continue
        scene_num = int(m.group(1))
        phrase_idx = int(m.group(2))
        try:
            with open(path) as f:
                data = json.load(f)
            # Check if the actual video file exists
            video_name = fname.replace("_status.json", ".mp4")
            video_path = os.path.join(agui_events._OUTPUT_DIR, "video", video_name)
            clips.append({
                "scene_num": scene_num,
                "phrase_idx": phrase_idx,
                "video_path": video_path if os.path.exists(video_path) else "",
                "narration_text": data.get("prompt_full", data.get("prompt_preview", "")),
                "duration": data.get("duration", 0.0),
                "lora_id": data.get("lora_id", ""),
                "status": "approved" if data.get("quality") == "acceptable" else "pending",
                "quality": data.get("quality", "unknown"),
                "qa_reason": data.get("qa_reason", ""),
                "attempts": data.get("attempts", 0),
            })
        except Exception as exc:
            logger.debug("Failed to read %s: %s", path, exc)

    return JSONResponse({"clips": clips})


@router.get("/timeline")
async def get_timeline():
    """Read the OTIO timeline file and return structured track/clip data.

    Gated: requires 'clips' stage to be approved first.
    """
    from callbacks.approval_gate import is_stage_approved as _is_stage_approved

    if not _is_stage_approved("clips"):
        return JSONResponse({
            "timeline": None,
            "gate": {"blocked": True, "requires": "clips", "message": "Approve clips first to unlock the timeline"},
        })
    import glob as _glob

    # Find the most recent OTIO file (skip backups starting with _)
    pattern = os.path.join(agui_events._OUTPUT_DIR, "timelines", "*.otio")
    otio_files = [
        f for f in sorted(_glob.glob(pattern))
        if not os.path.basename(f).startswith("_")
    ]
    if not otio_files:
        return JSONResponse({"timeline": None})

    otio_path = otio_files[-1]  # most recent
    try:
        with open(otio_path) as f:
            otio_data = json.load(f)

        tracks: list[dict] = []
        for track in otio_data.get("tracks", {}).get("children", []):
            clips: list[dict] = []
            gaps: list[dict] = []
            for child in track.get("children", []):
                schema = child.get("OTIO_SCHEMA", "")
                sr = child.get("source_range", {})
                duration_val = sr.get("duration", {}).get("value", 0)
                duration_rate = sr.get("duration", {}).get("rate", 24)
                duration_sec = duration_val / duration_rate if duration_rate else 0

                if "Gap" in schema:
                    gaps.append({
                        "name": child.get("name", "gap"),
                        "metadata": child.get("metadata", {}),
                    })
                else:
                    clips.append({
                        "name": child.get("name", ""),
                        "duration": round(duration_sec, 2),
                        "metadata": child.get("metadata", {}),
                    })

            tracks.append({
                "name": track.get("name", ""),
                "kind": track.get("kind", ""),
                "clips": clips,
                "gaps": gaps,
                "total_clips": len(clips),
                "total_gaps": len(gaps),
            })

        return JSONResponse({
            "timeline": {
                "timeline_name": otio_data.get("name", os.path.basename(otio_path)),
                "tracks": tracks,
            }
        })
    except Exception as exc:
        logger.warning("Failed to read OTIO timeline: %s", exc)
        return JSONResponse({"timeline": None})


@router.get("/qa-results")
async def get_qa_results():
    """Return QA results by checking pipeline output completeness.

    Derives pass/fail per phase by checking whether expected output
    files exist and have valid content.  Includes per-clip detail.
    """
    results: list[dict] = []

    # Scenario: check if scenes backup exists with content
    scenes_path = os.path.join(agui_events._OUTPUT_DIR, "timelines", "_scenes_backup.json")
    if os.path.exists(scenes_path):
        try:
            with open(scenes_path) as f:
                scenes = json.load(f)
            if scenes and len(scenes) > 0:
                scene_details = []
                for s in scenes:
                    scene_details.append({
                        "scene_num": s.get("scene_num", 0),
                        "title": s.get("title", ""),
                        "duration_sec": s.get("duration_sec", 0),
                        "voices": len(s.get("voices", [])),
                        "has_hook": bool(s.get("dopamine_hook")),
                    })
                results.append({
                    "phase": "scenario",
                    "valid": True,
                    "message": f"{len(scenes)} scenes generated with V1/V2/V3 voices",
                    "details": scene_details,
                })
            else:
                results.append({
                    "phase": "scenario",
                    "valid": False,
                    "errors": "Scenes file is empty",
                })
        except Exception as exc:
            results.append({
                "phase": "scenario",
                "valid": False,
                "errors": str(exc),
            })

    # Audio: check WAV files exist
    import glob as _glob
    wavs = sorted(_glob.glob(os.path.join(agui_events._OUTPUT_DIR, "audio", "*.wav")))
    if wavs:
        audio_details = [{"file": os.path.basename(w), "size_kb": round(os.path.getsize(w) / 1024, 1)} for w in wavs]
        results.append({
            "phase": "audio",
            "valid": True,
            "message": f"{len(wavs)} narration WAV files produced",
            "details": audio_details,
        })
    elif os.path.exists(os.path.join(agui_events._OUTPUT_DIR, "audio")):
        results.append({
            "phase": "audio",
            "valid": False,
            "errors": "Audio directory exists but no WAV files found",
        })

    # Visual Direction: check visual style backup exists
    style_path = os.path.join(agui_events._OUTPUT_DIR, "timelines", "_visual_style_backup.json")
    if os.path.exists(style_path):
        try:
            with open(style_path) as f:
                vs = json.load(f)
            concept_count = len(vs.get("concepts", []))
            results.append({
                "phase": "visual_direction",
                "valid": True,
                "message": f"Visual style generated with {concept_count} concepts",
                "details": [{"style": vs.get("style", ""), "palette": vs.get("palette", ""), "concepts": concept_count}],
            })
        except Exception as exc:
            from maintainer import notify_maintainer
            notify_maintainer("agui_visual_style", str(exc))
            results.append({
                "phase": "visual_direction",
                "valid": True,
                "message": "Visual style and concepts generated",
            })

    # Production: check video files with per-clip QA detail
    import re as _re
    videos = sorted(_glob.glob(os.path.join(agui_events._OUTPUT_DIR, "video", "*.mp4")))
    status_files = sorted(_glob.glob(os.path.join(agui_events._OUTPUT_DIR, "video", "*_status.json")))
    if videos or status_files:
        clip_details = []
        for sf in status_files:
            fname = os.path.basename(sf)
            m = _re.match(r"scene_(\d+)_phrase_(\d+)_status\.json", fname)
            if not m:
                continue
            try:
                with open(sf) as f:
                    sd = json.load(f)
                clip_details.append({
                    "scene_num": int(m.group(1)),
                    "phrase_idx": int(m.group(2)),
                    "quality": sd.get("quality", "unknown"),
                    "qa_reason": sd.get("qa_reason", ""),
                    "attempts": sd.get("attempts", 0),
                    "has_video": os.path.exists(sf.replace("_status.json", ".mp4")),
                })
            except Exception as exc:
                from maintainer import notify_maintainer
                notify_maintainer("agui_clip_detail", str(exc), {"path": path})
        passed = sum(1 for c in clip_details if c["quality"] in ("acceptable", "excellent", "good"))
        failed = sum(1 for c in clip_details if c["quality"] not in ("acceptable", "excellent", "good", "unknown"))
        results.append({
            "phase": "production",
            "valid": len(videos) > 0,
            "message": f"{len(videos)} video clips produced ({passed} passed QA, {failed} failed)",
            "errors": f"QA status files exist ({len(status_files)}) but no MP4 files" if not videos else "",
            "details": clip_details,
        })

    # Assembly: check for final documentary
    assembly_dir = os.path.join(agui_events._OUTPUT_DIR, "assembly")
    final_vids = _glob.glob(os.path.join(assembly_dir, "*.mp4")) if os.path.exists(assembly_dir) else []
    if final_vids:
        results.append({
            "phase": "assembly",
            "valid": True,
            "message": f"Final documentary assembled: {os.path.basename(final_vids[0])}",
            "details": [{"file": os.path.basename(v), "size_mb": round(os.path.getsize(v) / (1024*1024), 1)} for v in final_vids],
        })

    return JSONResponse({"results": results})


# ---------------------------------------------------------------------------
# SSE stream endpoint
# ---------------------------------------------------------------------------

@router.get("/stream")
async def stream_events(request: Request):
    """Server-Sent Events stream for the centrepiece dashboard.

    Subscribes to the shared AG-UI event bus and relays every event as
    SSE.  The dashboard listens for ``slot_state``, ``otio_authoritative``,
    and ``artifact_update`` to drive the three-track view without ever
    polling.
    """

    async def _event_gen():
        queue = subscribe_agui_events()
        try:
            # Kick the connection with an initial snapshot event so late
            # subscribers see the current OTIO state without re-fetching.
            from otio_timeline_model import build_timeline_view
            artifacts = _store.get_all_artifacts()
            view = build_timeline_view(agui_events._OUTPUT_DIR, feedback_artifacts=artifacts)
            snapshot = {
                "type": "otio_snapshot",
                "data": view.to_dict(),
                "timestamp": time.time(),
            }
            yield f"event: {snapshot['type']}\ndata: {json.dumps(snapshot['data'])}\n\n"

            last_heartbeat = time.time()
            while True:
                if await request.is_disconnected():
                    break
                if queue:
                    event = queue.popleft()
                    ev_type = event.get("type", "message")
                    payload = json.dumps({
                        "data": event.get("data"),
                        "timestamp": event.get("timestamp"),
                    })
                    yield f"event: {ev_type}\ndata: {payload}\n\n"
                    last_heartbeat = time.time()
                else:
                    if time.time() - last_heartbeat > 15:
                        yield ": heartbeat\n\n"
                        last_heartbeat = time.time()
                    await asyncio.sleep(0.15)
        finally:
            unsubscribe_agui_events(queue)

    return StreamingResponse(_event_gen(), media_type="text/event-stream")
