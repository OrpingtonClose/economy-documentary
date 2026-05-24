"""
B2 checkpoint module -- immediate upload of every pipeline artifact to Backblaze B2.

Every intermediate artifact (scenario JSON, TTS WAVs, visual concepts, video
clips, QA status files, OTIO timelines, assembly outputs, final documentaries)
is uploaded to B2 **immediately** after creation.  On restart the pipeline
restores all artifacts from B2 so no stage needs to re-run.

Bucket layout (mirrors local /tmp/documentary-pipeline/):
    cloudberry-documentary-v2/
        <run_id>/
            stage_markers/          # empty files marking completed stages
                scenario.done
                audio.done
                visual_direction.done
                production.done
                assembly.done
            state/
                pipeline_state.json  # full ADK session state snapshot
                scenes.json
                visual_style.json
                visual_concepts.json
            timelines/
                *.otio
            audio/
                scene_NNN_VOICE.wav
                scene_NNN_VOICE.txt  # text-hash sidecar
            video/
                scene_NNN_phrase_NNN.mp4
                scene_NNN_phrase_NNN_status.json
            assembly/
                *.mp4, *.wav
            output/
                final_documentary_*.mp4
                pipeline_state.json

Environment variables:
    B2_KEY_ID           -- Backblaze application key ID
    B2_APPLICATION_KEY  -- Backblaze application key
    B2_BUCKET_NAME      -- target bucket (default: cloudberry-documentary-v2)
    B2_RUN_ID           -- unique run identifier (default: auto-generated)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton B2 client
# ---------------------------------------------------------------------------

_b2_api = None
_b2_bucket = None
_b2_lock = threading.Lock()
_run_id: str = ""


def _get_bucket():
    """Lazily initialise and return the B2 bucket handle (thread-safe)."""
    global _b2_api, _b2_bucket

    if _b2_bucket is not None:
        return _b2_bucket

    with _b2_lock:
        if _b2_bucket is not None:
            return _b2_bucket

        key_id = os.environ.get("B2_KEY_ID", "")
        app_key = os.environ.get("B2_APPLICATION_KEY", "")
        bucket_name = os.environ.get("B2_BUCKET_NAME", "bearnaise-pipeline-artifacts")

        if not key_id or not app_key:
            logger.warning("B2_KEY_ID / B2_APPLICATION_KEY not set -- B2 checkpoint disabled")
            return None

        try:
            from b2sdk.v2 import B2Api, InMemoryAccountInfo  # type: ignore[import-not-found]
            info = InMemoryAccountInfo()
            _b2_api = B2Api(info)
            _b2_api.authorize_account("production", key_id, app_key)
            _b2_bucket = _ensure_public_bucket(_b2_api, bucket_name)
            logger.info(
                "B2 checkpoint enabled: bucket=%s (type=%s)",
                bucket_name, getattr(_b2_bucket, "type_", "unknown"),
            )
        except Exception as e:
            logger.error("B2 checkpoint init failed: %s", e)
            _b2_bucket = None

    return _b2_bucket


# ---------------------------------------------------------------------------
# Bucket provisioning (#108)
# ---------------------------------------------------------------------------

# Per-run buckets must be created as ``allPublic`` so downstream consumers
# (frontend preview, QA dashboard, human reviewers) can fetch artifacts by
# URL without additional auth.  The previous code silently used whatever
# type the bucket happened to have, which meant dashboards 404ed when a
# run used a freshly-created private bucket.
B2_BUCKET_TYPE_PUBLIC = "allPublic"


def _ensure_public_bucket(api, bucket_name: str):
    """Return a bucket handle, creating it as allPublic if missing.

    Issue #108: per-run B2 buckets MUST be ``allPublic``.  If the bucket
    already exists with a different type we log a loud warning (we don't
    mutate existing buckets — that's a destructive op that needs human
    sign-off) but still return the handle so uploads work.  New buckets
    are always created with ``bucket_type="allPublic"``.
    """
    try:
        bucket = api.get_bucket_by_name(bucket_name)
        existing_type = getattr(bucket, "type_", None)
        if existing_type and existing_type != B2_BUCKET_TYPE_PUBLIC:
            logger.warning(
                "B2 bucket %s has type=%s (expected %s). "
                "Artifact URLs may 401/404 for public consumers. "
                "Create a new allPublic bucket or use b2 update-bucket.",
                bucket_name, existing_type, B2_BUCKET_TYPE_PUBLIC,
            )
        return bucket
    except Exception as get_exc:
        # Either the bucket doesn't exist, or the SDK raised
        # NonExistentBucket.  Fall through to create.
        logger.info(
            "B2 bucket %s not found (%s) — creating as %s per #108",
            bucket_name, get_exc, B2_BUCKET_TYPE_PUBLIC,
        )
        # b2sdk v2 signature: create_bucket(name, bucket_type, ...)
        # bucket_type must be "allPublic" or "allPrivate".
        return api.create_bucket(bucket_name, bucket_type=B2_BUCKET_TYPE_PUBLIC)


def set_run_id(run_id: str) -> None:
    """Set the global run ID for all subsequent B2 operations."""
    global _run_id
    _run_id = run_id
    os.environ["B2_RUN_ID"] = run_id


def get_run_id() -> str:
    """Return the current run ID, creating one if needed."""
    global _run_id
    if not _run_id:
        _run_id = os.environ.get("B2_RUN_ID", "")
        if not _run_id:
            # Generate from topic + timestamp
            topic = os.environ.get("DOCUMENTARY_TOPIC", "unknown")
            ts = int(time.time())
            safe_topic = "".join(c if c.isalnum() else "_" for c in topic.lower())[:30]
            _run_id = f"{safe_topic}_{ts}"
            os.environ["B2_RUN_ID"] = _run_id
    return _run_id


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------

def _b2_key(relative_path: str) -> str:
    """Build full B2 object key from run_id + relative path."""
    return f"{get_run_id()}/{relative_path}"


# ---------------------------------------------------------------------------
# _meta.json sidecar helpers (#70 -- artifact paper trail)
# ---------------------------------------------------------------------------

# Canonical sidecar keys. Every artifact in B2 (audio clip, video clip,
# scene assembly, QA result, visual concept) MUST have a sibling
# ``_meta.json`` file listing these fields so the paper trail is
# reconstructable from B2 alone.
SIDECAR_REQUIRED_KEYS = (
    "creator_agent",
    "prompt_used",
    "qa_results_so_far",
    "validation_outcomes",
    "parent_artifact_refs",
)


def _sidecar_key(b2_relative_path: str) -> str:
    """Return the sidecar B2 relative path for a primary artifact.

    For ``audio/scene_001_V1.wav`` this returns
    ``audio/scene_001_V1.wav._meta.json``.  The ``._meta.json`` suffix is
    used (instead of replacing the extension) so ordering in B2 listings
    keeps the primary next to its sidecar and so tools can map one to
    the other with a simple suffix strip.
    """
    return f"{b2_relative_path}._meta.json"


def _build_sidecar(
    local_path: str,
    primary_b2_key: str,
    meta: dict,
) -> dict:
    """Enrich caller-provided meta with provenance defaults.

    Callers only need to supply the parts they know (creator_agent,
    prompt_used, etc.); this fills in safe defaults for anything missing
    so the sidecar shape is uniform across artifact types.
    """
    enriched: dict = {
        "primary_b2_key": primary_b2_key,
        "primary_local_path": local_path,
        "primary_size_bytes": (
            os.path.getsize(local_path) if os.path.exists(local_path) else 0
        ),
        "sidecar_written_at": time.time(),
        "run_id": get_run_id(),
        "creator_agent": "",
        "prompt_used": "",
        "qa_results_so_far": [],
        "validation_outcomes": [],
        "parent_artifact_refs": [],
    }
    enriched.update(meta or {})
    return enriched


def _upload_json_key(data: dict | list | str, key: str) -> bool:
    """Internal JSON upload — ``key`` is a run-relative path (run_id prefix added automatically)."""
    bucket = _get_bucket()
    if bucket is None:
        return False
    try:
        if isinstance(data, str):
            payload = data.encode("utf-8")
        else:
            payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        from b2sdk.v2 import UploadSourceBytes  # type: ignore[import-not-found]
        bucket.upload(UploadSourceBytes(payload), _b2_key(key))
        logger.info("B2 uploaded sidecar (%d bytes) -> %s", len(payload), _b2_key(key))
        return True
    except Exception as e:
        logger.error("B2 sidecar upload failed -> %s: %s", key, e)
        return False


def upload_file(
    local_path: str,
    b2_relative_path: str,
    meta: Optional[dict] = None,
) -> bool:
    """Upload a single file to B2 immediately, optionally with a _meta.json sidecar.

    Args:
        local_path: Absolute path to the local file.
        b2_relative_path: Path within the run directory (e.g. "audio/scene_001_V1_RU.wav").
        meta: Optional dict to write as a ``_meta.json`` sidecar alongside
            the primary file.  See :func:`upload_with_sidecar` for the
            canonical sidecar schema (creator_agent, prompt_used,
            qa_results_so_far, validation_outcomes, parent_artifact_refs).
            When provided, the sidecar upload is part of the same logical
            operation as the primary upload.

    Returns:
        True on success, False on failure (never raises).  When ``meta``
        is supplied, returns True only if BOTH primary and sidecar were
        uploaded (sidecar loss would break the paper trail for #70).
    """
    bucket = _get_bucket()
    if bucket is None:
        return False

    if not os.path.exists(local_path):
        logger.warning("B2 upload skipped -- file not found: %s", local_path)
        return False

    key = _b2_key(b2_relative_path)
    try:
        t0 = time.time()
        size = os.path.getsize(local_path)
        bucket.upload_local_file(
            local_file=local_path,
            file_name=key,
        )
        elapsed = time.time() - t0
        logger.info("B2 uploaded %s (%.1f MB, %.1fs) -> %s", local_path, size / 1e6, elapsed, key)
    except Exception as e:
        logger.error("B2 upload failed %s -> %s: %s", local_path, key, e)
        return False

    # Sidecar is written AFTER the primary so the primary is never
    # orphaned by a sidecar-only upload.  Failure to upload the sidecar
    # is a hard failure for this call — the #70 paper trail requires both.
    if meta is not None:
        sidecar_path = _sidecar_key(b2_relative_path)
        enriched = _build_sidecar(
            local_path=local_path,
            primary_b2_key=key,
            meta=meta,
        )
        ok = _upload_json_key(enriched, sidecar_path)
        if not ok:
            logger.error(
                "B2 primary uploaded but _meta.json sidecar FAILED for %s "
                "-- artifact paper trail broken (#70)",
                key,
            )
            return False

    return True


def upload_json(
    data: dict | list | str,
    b2_relative_path: str,
    meta: Optional[dict] = None,
) -> bool:
    """Upload JSON data directly to B2 (no local file needed).

    Args:
        data: Dict/list to serialise, or a pre-serialised JSON string.
        b2_relative_path: Path within the run directory.
        meta: Optional ``_meta.json`` sidecar (see :func:`upload_with_sidecar`).

    Returns:
        True on success.  When ``meta`` is supplied, returns True only
        if BOTH primary and sidecar uploaded (#70 paper trail).
    """
    bucket = _get_bucket()
    if bucket is None:
        return False

    key = _b2_key(b2_relative_path)
    try:
        if isinstance(data, str):
            payload = data.encode("utf-8")
        else:
            payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

        from b2sdk.v2 import UploadSourceBytes  # type: ignore[import-not-found]
        bucket.upload(UploadSourceBytes(payload), key)
        logger.info("B2 uploaded JSON (%d bytes) -> %s", len(payload), key)
    except Exception as e:
        logger.error("B2 upload JSON failed -> %s: %s", key, e)
        return False

    if meta is not None:
        sidecar_rel = _sidecar_key(b2_relative_path)
        enriched = _build_sidecar(
            local_path="",  # JSON upload has no local file
            primary_b2_key=key,
            meta=meta,
        )
        ok = _upload_json_key(enriched, sidecar_rel)
        if not ok:
            logger.error(
                "B2 JSON uploaded but _meta.json sidecar FAILED for %s "
                "-- artifact paper trail broken (#70)",
                key,
            )
            return False

    return True


def upload_stage_marker(stage: str) -> bool:
    """Mark a pipeline stage as complete in B2.

    Args:
        stage: Stage name (scenario, audio, visual_direction, production, assembly).
    """
    return upload_json(
        {"stage": stage, "completed_at": time.time()},
        f"stage_markers/{stage}.done",
    )


# ---------------------------------------------------------------------------
# Convenience uploaders for each artifact type
# ---------------------------------------------------------------------------

def upload_scenario(scenes_json: str, visual_style_json: str) -> bool:
    """Upload scenario output (scenes + visual_style) immediately.

    Returns True if both uploads succeeded.
    """
    ok1 = upload_json(scenes_json, "state/scenes.json")
    ok2 = upload_json(visual_style_json, "state/visual_style.json")
    logger.info("B2: scenario artifacts uploaded (ok=%s)", ok1 and ok2)
    return ok1 and ok2


def upload_tts_clip(
    wav_path: str,
    sidecar_path: str = "",
    meta: Optional[dict] = None,
) -> None:
    """Upload a TTS WAV file (and its text-hash sidecar) immediately.

    If ``meta`` is supplied, a canonical ``_meta.json`` sidecar is
    written next to the WAV in B2 (#70).  Expected shape:

        {
            "creator_agent": "audio_agent",
            "prompt_used": "<TTS prompt text>",
            "qa_results_so_far": [...],
            "validation_outcomes": [...],
            "parent_artifact_refs": ["state/scenes.json"],
        }
    """
    basename = os.path.basename(wav_path)
    upload_file(wav_path, f"audio/{basename}", meta=meta)
    if sidecar_path and os.path.exists(sidecar_path):
        # Text-hash sidecar is content-addressed; its own _meta would be
        # redundant.  It's tracked as a parent_artifact_ref on the WAV.
        upload_file(sidecar_path, f"audio/{os.path.basename(sidecar_path)}")


def upload_visual_concepts(visual_concepts_json: str) -> bool:
    """Upload visual direction output immediately.

    Returns True if the upload succeeded.
    """
    ok = upload_json(visual_concepts_json, "state/visual_concepts.json")
    logger.info("B2: visual concepts uploaded (ok=%s)", ok)
    return ok


def upload_timeline(otio_path: str, meta: Optional[dict] = None) -> None:
    """Upload OTIO timeline immediately.

    Pass ``meta`` to attach a ``_meta.json`` sidecar — useful for scene-
    level assembly artifacts (#84) so the paper trail records which
    clips + QA verdicts were current when the scene was assembled.
    """
    basename = os.path.basename(otio_path)
    upload_file(otio_path, f"timelines/{basename}", meta=meta)


def upload_gatekeeper_report(report: dict, stage: str) -> bool:
    """Upload a gatekeeper audit report to B2.

    Called AFTER all artifacts for the stage are already in B2,
    so the audit trail is: artifacts first, then validation verdict.

    Args:
        report: Structured audit report from gatekeeper.format_audit_report().
        stage: Pipeline stage name (audio, production, etc.).

    Returns:
        True on success.
    """
    ok = upload_json(report, f"gatekeeper/{stage}_audit.json")
    verdict = report.get("verdict", "UNKNOWN")
    rejects = report.get("rejects", 0)
    total = report.get("total_checks", 0)
    logger.info(
        "B2: gatekeeper audit uploaded for %s (verdict=%s, %d/%d checks, %d rejects)",
        stage, verdict, total, total, rejects,
    )
    return ok


def upload_final_output(local_path: str) -> bool:
    """Upload final documentary MP4.

    Returns True if the upload succeeded.
    """
    basename = os.path.basename(local_path)
    return upload_file(local_path, f"output/{basename}")


def upload_pipeline_state(state: dict) -> bool:
    """Upload full pipeline state snapshot.

    Returns True if the upload succeeded.
    """
    # Filter non-serialisable values
    serialisable = {}
    for k, v in state.items():
        try:
            json.dumps(v)
            serialisable[k] = v
        except (TypeError, ValueError):
            serialisable[k] = str(v)
    return upload_json(serialisable, "state/pipeline_state.json")


# ---------------------------------------------------------------------------
# Restore helpers -- download artifacts from B2 to local disk on restart
# ---------------------------------------------------------------------------


def _list_all_run_ids() -> list[str]:
    """List all run IDs in the B2 bucket by scanning for any sub-path.

    Detects runs by the first path component of every file in the bucket,
    so even partially-uploaded runs (no stage markers yet) are found.

    Returns a list of unique run_id strings (sorted, latest last).
    """
    bucket = _get_bucket()
    if bucket is None:
        return []

    try:
        seen_runs: list[str] = []
        for file_version, _ in bucket.ls("", recursive=True):
            fname = file_version.file_name
            # Extract the top-level directory as the run_id
            parts = fname.split("/", 1)
            if len(parts) >= 2:
                run_id = parts[0]
                if run_id and run_id not in seen_runs:
                    seen_runs.append(run_id)
        seen_runs.sort()
        return seen_runs
    except Exception as e:
        logger.error("B2 _list_all_run_ids failed: %s", e)
        return []


def _read_run_state(run_id: str) -> Optional[dict]:
    """Download and parse pipeline_state.json for a specific run.

    Returns the parsed dict, or None if unavailable.
    """
    bucket = _get_bucket()
    if bucket is None:
        return None

    key = f"{run_id}/state/pipeline_state.json"
    try:
        import io
        buffer = io.BytesIO()
        bucket.download_file_by_name(key).save(buffer)
        return json.loads(buffer.getvalue().decode("utf-8"))
    except Exception as exc:
        from maintainer import notify_maintainer
        notify_maintainer("b2_restore_state", str(exc), {"path": key})
        return None


def download_file(b2_key: str, local_path: str) -> bool:
    """Download a single file from B2 to a local path.

    Args:
        b2_key: Full B2 object key (e.g. "run_abc123/audio/scene_001.wav").
        local_path: Where to write the file on local disk.

    Returns:
        True on success, False on failure (never raises).
    """
    bucket = _get_bucket()
    if bucket is None:
        return False

    os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
    try:
        t0 = time.time()
        bucket.download_file_by_name(b2_key).save_to(local_path)
        elapsed = time.time() - t0
        size = os.path.getsize(local_path)
        logger.info(
            "B2 downloaded %s (%.1f MB, %.1fs) -> %s",
            b2_key, size / 1e6, elapsed, local_path,
        )
        return True
    except Exception as e:
        logger.error("B2 download failed %s -> %s: %s", b2_key, local_path, e)
        return False
