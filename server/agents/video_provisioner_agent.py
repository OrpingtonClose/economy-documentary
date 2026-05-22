"""
VideoProvisionerAgent — merged provisioner + video agent.

Owns rendering end-to-end: visual planning, allocate GPU workers,
render clips, evaluate quality, scale fleet.

Architecture:
    The agent uses raw LLM reasoning with vast CLI output + Letta-based
    memory.  No hardcoded model registries.  The agent searches Vast.ai,
    reads the results, and reasons about what GPU to pick.  It remembers
    past decisions in memory.

    Phase-based operation:
    1. SURVEY — Read OTIO timeline, find gaps in V1_Video track
    2. VISUAL PLANNING — Generate LTX-2.3 prompts for each gap
    3. INFRASTRUCTURE — Provision GPU VMs on Vast.ai
    4. PRODUCTION — Render clips on GPU workers
    5. CLEANUP — Validate timeline, destroy VMs

All OTIO operations use stateless file primitives (tools.otio_file_ops,
tools.otio_metadata, tools.otio_lifecycle) — no agent-to-agent delegation.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

import opentimelineio as otio

from recovery_agents import AgentTool, RecoveryAgent
from tools.otio_file_ops import (
    resolve_timeline_path,
    otio_read,
    otio_read_modify_write,
    TRACK_V1,
    TRACK_A1,
)
from tools.otio_metadata import read_pipeline_metadata, write_pipeline_metadata
from tools.otio_lifecycle import guard_mutation, get_otio_lifecycle_state

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# 1. OTIO reading tools
# ═══════════════════════════════════════════════════════════════════════════

def _tool_read_timeline() -> str:
    """Read the full OTIO timeline structure."""
    try:
        tp = resolve_timeline_path()
        timeline = otio_read(tp)

        tracks_info = []
        for track in timeline.tracks:
            items = []
            for item in track:
                entry = {
                    "name": item.name,
                    "type": type(item).__name__,
                }
                if isinstance(item, otio.schema.Clip) and item.source_range:
                    entry["duration_sec"] = round(
                        item.source_range.duration.to_seconds(), 3
                    )
                    if item.media_reference and hasattr(
                        item.media_reference, "target_url"
                    ):
                        entry["media"] = item.media_reference.target_url
                elif isinstance(item, otio.schema.Gap) and item.source_range:
                    entry["duration_sec"] = round(
                        item.source_range.duration.to_seconds(), 3
                    )
                doc_meta = item.metadata.get("documentary", {})
                if doc_meta:
                    entry["metadata"] = dict(doc_meta)
                items.append(entry)

            total_dur = sum(
                i.source_range.duration.to_seconds()
                for i in track
                if i.source_range
            )
            tracks_info.append({
                "name": track.name,
                "kind": str(track.kind),
                "items": items,
                "total_duration_sec": round(total_dur, 3),
            })

        doc_meta = timeline.metadata.get("documentary", {})
        return json.dumps({
            "name": timeline.name,
            "state": doc_meta.get("state", "unknown"),
            "tracks": tracks_info,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_read_pipeline_data(key: str) -> str:
    """Read pipeline metadata from the OTIO timeline."""
    try:
        tp = resolve_timeline_path()
        value = read_pipeline_metadata(tp, key)
        if value is None:
            return json.dumps({"error": f"Key '{key}' not found in pipeline metadata"})
        return json.dumps({"key": key, "value": value})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_get_scene_durations() -> str:
    """Get per-scene duration budgets."""
    try:
        tp = resolve_timeline_path()
        timeline = otio_read(tp)

        scenes: dict = {}
        for track in timeline.tracks:
            for item in track:
                doc_meta = item.metadata.get("documentary", {})
                scene_num = doc_meta.get("scene_num", 0)
                if not scene_num:
                    continue
                if scene_num not in scenes:
                    scenes[scene_num] = {
                        "narration_sec": 0,
                        "video_sec": 0,
                        "narration_clips": 0,
                        "video_clips": 0,
                    }
                dur = 0.0
                if item.source_range:
                    dur = item.source_range.duration.to_seconds()
                if track.name == TRACK_A1 and isinstance(item, otio.schema.Clip):
                    scenes[scene_num]["narration_sec"] += dur
                    scenes[scene_num]["narration_clips"] += 1
                elif track.name == TRACK_V1:
                    if isinstance(item, otio.schema.Clip):
                        scenes[scene_num]["video_sec"] += dur
                        scenes[scene_num]["video_clips"] += 1
                    elif isinstance(item, otio.schema.Gap) and dur > 0:
                        scenes[scene_num]["video_sec"] += dur

        return json.dumps({"scenes": scenes}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_find_video_gaps() -> str:
    """Scan V1_Video track for Gaps (missing video).

    Cross-references with visual_concepts metadata.  Returns a JSON list
    of render jobs.  Each gap has: scene_num, phrase_idx, duration,
    status (needs_visual_concept or needs_render).
    """
    try:
        tp = resolve_timeline_path()
        timeline = otio_read(tp)

        # Read visual_concepts metadata
        visual_concepts: dict = {}
        try:
            vc_value = read_pipeline_metadata(tp, "visual_concepts")
            if vc_value is not None:
                if isinstance(vc_value, dict):
                    visual_concepts = vc_value
                elif isinstance(vc_value, list):
                    visual_concepts = {
                        f"{v.get('scene_num', '')}-{v.get('phrase_idx', '')}": v
                        for v in vc_value
                        if isinstance(v, dict)
                    }
        except Exception:
            pass  # No visual concepts yet — all gaps need planning

        gaps = []
        for track in timeline.tracks:
            if track.name != TRACK_V1:
                continue
            for item in track:
                if not isinstance(item, otio.schema.Gap):
                    continue
                if not item.source_range:
                    continue
                dur = item.source_range.duration.to_seconds()
                if dur < 0.1:
                    continue  # skip zero-duration gaps

                doc_meta = item.metadata.get("documentary", {})
                scene_num = doc_meta.get("scene_num", 0)
                phrase_idx = doc_meta.get("phrase_idx", 0)

                if not scene_num:
                    continue  # unlabelled gap — skip

                # Check if visual_concepts already cover this gap
                vc_key = f"{scene_num}-{phrase_idx}"
                has_concept = vc_key in visual_concepts

                status = "needs_render" if has_concept else "needs_visual_concept"
                gaps.append({
                    "scene_num": scene_num,
                    "phrase_idx": phrase_idx,
                    "duration": round(dur, 3),
                    "status": status,
                })

        return json.dumps({
            "total_gaps": len(gaps),
            "needs_visual_concept": sum(1 for g in gaps if g["status"] == "needs_visual_concept"),
            "needs_render": sum(1 for g in gaps if g["status"] == "needs_render"),
            "gaps": gaps,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ═══════════════════════════════════════════════════════════════════════════
# 2. OTIO writing tools
# ═══════════════════════════════════════════════════════════════════════════

def _tool_write_pipeline_data(key: str, value_json: str, provenance_json: str = "{}") -> str:
    """Write pipeline metadata to the OTIO timeline with provenance."""
    try:
        tp = resolve_timeline_path()
        value = json.loads(value_json)
        provenance = json.loads(provenance_json) if provenance_json else None
        return write_pipeline_metadata(tp, key, value, provenance)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_add_clip(track: str, scene_num: int, phrase_idx: int,
                   clip_path: str, duration: float,
                   provenance_json: str = "{}") -> str:
    """Add a clip to the OTIO timeline with provenance."""
    try:
        tp = resolve_timeline_path()

        # Mutation guard — raises OtioStateViolation if blocked.
        guard_mutation(tp, operation="add_clip", allow_escalation=True)

        try:
            provenance = json.loads(provenance_json) if provenance_json else {}
        except (json.JSONDecodeError, TypeError):
            provenance = {}

        def _mutate(timeline: otio.schema.Timeline) -> dict:
            # Find the target track
            target_track = None
            for t in timeline.tracks:
                if t.name == track:
                    target_track = t
                    break

            if target_track is None:
                return {"error": f"Track '{track}' not found"}

            # Build clip name
            clip_name = f"scene_{scene_num:03d}_phrase_{phrase_idx:03d}"

            # Idempotency: skip if clip already exists
            for item in target_track:
                if isinstance(item, otio.schema.Clip) and item.name == clip_name:
                    return {
                        "status": "already_exists",
                        "clip_name": clip_name,
                        "message": "Clip already exists, skipping duplicate",
                    }

            # Create the clip
            clip = otio.schema.Clip(
                name=clip_name,
                media_reference=otio.schema.ExternalReference(
                    target_url=clip_path,
                ),
                source_range=otio.opentime.TimeRange(
                    start_time=otio.opentime.RationalTime(0, 24),
                    duration=otio.opentime.RationalTime(duration * 24, 24),
                ),
            )
            clip.metadata["documentary"] = {
                "scene_num": scene_num,
                "phrase_idx": phrase_idx,
                "type": "clip",
            }
            if provenance:
                clip.metadata["documentary"]["provenance"] = provenance

            target_track.append(clip)
            return {
                "added": True,
                "track": track,
                "scene_num": scene_num,
                "phrase_idx": phrase_idx,
            }

        result = otio_read_modify_write(tp, _mutate)
        return json.dumps(result)
    except Exception as e:
        from tools.otio_lifecycle import OtioStateViolation
        if isinstance(e, OtioStateViolation):
            return json.dumps({
                "error": f"Mutation blocked: {e}",
                "hint": "Open a REPLACE/EXTEND escalation first",
            })
        return json.dumps({"error": str(e)})


def _tool_validate_timeline(phase: str) -> str:
    """Validate timeline structural integrity for a given pipeline phase."""
    try:
        tp = resolve_timeline_path()
        timeline = otio_read(tp)

        from callbacks.timeline_guardian import _VALIDATORS

        validator = _VALIDATORS.get(phase)
        if not validator:
            return json.dumps({
                "valid": False,
                "error": f"Unknown phase: {phase}",
            })

        # Validators take (timeline, state) but in the stateless world
        # we pass an empty dict — the file is the source of truth.
        error = validator(timeline, {})
        if error:
            return json.dumps({"valid": False, "phase": phase, "errors": error})

        return json.dumps({
            "valid": True,
            "phase": phase,
            "message": "All checks passed",
        })
    except Exception as e:
        return json.dumps({"valid": False, "error": str(e)})


def _tool_rebalance_durations(adjustments_json: str, reason: str) -> str:
    """Rebalance scene duration budgets."""
    try:
        tp = resolve_timeline_path()

        # Mutation guard — raises OtioStateViolation if blocked.
        try:
            guard_mutation(
                tp,
                operation="rebalance_durations",
                allow_escalation=True,
            )
        except Exception as e:
            from tools.otio_lifecycle import OtioStateViolation
            if isinstance(e, OtioStateViolation):
                return json.dumps({
                    "success": False,
                    "error": f"Mutation blocked: {e}",
                    "hint": "Open a REPLACE/EXTEND escalation first",
                })
            raise

        adjustments = json.loads(adjustments_json)
        if not isinstance(adjustments, dict):
            return json.dumps({
                "success": False,
                "error": "adjustments must be a JSON dict {scene_num: duration_sec}",
            })

        def _mutate(timeline: otio.schema.Timeline) -> dict:
            # Calculate total before
            total_before = 0.0
            for track in timeline.tracks:
                if track.name == TRACK_V1:
                    for item in track:
                        if item.source_range:
                            total_before += item.source_range.duration.to_seconds()

            # Apply adjustments
            applied = {}
            for track in timeline.tracks:
                if track.name != TRACK_V1:
                    continue
                for item in track:
                    doc_meta = item.metadata.get("documentary", {})
                    scene_num = doc_meta.get("scene_num", 0)
                    if scene_num and str(scene_num) in adjustments:
                        new_dur = float(adjustments[str(scene_num)])
                        if new_dur < 2.0:
                            return {
                                "success": False,
                                "error": (
                                    f"Scene {scene_num}: duration "
                                    f"{new_dur}s below minimum 2.0s"
                                ),
                            }
                        if item.source_range:
                            old_dur = item.source_range.duration.to_seconds()
                            item.source_range = otio.opentime.TimeRange(
                                start_time=otio.opentime.RationalTime(0, 24),
                                duration=otio.opentime.RationalTime.from_seconds(
                                    new_dur, 24
                                ),
                            )
                            applied[scene_num] = {
                                "old_dur": round(old_dur, 3),
                                "new_dur": round(new_dur, 3),
                            }

            # Calculate total after
            total_after = 0.0
            for track in timeline.tracks:
                if track.name == TRACK_V1:
                    for item in track:
                        if item.source_range:
                            total_after += item.source_range.duration.to_seconds()

            # Record rebalance history in metadata
            doc_meta = timeline.metadata.setdefault("documentary", {})
            doc_meta.setdefault("rebalance_history", []).append({
                "adjustments": adjustments,
                "reason": reason,
                "applied": applied,
                "total_before": round(total_before, 3),
                "total_after": round(total_after, 3),
            })

            return {
                "applied": applied,
                "total_before": round(total_before, 3),
                "total_after": round(total_after, 3),
            }

        result = otio_read_modify_write(tp, _mutate)

        # If the mutation returned a failure (e.g. below-minimum duration),
        # propagate it directly.
        if result.get("success") is False:
            return json.dumps(result)

        return json.dumps({
            "success": True,
            "applied": result.get("applied", {}),
            "total_before": result.get("total_before", 0),
            "total_after": result.get("total_after", 0),
            "conservation_check": (
                "PASS"
                if abs(result.get("total_before", 0) - result.get("total_after", 0)) < 0.1
                else "FAIL"
            ),
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ═══════════════════════════════════════════════════════════════════════════
# 3. Visual planning tools
# ═══════════════════════════════════════════════════════════════════════════

def _tool_query_lora_catalog(content_type: str = "", mood: str = "") -> str:
    """Query the LoRA catalog for matching styles."""
    try:
        from tools.lora_tools import query_lora_catalog as _query
        return _query(content_type=content_type, mood=mood)
    except ImportError:
        return json.dumps({"error": "lora_tools not available (ADK not installed)"})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_get_lora_details(lora_id: str) -> str:
    """Get full details for a specific LoRA entry."""
    try:
        from tools.lora_tools import get_lora_details as _details
        return _details(lora_id=lora_id)
    except ImportError:
        return json.dumps({"error": "lora_tools not available (ADK not installed)"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ═══════════════════════════════════════════════════════════════════════════
# 4. Provisioning tools (thin wrappers around vastai CLI)
# ═══════════════════════════════════════════════════════════════════════════

def _tool_search_gpu_offers(query: str) -> str:
    """Search Vast.ai for available GPU offers.

    Runs `vastai search offers` with the given query string and returns
    raw JSON.  The agent reads the results and reasons about what GPU
    to pick — no hardcoded model registries.

    Args:
        query: Vast.ai search query string (e.g. "gpu_ram>=48 dph_total<=5.00 rentable=true")
    """
    try:
        from worker_provisioner import _vast_cmd
        result = _vast_cmd([
            "search", "offers",
            "--type", "on-demand",
            "--order", "inet_down-",
            "--raw",
            query,
        ])
        if isinstance(result, list):
            # Format for readability
            offers = []
            for o in result[:30]:
                offers.append({
                    "id": int(o.get("id", 0)),
                    "gpu_name": o.get("gpu_name", "unknown"),
                    "gpu_ram_gb": round(float(o.get("gpu_ram", 0)) / 1024, 1),
                    "num_gpus": o.get("num_gpus", 1),
                    "dph_total": round(float(o.get("dph_total", 0)), 4),
                    "disk_space_gb": round(float(o.get("disk_space", 0)), 0),
                    "inet_down": round(float(o.get("inet_down", 0)), 0),
                    "inet_up": round(float(o.get("inet_up", 0)), 0),
                    "reliability": round(float(o.get("reliability", 0)), 3),
                    "rentable": o.get("rentable", False),
                    "verified": o.get("verified", False),
                    "country": o.get("country", ""),
                })
            return json.dumps({
                "query": query,
                "total_offers": len(result),
                "offers_returned": len(offers),
                "offers": offers,
            }, indent=2)
        return json.dumps({"query": query, "raw_result": str(result)[:2000]})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_provision_vm(offer_id: int, disk_gb: int = 224,
                       worker_mode: str = "ltx",
                       docker_image: str = "",
                       env_vars_json: str = "{}") -> str:
    """Provision a Vast.ai GPU VM from a specific offer.

    Runs `vastai create instance`.  The agent picks the offer_id from
    search_gpu_offers results.

    Args:
        offer_id: Offer ID from search_gpu_offers.
        disk_gb: Disk size in GB.  Enforced minimums: tts=64, ltx=300.
        worker_mode: Worker mode for gpu_worker.py (tts, ltx, both).
        docker_image: Docker image to use.  Empty = auto-resolve from manifest.
        env_vars_json: JSON dict of additional env vars to set on the VM.
    """
    # Enforce disk minimums — Vast.ai offer metadata is unreliable about
    # actual disk size, and model downloads (especially LTX-2.3) need
    # far more disk than the offer specs suggest.
    DISK_MINIMUMS = {"tts": 64, "ltx": 150, "both": 150}
    min_disk = DISK_MINIMUMS.get(worker_mode, 150)
    if disk_gb < min_disk:
        logger.warning(
            "provision_vm: disk_gb=%d below minimum %d for %s — raising to minimum",
            disk_gb, min_disk, worker_mode,
        )
        disk_gb = min_disk

    try:
        from worker_provisioner import (
            _vast_cmd,
            _HEALTH_CONTROL_PORT,
            normalize_worker_mode,
            resolve_docker_image,
        )
        import shlex
        import subprocess
        import uuid

        worker_mode = normalize_worker_mode(worker_mode)

        if not docker_image:
            docker_image, _torch_index = resolve_docker_image(worker_mode)

        # Build onstart script
        b2_key_id = os.environ.get("B2_KEY_ID", "")
        b2_app_key = os.environ.get("B2_APPLICATION_KEY", "")
        dashscope_key = os.environ.get("DASHSCOPE_API_KEY", "")
        openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")

        try:
            _branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=os.path.dirname(os.path.abspath(__file__)),
                text=True,
            ).strip()
            if not _branch or _branch == "HEAD":
                _branch = "main"
        except Exception:
            _branch = "main"

        # Parse additional env vars
        extra_env = {}
        try:
            extra_env = json.loads(env_vars_json) if env_vars_json else {}
        except (json.JSONDecodeError, TypeError):
            pass

        remote_port = 8880
        onstart = (
            f"export B2_KEY_ID={shlex.quote(b2_key_id)} && "
            f"export B2_APPLICATION_KEY={shlex.quote(b2_app_key)} && "
            f"export WORKER_MODE={shlex.quote(worker_mode)} && "
            f"export DASHSCOPE_API_KEY={shlex.quote(dashscope_key)} && "
            f"export OPENROUTER_API_KEY={shlex.quote(openrouter_key)} && "
            + "".join(
                f"export {k}={shlex.quote(str(v))} && "
                for k, v in extra_env.items()
            )
            + "export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True && "
            "apt-get update && apt-get install -y git curl ffmpeg libsndfile1 sox libsox-dev && "
            f"(git clone -b {shlex.quote(_branch)} --single-branch "
            "https://github.com/OrpingtonClose/economy-documentary.git "
            "/workspace/economy-documentary 2>&1 || "
            "git clone -b main --single-branch "
            "https://github.com/OrpingtonClose/economy-documentary.git "
            "/workspace/economy-documentary 2>&1 || "
            f"(cd /workspace/economy-documentary && git fetch origin {shlex.quote(_branch)} && "
            f"git checkout {shlex.quote(_branch)} && git pull origin {shlex.quote(_branch)})) && "
            "python3 -c 'import torch; print(f\"torch {torch.__version__} from {torch.__file__}\")' && "
            "pip install --break-system-packages --no-cache-dir "
            "'fastapi>=0.100.0' 'uvicorn>=0.20.0' 'pydantic>=2.0.0' "
            "'numpy>=1.26.0,<2.0.0' 'soundfile>=0.12.0' && "
            "python3 /workspace/economy-documentary/scripts/gpu_worker.py "
            f"--mode {shlex.quote(worker_mode)} --port {remote_port}"
        )

        _run_id = os.environ.get("DOCUMENTARY_RUN_ID", uuid.uuid4().hex[:8])
        _label = f"documentary-video-{_run_id}"

        _env_ports = (
            f"-p {remote_port}:{remote_port} "
            f"-p {_HEALTH_CONTROL_PORT}:{_HEALTH_CONTROL_PORT}"
        )

        create_result = _vast_cmd([
            "create", "instance",
            str(offer_id),
            "--image", docker_image,
            "--disk", str(disk_gb),
            "--ssh",
            "--direct",
            "--env", _env_ports,
            "--label", _label,
            "--onstart-cmd", onstart,
        ])

        # Parse response
        vm_id = None
        if isinstance(create_result, dict):
            vm_id = create_result.get("new_contract")
        elif isinstance(create_result, str) and "new_contract" in create_result:
            import re
            match = re.search(r"'new_contract'\s*:\s*(\d+)", create_result)
            if match:
                vm_id = match.group(1)

        if vm_id:
            try:
                from tools.vastai_tools import register_owned_vm
                register_owned_vm(str(vm_id))
            except Exception:
                pass
            return json.dumps({
                "status": "created",
                "offer_id": offer_id,
                "vm_id": str(vm_id),
                "docker_image": docker_image,
                "worker_mode": worker_mode,
            })

        return json.dumps({
            "status": "error",
            "offer_id": offer_id,
            "error": f"Unexpected response: {create_result}",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# Rate limiter for health checks — prevents tight polling loops
_check_vm_last_call: dict[str, float] = {}
_CHECK_VM_MIN_INTERVAL = 10.0  # seconds between calls per VM


def _tool_check_vm_status(vm_id: str) -> str:
    """Check a Vast.ai VM's status, connection details, and health.

    Runs `vastai show instance`. Rate-limited: returns a "wait" response
    if called more than once per 10 seconds for the same VM.

    Args:
        vm_id: The instance ID from provision_vm.
    """
    import time as _time
    now = _time.monotonic()
    last = _check_vm_last_call.get(vm_id, 0)
    if now - last < _CHECK_VM_MIN_INTERVAL:
        return json.dumps({
            "vm_id": vm_id,
            "status": "rate_limited",
            "message": f"Called too soon — wait {_CHECK_VM_MIN_INTERVAL - (now - last):.0f}s. Use exponential backoff: 10s, 30s, 60s, 120s.",
        })
    _check_vm_last_call[vm_id] = now
    try:
        from worker_provisioner import _vast_cmd
        result = _vast_cmd(["show", "instance", vm_id, "--raw"])
        if isinstance(result, dict):
            actual_status = result.get("actual_status", "unknown")
            public_ipaddr = result.get("public_ipaddr", "")
            ports = result.get("ports", {}) or {}
            direct_port = 0
            port_key = "8880/tcp"
            if port_key in ports:
                bindings = ports[port_key]
                if isinstance(bindings, list) and bindings:
                    direct_port = int(bindings[0].get("HostPort", 0))
                elif isinstance(bindings, (int, str)):
                    direct_port = int(bindings)

            # Try worker endpoint
            health_text = None
            if actual_status == "running" and public_ipaddr and direct_port:
                try:
                    health_url = f"http://{public_ipaddr}:{direct_port}/"
                    req = Request(health_url)
                    with urlopen(req, timeout=10) as resp:
                        health_text = resp.read().decode().strip()
                except Exception as e:
                    health_text = f"error: {e}"

            return json.dumps({
                "vm_id": vm_id,
                "status": actual_status,
                "public_ipaddr": public_ipaddr,
                "direct_port": direct_port,
                "ssh_host": result.get("ssh_host", ""),
                "ssh_port": result.get("ssh_port", 0),
                "health_text": health_text,
            }, indent=2)
        return json.dumps({"vm_id": vm_id, "raw": str(result)[:500]})
    except Exception as e:
        return json.dumps({"error": str(e)})


_check_health_last_call: dict[str, float] = {}
_CHECK_HEALTH_MIN_INTERVAL = 10.0


def _tool_check_worker_health(url: str, capability: str = "ltx") -> str:
    """HTTP GET to a worker's / endpoint. Rate-limited per URL.

    Args:
        url: Worker URL (e.g. "http://1.2.3.4:8880").
        capability: Capability to check (e.g. "ltx", "tts").
    """
    import time as _time
    now = _time.monotonic()
    last = _check_health_last_call.get(url, 0)
    if now - last < _CHECK_HEALTH_MIN_INTERVAL:
        return json.dumps({
            "url": url,
            "healthy": False,
            "rate_limited": True,
            "message": f"Called too soon — wait {_CHECK_HEALTH_MIN_INTERVAL - (now - last):.0f}s. Use exponential backoff: 10s, 30s, 60s, 120s.",
        })
    _check_health_last_call[url] = now
    try:
        from worker_provisioner import check_worker_health
        healthy = check_worker_health(url, capability)
        # Also get raw health text
        health_url = f"{url.rstrip('/')}/"
        try:
            req = Request(health_url)
            with urlopen(req, timeout=10) as resp:
                text = resp.read().decode().strip()
                return json.dumps({
                    "url": url,
                    "healthy": healthy,
                    "health_text": text,
                }, indent=2)
        except Exception as e:
            logger.warning("Health text fetch failed for %s: %s", url, e)
            return json.dumps({"url": url, "healthy": healthy})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_terminate_vm(vm_id: str) -> str:
    """Destroy a Vast.ai VM instance.

    Runs `vastai destroy instance`.

    Args:
        vm_id: The instance ID to terminate.
    """
    try:
        from worker_provisioner import _vast_cmd
        result = _vast_cmd(["destroy", "instance", "--yes", vm_id])
        return json.dumps({
            "vm_id": vm_id,
            "status": "destroyed",
            "raw_result": str(result)[:500],
        })
    except Exception as e:
        return json.dumps({"error": str(e), "vm_id": vm_id})


def _tool_list_active_vms() -> str:
    """List all active Vast.ai VM instances.

    Runs `vastai show instances --raw`.
    """
    try:
        from worker_provisioner import _vast_cmd
        result = _vast_cmd(["show", "instances", "--raw"])
        if isinstance(result, list):
            vms = []
            for v in result:
                vms.append({
                    "id": str(v.get("id", "")),
                    "label": v.get("label", ""),
                    "actual_status": v.get("actual_status", "unknown"),
                    "gpu_name": v.get("gpu_name", ""),
                    "public_ipaddr": v.get("public_ipaddr", ""),
                    "dph_total": round(float(v.get("dph_total", 0)), 4),
                })
            return json.dumps({"total": len(vms), "instances": vms}, indent=2)
        return json.dumps({"raw_result": str(result)[:2000]})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_get_account_credits() -> str:
    """Query Vast.ai account and return available credit balance.

    Runs `vastai show user --raw`.
    """
    try:
        from worker_provisioner import get_account_credits
        credits = get_account_credits()
        return json.dumps({"credits_usd": round(credits, 2)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ═══════════════════════════════════════════════════════════════════════════
# 5. Production tools
# ═══════════════════════════════════════════════════════════════════════════

def _tool_render_clip(scene_num: int, phrase_idx: int, prompt: str,
                      negative_prompt: str = "", duration: float = 5.0,
                      lora_id: str = "", worker_url: str = "") -> str:
    """Submit a video clip render job to a GPU worker.

    HTTP POST to the worker's / endpoint.  Accepts
    worker_url explicitly — the agent decides which worker to use.

    Args:
        scene_num: Scene number.
        phrase_idx: Phrase index within scene.
        prompt: LTX-2.3 video prompt.
        negative_prompt: Negative prompt for generation.
        duration: Target duration in seconds.
        lora_id: LoRA style identifier.
        worker_url: URL of the GPU worker (e.g. "http://1.2.3.4:8880").
    """
    if not worker_url:
        return json.dumps({
            "error": "No worker_url provided. Use search_gpu_offers + provision_vm first.",
            "scene_num": scene_num,
            "phrase_idx": phrase_idx,
        })

    try:
        req = Request(
            f"{worker_url.rstrip('/')}/",
            data=prompt.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
        )
        with urlopen(req) as resp:
            mp4_bytes = resp.read()
        return json.dumps({
            "status": "generated",
            "scene_num": scene_num,
            "phrase_idx": phrase_idx,
            "worker_url": worker_url,
            "bytes": len(mp4_bytes),
        })
    except URLError as e:
        return json.dumps({
            "error": f"GPU worker unreachable: {e}",
            "scene_num": scene_num,
            "phrase_idx": phrase_idx,
            "worker_url": worker_url,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


def _tool_check_clip(job_id: str, worker_url: str) -> str:
    """DEPRECATED: Synchronous protocol has no job IDs.

    Probes worker health via GET / instead.
    """
    if not worker_url:
        return json.dumps({"error": "No worker_url provided"})

    try:
        req = Request(f"{worker_url.rstrip('/')}/")
        with urlopen(req, timeout=10) as resp:
            text = resp.read().decode().strip()
        return json.dumps({"job_id": job_id, "worker_alive": text.startswith("ok"), "health": text})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ═══════════════════════════════════════════════════════════════════════════
# Tool definitions for the agent
# ═══════════════════════════════════════════════════════════════════════════

_VIDEO_PROVISIONER_TOOLS = [
    # -- OTIO reading tools --
    AgentTool(
        name="read_timeline",
        description=(
            "Read the full OTIO timeline structure — tracks, clips, "
            "gaps, durations, metadata. This is the authoritative "
            "view of the documentary's structure."
        ),
        parameters={"type": "object", "properties": {}},
        fn=lambda: _tool_read_timeline(),
    ),
    AgentTool(
        name="read_pipeline_data",
        description=(
            "Read pipeline metadata from the OTIO timeline. Keys: "
            "scenes, whisperx_alignment, visual_concepts, visual_style, "
            "style_lock, content_analysis. If the key doesn't exist, "
            "returns an error — that IS the contract violation."
        ),
        parameters={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Pipeline metadata key",
                },
            },
            "required": ["key"],
        },
        fn=lambda key: _tool_read_pipeline_data(key),
    ),
    AgentTool(
        name="get_scene_durations",
        description=(
            "Get per-scene duration budgets — narration vs video vs total. "
            "Shows how much time each scene has, how much narration fills, "
            "and how much video needs."
        ),
        parameters={"type": "object", "properties": {}},
        fn=lambda: _tool_get_scene_durations(),
    ),
    AgentTool(
        name="find_video_gaps",
        description=(
            "Scan V1_Video track for Gaps (missing video). Cross-references "
            "with visual_concepts metadata. Returns JSON list of render jobs. "
            "Each gap has: scene_num, phrase_idx, duration, status "
            "(needs_visual_concept or needs_render). This is the primary "
            "tool for PHASE 1: SURVEY."
        ),
        parameters={"type": "object", "properties": {}},
        fn=lambda: _tool_find_video_gaps(),
    ),

    # -- OTIO writing tools --
    AgentTool(
        name="write_pipeline_data",
        description=(
            "Write pipeline metadata to the OTIO timeline with provenance. "
            "This is how you persist visual_concepts and other intermediate data."
        ),
        parameters={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Pipeline metadata key"},
                "value_json": {"type": "string", "description": "JSON string of the value to write"},
                "provenance_json": {"type": "string", "description": "JSON provenance record"},
            },
            "required": ["key", "value_json"],
        },
        fn=lambda key, value_json, provenance_json="{}": _tool_write_pipeline_data(
            key, value_json, provenance_json
        ),
    ),
    AgentTool(
        name="add_clip",
        description=(
            "Add a rendered video clip to the OTIO timeline with provenance."
        ),
        parameters={
            "type": "object",
            "properties": {
                "track": {"type": "string", "description": "Track name (V1_Video)"},
                "scene_num": {"type": "integer", "description": "Scene number"},
                "phrase_idx": {"type": "integer", "description": "Phrase index within scene"},
                "clip_path": {"type": "string", "description": "Path to the clip file"},
                "duration": {"type": "number", "description": "Duration in seconds"},
                "provenance_json": {"type": "string", "description": "JSON provenance record"},
            },
            "required": ["track", "scene_num", "phrase_idx", "clip_path", "duration"],
        },
        fn=lambda track, scene_num, phrase_idx, clip_path, duration, provenance_json="{}": _tool_add_clip(
            track, scene_num, phrase_idx, clip_path, duration, provenance_json
        ),
    ),
    AgentTool(
        name="validate_timeline",
        description=(
            "Validate timeline structural integrity for a pipeline phase. "
            "Checks: correct track structure, no missing clips, no "
            "zero-duration gaps, no duplicate clip names."
        ),
        parameters={
            "type": "object",
            "properties": {
                "phase": {
                    "type": "string",
                    "enum": ["scenario", "audio", "visual_direction", "production", "assembly"],
                    "description": "Pipeline phase to validate against",
                },
            },
            "required": ["phase"],
        },
        fn=lambda phase: _tool_validate_timeline(phase),
    ),
    AgentTool(
        name="rebalance_durations",
        description=(
            "Rebalance scene duration budgets. Adjusts scene durations "
            "while conserving total timeline duration."
        ),
        parameters={
            "type": "object",
            "properties": {
                "adjustments_json": {"type": "string", "description": "JSON dict {scene_num: new_duration_sec}"},
                "reason": {"type": "string", "description": "Why the rebalance is needed (audit trail)"},
            },
            "required": ["adjustments_json", "reason"],
        },
        fn=lambda adjustments_json, reason: _tool_rebalance_durations(adjustments_json, reason),
    ),

    # -- Visual planning tools --
    AgentTool(
        name="query_lora_catalog",
        description=(
            "Query the LoRA catalog for matching styles. Returns ranked "
            "LoRA matches based on content type and mood."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content_type": {"type": "string", "description": "Content type (historical, technology, nature, data, urban)"},
                "mood": {"type": "string", "description": "Desired mood (dramatic, nostalgic, mysterious)"},
            },
            "required": [],
        },
        fn=lambda content_type="", mood="": _tool_query_lora_catalog(content_type, mood),
    ),
    AgentTool(
        name="get_lora_details",
        description="Get full details for a specific LoRA entry.",
        parameters={
            "type": "object",
            "properties": {
                "lora_id": {"type": "string", "description": "LoRA identifier (e.g. documentary-realism)"},
            },
            "required": ["lora_id"],
        },
        fn=lambda lora_id: _tool_get_lora_details(lora_id),
    ),

    # -- Provisioning tools --
    AgentTool(
        name="search_gpu_offers",
        description=(
            "Search Vast.ai for available GPU offers. Returns raw JSON "
            "with GPU name, VRAM, price, reliability, bandwidth, location. "
            "You read the results and REASON about what GPU to pick. "
            "LTX-2.3 needs significant VRAM (~46GB for transformer alone, "
            "80GB+ recommended). No hardcoded registries — you decide."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Vast.ai search query string. Examples: "
                        "'gpu_ram>=48 dph_total<=5.00 rentable=true reliability>0.95', "
                        "'gpu_name=H200 gpu_ram>=48 dph_total<=8.00 rentable=true'"
                    ),
                },
            },
            "required": ["query"],
        },
        fn=lambda query: _tool_search_gpu_offers(query),
    ),
    AgentTool(
        name="provision_vm",
        description=(
            "Provision a Vast.ai GPU VM from a specific offer. You pick "
            "the offer_id from search_gpu_offers results. The VM starts "
            "gpu_worker.py which loads LTX-2.3 and serves POST /."
        ),
        parameters={
            "type": "object",
            "properties": {
                "offer_id": {"type": "integer", "description": "Offer ID from search_gpu_offers"},
                "disk_gb": {"type": "integer", "description": "Disk size GB (default 224 for LTX)"},
                "worker_mode": {"type": "string", "enum": ["tts", "ltx", "both"], "description": "Worker mode"},
                "docker_image": {"type": "string", "description": "Docker image (empty = auto-resolve)"},
                "env_vars_json": {"type": "string", "description": "JSON dict of extra env vars"},
            },
            "required": ["offer_id"],
        },
        fn=lambda offer_id, disk_gb=224, worker_mode="ltx", docker_image="", env_vars_json="{}": _tool_provision_vm(
            offer_id, disk_gb, worker_mode, docker_image, env_vars_json
        ),
    ),
    AgentTool(
        name="check_vm_status",
        description="Check a VM's status, connection details, and health endpoint.",
        parameters={
            "type": "object",
            "properties": {
                "vm_id": {"type": "string", "description": "Instance ID from provision_vm"},
            },
            "required": ["vm_id"],
        },
        fn=lambda vm_id: _tool_check_vm_status(vm_id),
    ),
    AgentTool(
        name="check_worker_health",
        description="HTTP GET to a worker's / endpoint. Returns plain text status.",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Worker URL (e.g. http://1.2.3.4:8880)"},
                "capability": {"type": "string", "description": "Capability to check (ltx, tts)"},
            },
            "required": ["url"],
        },
        fn=lambda url, capability="ltx": _tool_check_worker_health(url, capability),
    ),
    AgentTool(
        name="terminate_vm",
        description="Destroy a Vast.ai VM instance. Use during CLEANUP phase.",
        parameters={
            "type": "object",
            "properties": {
                "vm_id": {"type": "string", "description": "Instance ID to terminate"},
            },
            "required": ["vm_id"],
        },
        fn=lambda vm_id: _tool_terminate_vm(vm_id),
    ),
    AgentTool(
        name="list_active_vms",
        description="List all active Vast.ai VM instances.",
        parameters={"type": "object", "properties": {}},
        fn=lambda: _tool_list_active_vms(),
    ),
    AgentTool(
        name="get_account_credits",
        description="Query Vast.ai account and return available credit balance in USD.",
        parameters={"type": "object", "properties": {}},
        fn=lambda: _tool_get_account_credits(),
    ),

    # -- Production tools --
    AgentTool(
        name="render_clip",
        description=(
            "Submit a video clip render job to a GPU worker. HTTP POST to "
            "POST /. Accepts worker_url explicitly — you decide "
            "which worker to use."
        ),
        parameters={
            "type": "object",
            "properties": {
                "scene_num": {"type": "integer", "description": "Scene number"},
                "phrase_idx": {"type": "integer", "description": "Phrase index within scene"},
                "prompt": {"type": "string", "description": "LTX-2.3 video prompt"},
                "negative_prompt": {"type": "string", "description": "Negative prompt"},
                "duration": {"type": "number", "description": "Target duration in seconds"},
                "lora_id": {"type": "string", "description": "LoRA style identifier"},
                "worker_url": {"type": "string", "description": "URL of the GPU worker"},
            },
            "required": ["scene_num", "phrase_idx", "prompt", "worker_url"],
        },
        fn=lambda scene_num, phrase_idx, prompt, worker_url,
               negative_prompt="", duration=5.0, lora_id="": _tool_render_clip(
            scene_num, phrase_idx, prompt, negative_prompt, duration, lora_id, worker_url
        ),
    ),
    AgentTool(
        name="check_clip",
        description="Check the status of a GPU render job. GET /video/status/{job_id}.",
        parameters={
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Render job ID"},
                "worker_url": {"type": "string", "description": "URL of the GPU worker"},
            },
            "required": ["job_id", "worker_url"],
        },
        fn=lambda job_id, worker_url: _tool_check_clip(job_id, worker_url),
    ),
]


# ═══════════════════════════════════════════════════════════════════════════
# VideoProvisionerAgent
# ═══════════════════════════════════════════════════════════════════════════

_VIDEO_PROVISIONER_INSTRUCTION = """\
You are the Video Provisioner Agent — you own rendering end-to-end.

