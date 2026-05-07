"""
REST API for the dashboard to read OTIO timeline state.

The dashboard subscribes to SSE events for real-time updates, but it
also needs to read the current state of the OTIO file on demand
(e.g., on page load, on reconnect). This module provides a minimal
FastAPI router that serves the OTIO file contents as JSON.

All endpoints read from the OTIO file on disk — stateless, no
in-memory manager needed.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/otio", tags=["otio"])


def _get_pipeline_dir() -> str:
    """Get the pipeline directory from env or default."""
    return os.environ.get("PIPELINE_DIR", "/tmp/documentary-pipeline")


def _resolve_timeline() -> str:
    """Resolve the timeline path from the pipeline manifest."""
    from tools.otio_file_ops import resolve_timeline_path
    return resolve_timeline_path()


@router.get("/timeline")
async def get_timeline() -> dict[str, Any]:
    """Return the full OTIO timeline structure for the dashboard.

    Returns tracks, clips, and metadata in a JSON format the
    frontend otio-timeline component can render.
    """
    try:
        from tools.otio_file_ops import otio_read
        tp = _resolve_timeline_path()
        timeline = otio_read(tp)

        tracks = []
        for track in timeline.tracks:
            clips = []
            for item in track:
                clip = {
                    "name": item.name or "",
                    "type": type(item).__name__,
                    "source_range": None,
                }
                if hasattr(item, "source_range") and item.source_range:
                    sr = item.source_range
                    clip["source_range"] = {
                        "start_time": float(sr.start_time.value) if hasattr(sr.start_time, 'value') else 0,
                        "duration": float(sr.duration.value) if hasattr(sr.duration, 'value') else 0,
                    }
                clips.append(clip)
            tracks.append({
                "name": track.name,
                "kind": track.kind if hasattr(track, "kind") else "video",
                "clips": clips,
            })

        doc_meta = dict(timeline.metadata.get("documentary", {}))

        return {
            "name": timeline.name,
            "tracks": tracks,
            "metadata": doc_meta,
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/metadata/{key}")
async def get_metadata(key: str) -> dict[str, Any]:
    """Read a single metadata key from the OTIO file."""
    try:
        from tools.otio_metadata import read_pipeline_metadata
        tp = _resolve_timeline_path()
        val = read_pipeline_metadata(tp, key)
        if val is None:
            raise HTTPException(status_code=404, detail=f"Key '{key}' not found")
        return {"key": key, "value": val}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/lifecycle")
async def get_lifecycle() -> dict[str, Any]:
    """Read the OTIO lifecycle state (draft/authoritative)."""
    try:
        from tools.otio_lifecycle import get_otio_lifecycle_state, get_escalation
        tp = _resolve_timeline_path()
        state = get_otio_lifecycle_state(tp)
        escalation = get_escalation(tp)
        return {
            "state": state,
            "escalation": escalation,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/gates")
async def get_gates() -> dict[str, Any]:
    """Read all gate validation results from OTIO metadata."""
    try:
        from tools.otio_metadata import read_pipeline_metadata
        tp = _resolve_timeline_path()
        gates = {}
        for stage in ("scenario", "audio", "video", "assembly"):
            gate = read_pipeline_metadata(tp, f"gate_{stage}")
            if gate is not None:
                gates[stage] = gate
        return {"gates": gates}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ladders")
async def get_ladders() -> dict[str, Any]:
    """Read escalation ladder states from OTIO metadata."""
    try:
        from tools.otio_metadata import read_pipeline_metadata
        tp = _resolve_timeline_path()
        ladders = {}
        for stage in ("audio", "video"):
            ladder = read_pipeline_metadata(tp, f"{stage}_ladder")
            if ladder is not None:
                ladders[stage] = ladder
        return {"ladders": ladders}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/manifest")
async def get_manifest() -> dict[str, Any]:
    """Read the pipeline manifest (run ID, timeline path, etc.)."""
    try:
        pipeline_dir = _get_pipeline_dir()
        manifest_path = os.path.join(pipeline_dir, "pipeline_manifest.json")
        if not os.path.exists(manifest_path):
            raise HTTPException(status_code=404, detail="Pipeline manifest not found")
        with open(manifest_path, "r") as f:
            return json.load(f)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _resolve_timeline_path() -> str:
    """Resolve the timeline path, catching errors for HTTP responses."""
    from tools.otio_file_ops import resolve_timeline_path
    try:
        return resolve_timeline_path()
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
