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
from pathlib import Path
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
        bucket_name = os.environ.get("B2_BUCKET_NAME", "cloudberry-documentary-v2")

        if not key_id or not app_key:
            logger.warning("B2_KEY_ID / B2_APPLICATION_KEY not set -- B2 checkpoint disabled")
            return None

        try:
            from b2sdk.v2 import B2Api, InMemoryAccountInfo
            info = InMemoryAccountInfo()
            _b2_api = B2Api(info)
            _b2_api.authorize_account("production", key_id, app_key)
            _b2_bucket = _b2_api.get_bucket_by_name(bucket_name)
            logger.info("B2 checkpoint enabled: bucket=%s", bucket_name)
        except Exception as e:
            logger.error("B2 checkpoint init failed: %s", e)
            _b2_bucket = None

    return _b2_bucket


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


def set_run_id(run_id: str) -> None:
    """Set the run ID explicitly (e.g. when restoring from B2)."""
    global _run_id
    _run_id = run_id
    os.environ["B2_RUN_ID"] = run_id


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------

def _b2_key(relative_path: str) -> str:
    """Build full B2 object key from run_id + relative path."""
    return f"{get_run_id()}/{relative_path}"


def upload_file(local_path: str, b2_relative_path: str) -> bool:
    """Upload a single file to B2 immediately.

    Args:
        local_path: Absolute path to the local file.
        b2_relative_path: Path within the run directory (e.g. "audio/scene_001_V1_RU.wav").

    Returns:
        True on success, False on failure (never raises).
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
        return True
    except Exception as e:
        logger.error("B2 upload failed %s -> %s: %s", local_path, key, e)
        return False


def upload_json(data: dict | list | str, b2_relative_path: str) -> bool:
    """Upload JSON data directly to B2 (no local file needed).

    Args:
        data: Dict/list to serialise, or a pre-serialised JSON string.
        b2_relative_path: Path within the run directory.

    Returns:
        True on success.
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

        from b2sdk.v2 import UploadSourceBytes
        bucket.upload(UploadSourceBytes(payload), key)
        logger.info("B2 uploaded JSON (%d bytes) -> %s", len(payload), key)
        return True
    except Exception as e:
        logger.error("B2 upload JSON failed -> %s: %s", key, e)
        return False


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


def upload_tts_clip(wav_path: str, sidecar_path: str = "") -> None:
    """Upload a TTS WAV file (and its text-hash sidecar) immediately."""
    basename = os.path.basename(wav_path)
    upload_file(wav_path, f"audio/{basename}")
    if sidecar_path and os.path.exists(sidecar_path):
        upload_file(sidecar_path, f"audio/{os.path.basename(sidecar_path)}")


def upload_visual_concepts(visual_concepts_json: str) -> bool:
    """Upload visual direction output immediately.

    Returns True if the upload succeeded.
    """
    ok = upload_json(visual_concepts_json, "state/visual_concepts.json")
    logger.info("B2: visual concepts uploaded (ok=%s)", ok)
    return ok


def upload_video_clip(mp4_path: str, status_path: str = "") -> None:
    """Upload a video clip (and its QA status file) immediately."""
    basename = os.path.basename(mp4_path)
    upload_file(mp4_path, f"video/{basename}")
    if status_path and os.path.exists(status_path):
        upload_file(status_path, f"video/{os.path.basename(status_path)}")


def upload_timeline(otio_path: str) -> None:
    """Upload OTIO timeline immediately."""
    basename = os.path.basename(otio_path)
    upload_file(otio_path, f"timelines/{basename}")


def upload_assembly_file(local_path: str) -> None:
    """Upload an assembly intermediate file."""
    basename = os.path.basename(local_path)
    upload_file(local_path, f"assembly/{basename}")


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

def check_stage_complete(stage: str) -> bool:
    """Check if a pipeline stage was already completed in B2.

    Returns True if the stage marker exists in B2.
    """
    bucket = _get_bucket()
    if bucket is None:
        return False

    # List the stage_markers/ folder and look for the specific .done file.
    # NOTE: bucket.ls() treats its argument as a *folder prefix*, not an exact
    # key.  Passing the full filename (e.g. "run/stage_markers/audio.done")
    # returns nothing because B2 looks for files *inside* that "folder".
    target_name = f"stage_markers/{stage}.done"
    prefix = _b2_key("stage_markers/")
    target_key = _b2_key(target_name)
    try:
        for file_version, _ in bucket.ls(prefix):
            if file_version.file_name == target_key:
                return True
        return False
    except Exception:
        return False


def restore_state_json(b2_relative_path: str) -> Optional[str]:
    """Download a JSON file from B2 and return its contents as a string.

    Returns None if the file doesn't exist or download fails.
    """
    bucket = _get_bucket()
    if bucket is None:
        return None

    key = _b2_key(b2_relative_path)
    try:
        import io
        buffer = io.BytesIO()
        bucket.download_file_by_name(key).save(buffer)
        return buffer.getvalue().decode("utf-8")
    except Exception:
        return None