You merge the provisioner with the video agent. Your domain: visual
planning, GPU worker provisioning, clip rendering, quality evaluation,
and fleet scaling. You use raw LLM reasoning with vast CLI output and
Letta-based memory. No hardcoded model registries — you search Vast.ai,
read the results, and reason about what GPU to pick.

OPERATE IN PHASES:

═══════════════════════════════════════════════════════════════════════
PHASE 1: SURVEY
═══════════════════════════════════════════════════════════════════════

1. Call read_timeline to see the full timeline structure
2. Call find_video_gaps to identify Gaps in V1_Video track
3. Gaps = unfinished work. Clips with valid media = done.
4. Compute the work backlog: how many gaps need visual planning vs
   rendering.

If there are zero gaps, the timeline is complete — skip to PHASE 5.

═══════════════════════════════════════════════════════════════════════
PHASE 2: VISUAL PLANNING
═══════════════════════════════════════════════════════════════════════

For each gap with status "needs_visual_concept":

1. Read narration text and alignment:
   - read_pipeline_data("scenes") — get scene content
   - read_pipeline_data("whisperx_alignment") — get timing alignment
2. Identify semantic breakpoints in the narration
3. Generate LTX-2.3 prompts:
   - Single flowing paragraphs, present tense
   - ONE subject, ONE action per prompt
   - Describe camera movement, lighting, atmosphere
   - Avoid: text in frame, complex human figures, chaotic motion
4. Check LoRA catalog for matching styles:
   - query_lora_catalog(content_type, mood)
   - get_lora_details(lora_id) for top matches
5. Quality check your own prompts:
   - Style consistency across the scene
   - Prompt format (single paragraph, present tense)
   - Narrative-visual connection (does the visual match the narration?)
6. Write visual_concepts to OTIO:
   - write_pipeline_data("visual_concepts", concepts_json, provenance_json)

═══════════════════════════════════════════════════════════════════════
PHASE 3: INFRASTRUCTURE
═══════════════════════════════════════════════════════════════════════

Provision GPU VMs for rendering:

1. Check budget: get_account_credits()
2. Search Vast.ai for available GPUs:
   - search_gpu_offers("gpu_ram>=48 dph_total<=5.00 rentable=true reliability>0.95")
   - READ the results and REASON about what GPU to pick
   - LTX-2.3 needs ~46GB for the transformer alone at bf16
   - 80GB+ VRAM recommended (H200, H100, A100_SXM4)
   - If no exact GPU matches, broaden: search_gpu_offers("gpu_ram>=48 dph_total<=8.00 rentable=true")
3. Create instance: provision_vm(offer_id, disk_gb=224, worker_mode="ltx")
4. Wait for health: check_vm_status(vm_id) then check_worker_health(url)
5. Decide how many workers based on queue depth and budget:
   - More gaps = more workers (up to budget limit)
   - Each worker costs $2-8/hr depending on GPU
   - Don't spend more than 50% of credits on a single run