def restore_directory(b2_prefix: str, local_dir: str) -> int:
    """Download all files under a B2 prefix to a local directory.

    Args:
        b2_prefix: Prefix within the run directory (e.g. "audio/").
        local_dir: Local directory to write files to.

    Returns:
        Number of files restored.
    """
    bucket = _get_bucket()
    if bucket is None:
        return 0

    full_prefix = _b2_key(b2_prefix)
    os.makedirs(local_dir, exist_ok=True)
    count = 0

    try:
        for file_version, _ in bucket.ls(full_prefix, recursive=True):
            fname = file_version.file_name
            # Strip the run_id prefix to get the relative path
            rel = fname[len(get_run_id()) + 1:]  # +1 for the /
            # Get just the filename (strip the b2_prefix part)
            if rel.startswith(b2_prefix):
                local_name = rel[len(b2_prefix):]
            else:
                local_name = os.path.basename(rel)

            local_path = os.path.join(local_dir, local_name)
            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)

            if os.path.exists(local_path) and os.path.getsize(local_path) == file_version.size:
                logger.debug("B2 restore skip (already exists, same size): %s", local_path)
                count += 1
                continue

            try:
                bucket.download_file_by_name(fname).save_to(local_path)
                count += 1
                logger.info("B2 restored %s -> %s", fname, local_path)
            except Exception as e:
                logger.warning("B2 restore failed %s: %s", fname, e)
    except Exception as e:
        logger.error("B2 restore_directory failed for prefix %s: %s", full_prefix, e)

    return count


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
    except Exception:
        return None


def find_latest_run_id(topic: str = "") -> Optional[str]:
    """Find the most recent run ID in the B2 bucket that matches the topic.

    Content-addressable: scans every run's stored pipeline_state.json for a
    matching ``topic`` field.  Falls back to prefix matching on the run ID
    when the state file is missing or unreadable.  This means the pipeline
    can resume from *any* prior run about the same topic regardless of how
    the run_id was generated.

    Returns:
        The run_id string, or None if no matching runs found.
    """
    if not topic:
        return None

    all_runs = _list_all_run_ids()
    if not all_runs:
        return None

    safe_topic = "".join(c if c.isalnum() else "_" for c in topic.lower())[:30]

    # Pass 1 — content-addressable: check stored topic in pipeline_state.json
    content_matches: list[str] = []
    prefix_matches: list[str] = []
    for rid in all_runs:
        # Quick prefix check (cheap, no download)
        if safe_topic and rid.startswith(safe_topic):
            prefix_matches.append(rid)

        # Deep content check (downloads state file)
        state = _read_run_state(rid)
        if state and state.get("topic", "").strip().lower() == topic.strip().lower():
            content_matches.append(rid)
            logger.info("B2: run '%s' matches topic '%s' (content match)", rid, topic)

    # Prefer content matches, fall back to prefix matches
    matches = content_matches or prefix_matches
    if not matches:
        logger.info("B2: no run matches topic '%s' (checked %d runs)", topic, len(all_runs))
        return None

    # Return the latest match
    matches.sort()
    best = matches[-1]
    match_type = "content" if best in content_matches else "prefix"
    logger.info("B2: selected run '%s' for topic '%s' (%s match)", best, topic, match_type)
    return best


def restore_pipeline(topic: str, pipeline_base: str = "/tmp/documentary-pipeline") -> dict:
    """Restore full pipeline state from B2 for a given topic.

    Checks B2 for the latest run with this topic, downloads all artifacts
    to the local pipeline directories, and returns info about which stages
    are already complete.

    Args:
        topic: Documentary topic (used to find the matching run).
        pipeline_base: Local base directory for pipeline artifacts.

    Returns:
        Dict with:
            - run_id: The restored run ID (or empty if nothing found)
            - stages_complete: List of completed stage names
            - restored_files: Total number of files restored
            - state: Restored pipeline state dict (or empty)
    """
    result = {
        "run_id": "",
        "stages_complete": [],
        "restored_files": 0,
        "state": {},
    }

    run_id = find_latest_run_id(topic)
    if not run_id:
        logger.info("B2: no previous run found for topic '%s'", topic)
        return result

    logger.info("B2: found previous run '%s', restoring...", run_id)
    set_run_id(run_id)
    result["run_id"] = run_id

    # Check which stages are complete
    for stage in ("scenario", "audio", "visual_direction", "production", "assembly"):
        if check_stage_complete(stage):
            result["stages_complete"].append(stage)
            logger.info("B2: stage '%s' already complete", stage)

    # Restore pipeline state
    state_json = restore_state_json("state/pipeline_state.json")
    if state_json:
        try:
            result["state"] = json.loads(state_json)
            logger.info("B2: restored pipeline state (%d keys)", len(result["state"]))
        except json.JSONDecodeError:
            pass

    # Restore individual state files
    for name in ("scenes", "visual_style", "visual_concepts"):
        content = restore_state_json(f"state/{name}.json")
        if content:
            result["state"][name] = content
            logger.info("B2: restored state/%s.json", name)

    # Restore directories
    dirs_to_restore = {
        "audio/": os.path.join(pipeline_base, "audio"),
        "video/": os.path.join(pipeline_base, "video"),
        "timelines/": os.path.join(pipeline_base, "timelines"),
        "assembly/": os.path.join(pipeline_base, "assembly"),
        "output/": os.path.join(pipeline_base, "output"),
    }

    total = 0
    for b2_prefix, local_dir in dirs_to_restore.items():
        n = restore_directory(b2_prefix, local_dir)
        total += n
        if n > 0:
            logger.info("B2: restored %d files from %s -> %s", n, b2_prefix, local_dir)

    result["restored_files"] = total
    logger.info(
        "B2 restore complete: run_id=%s, stages=%s, files=%d",
        run_id, result["stages_complete"], total,
    )
    return result