REMEMBER: You decide which GPU to use. Read the search results, compare
VRAM, price, reliability, bandwidth. No hardcoded registries.

═══════════════════════════════════════════════════════════════════════
PHASE 4: PRODUCTION
═══════════════════════════════════════════════════════════════════════

Render clips:

1. For each gap with status "needs_render":
   - Read the visual_concept for this gap
   - render_clip(scene_num, phrase_idx, prompt, worker_url=..., lora_id=...)
2. Poll status: check_clip(job_id, worker_url)
3. When clip is ready, add to OTIO timeline:
   - add_clip("V1_Video", scene_num, phrase_idx, clip_path, duration, provenance)
4. Scale fleet based on queue depth:
   - If queue is long and budget allows: provision more VMs
   - If queue is short: terminate excess VMs
   - Always keep at least one worker until all clips are done

═══════════════════════════════════════════════════════════════════════
PHASE 5: CLEANUP
═══════════════════════════════════════════════════════════════════════

1. Validate timeline: validate_timeline("production")
2. Destroy all VMs: list_active_vms() then terminate_vm(vm_id) for each
3. Final summary: how many clips rendered, total cost, any failures

═══════════════════════════════════════════════════════════════════════

ESCALATION LADDER (STRICT ONE-SHOT — video rendering is expensive):
Video is strict because each render attempt consumes significant GPU time
and money.  Each tier gets exactly ONE attempt — no retries at the same level.

L0 DOMAIN FIX (1 attempt):
  - Domain-informed prompt rewrite (change visual concept, simplify prompt)
  - Adjust negative_prompt to fix artifacts
  - Change LoRA influence weight

L1 RETRY (1 attempt):
  - Different generation strategy (different seed, prompt structure)
  - Use alternative aspect ratio or motion parameters

L2 CREATIVE (1 attempt):
  - Alternative approach (different concept for same scene)
  - Use a different LoRA style
  - Simplify the visual concept substantially

L3 COLLABORATIVE (1 attempt):
  - May reshape clip plan (swap scenes, reorder, merge short clips)
  - Duration-preserving reshaping only — total runtime stays the same
  - Coordinate with OTIO gate for timeline modifications
  - Requires begin_escalation(EXTEND) to modify authoritative OTIO

L4 HUMAN (1 decision):
  - Present full diagnostic chain and remaining options
  - Recommend specific action with justification

FAILURE CLASSIFICATION:
Before escalating, CLASSIFY the failure:
- CONTENT failure (bad visual, artifact, concept mismatch, QA fail): use video ladder above
- INFRA failure (CUDA error, OOM, driver crash, timeout, preemption): use infra ladder below
- UNCLEAR: run short_diagnostic() on the worker to reclassify

INFRA LADDER (separate from content budget — PERMISSIVE for infra):
L0 FIX (4 attempts): retry on a different healthy worker
L1 RETRY (2 attempts): recycle suspect worker, redispatch
L2 CREATIVE (1 attempt): scale fleet, hot-swap GPU tier, different region
L3 COLLABORATIVE (1 attempt): coordinate with content ladder, down-spec params
L4 HUMAN (1 decision)

NEVER condemn a worker from a single bad clip — require 2+ independent
infra signals (job failure + infra_agent report) before terminating a VM.

RULES:
- ALL data flows through the OTIO file on disk. No agent state.
- Every write carries provenance.
- LTX-2.3 is a hard requirement — never substitute a different model.
- VRAM is a hard floor — never pick a GPU below 48GB.
- If scenes or alignment are missing, report the error — that's a contract violation.
- If GPU worker is unreachable, provision a new one.
- Remember past provisioning decisions in memory — which GPUs worked,
  which didn't, what price ranges are typical.
- Budget awareness: check credits before provisioning, don't overspend.
- Track ladder state in OTIO: write_pipeline_data("video_ladder", {level, attempts, history})
- Use begin_escalation(EXTEND) before modifying authoritative OTIO — duration-preserving only
- Each video tier gets ONE attempt — if it fails, escalate immediately
"""


class VideoProvisionerAgent(RecoveryAgent):
    """Merged provisioner + video agent.

    Owns rendering end-to-end: visual planning, allocate GPU workers,
    render clips, evaluate quality, scale fleet.

    Uses raw LLM reasoning with vast CLI output + Letta-based memory.
    No hardcoded model registries. The agent searches Vast.ai, reads
    the results, and reasons about what GPU to pick.
    """

    def __init__(self) -> None:
        super().__init__(
            name="video_provisioner_agent",
            instruction=_VIDEO_PROVISIONER_INSTRUCTION,
            tools=_VIDEO_PROVISIONER_TOOLS,
            max_tool_rounds=12,
        )


# Module-level instance
video_provisioner_agent = VideoProvisionerAgent()
