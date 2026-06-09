from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque, defaultdict
import time
from typing import Any, Optional, Literal, cast
import opentimelineio as otio
from pydantic import BaseModel, Field

from effects import (
    Effect,
    UpdateScript,
    ReorderScenes,
    MergeIntoOTIO,
    DurationAdjusted,
    DeleteScene,
    DeleteFromOTIO,
    QueueJob,
    JobStarted,
    JobCompleted,
    JobFailed,
    JobRequeued,
    VMAllocated,
    VMDeallocated,
    VMObserved,
    ProductionFailed,
    KIND_TO_MODEL,
    EffectUnion,
)
from event_store import EventStore


def parse_duration(val: Any) -> float:
    """Parse standard durations (e.g., floats/integers) and time formats like "MM:SS" or "HH:MM:SS" to float.

    Examples:
        "2:30" -> 150.0
        "1:02:30" -> 3750.0
        15.5 -> 15.5
        "15.5" -> 15.5
    """
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        val = val.strip()
        if ":" in val:
            parts = val.split(":")
            try:
                if len(parts) == 2:
                    m = int(parts[0])
                    s = float(parts[1])
                    return m * 60.0 + s
                elif len(parts) == 3:
                    h = int(parts[0])
                    m = int(parts[1])
                    s = float(parts[2])
                    return h * 3600.0 + m * 60.0 + s
            except ValueError:
                pass  # Fall back to raw float parsing
        try:
            return float(val)
        except ValueError:
            raise ValueError(f"Could not parse duration string: {val}")
    raise ValueError(f"Invalid duration type: {type(val)}")


class Projection(ABC):
    """Abstract base for all incremental read models."""

    def __init__(self) -> None:
        self.last_sequence: int = 0

    def tick(self, store: EventStore) -> int:
        """Fetch events since ``last_sequence`` and apply each.

        Returns the number of events processed.
        """
        records = store.read_since(self.last_sequence)
        processed = 0
        for record in records:
            self.apply(record.effect)
            self.last_sequence = record.seq
            processed += 1
        return processed

    @abstractmethod
    def apply(self, event: Effect) -> None:
        """Mutate projection state in response to a single event."""
        ...

    def summary(self) -> str:
        """Return a human-readable summary for agent prompts."""
        return f"{self.__class__.__name__}(last_sequence={self.last_sequence})"


# ===========================================================================
# 6.2 OTIO Projection
# ===========================================================================

class Timeline(Projection):
    """Builds and validates an OpenTimelineIO timeline from events."""

    def __init__(self) -> None:
        super().__init__()
        self.timeline: otio.schema.Timeline = otio.schema.Timeline(
            name="Documentary", global_start_time=otio.opentime.RationalTime(0, 24)
        )
        self.slots: dict[str, dict] = {}  # slot_addr -> slot state fields

    def apply(self, event: Effect) -> None:
        if event.kind == "update_script":
            self._build_from_script(cast(UpdateScript, event))
        elif event.kind == "merge_into_otio":
            self._merge_clip(cast(MergeIntoOTIO, event))
        elif event.kind == "duration_adjusted":
            self._adjust_slot_duration(cast(DurationAdjusted, event))
        elif event.kind == "delete_scene":
            self._delete_scene(cast(DeleteScene, event))
        elif event.kind == "reorder_scenes":
            self._reorder_scenes(cast(ReorderScenes, event))
        elif event.kind == "delete_from_otio":
            self._remove_clip(cast(DeleteFromOTIO, event))

    def _build_from_script(self, event: UpdateScript) -> None:
        track_name_audio = "A1_Narration"
        track_name_video = "V1_Video"

        audio_track = None
        video_track = None
        for track in self.timeline.tracks:
            if track.name == track_name_audio:
                audio_track = track
            elif track.name == track_name_video:
                video_track = track

        if audio_track is None:
            audio_track = otio.schema.Track(name=track_name_audio, kind=otio.schema.TrackKind.Audio)
            self.timeline.tracks.append(audio_track)
        if video_track is None:
            video_track = otio.schema.Track(name=track_name_video, kind=otio.schema.TrackKind.Video)
            self.timeline.tracks.append(video_track)

        new_slot_addrs = set()
        for block in event.blocks:
            for track_prefix, track in [("A1", audio_track), ("V1", video_track)]:
                slot_addr = f"{track_prefix}:{block.scene_num}:{block.block_id}"
                new_slot_addrs.add(slot_addr)

                visual_notes = getattr(block, "visual_notes", "") or ""
                visual_concepts = getattr(block, "visual_concepts", "") or ""
                if not visual_concepts:
                    visual_concepts = f"Documentary scene {block.scene_num}: {visual_notes}"

                existing = self.slots.get(slot_addr)
                if existing is not None:
                    unchanged = (
                        existing.get("text") == block.text
                        and existing.get("speaker") == block.speaker
                        and abs(existing.get("scripted_sec", 0.0) - block.duration_sec) < 0.001
                        and existing.get("visual_notes") == visual_notes
                        and existing.get("visual_concepts") == visual_concepts
                    )
                    if unchanged:
                        continue
                    existing["text"] = block.text
                    existing["speaker"] = block.speaker
                    existing["scripted_sec"] = block.duration_sec
                    existing["measured_sec"] = None
                    existing["status"] = "scripted"
                    existing["artifact_uri"] = None
                    existing["visual_notes"] = visual_notes
                    existing["visual_concepts"] = visual_concepts
                    self._update_clip_duration(slot_addr, block.duration_sec)
                else:
                    rate = 24
                    duration_rt = otio.opentime.RationalTime(block.duration_sec * rate, rate)
                    clip = otio.schema.Clip(
                        name=slot_addr,
                        source_range=otio.opentime.TimeRange(
                            start_time=otio.opentime.RationalTime(0, rate),
                            duration=duration_rt,
                        ),
                        media_reference=otio.schema.MissingReference(),
                    )
                    track.append(clip)
                    self.slots[slot_addr] = {
                        "scene_num": block.scene_num,
                        "block_id": block.block_id,
                        "speaker": block.speaker,
                        "text": block.text,
                        "scripted_sec": block.duration_sec,
                        "measured_sec": None,
                        "status": "scripted",
                        "artifact_uri": None,
                        "visual_notes": visual_notes,
                        "visual_concepts": visual_concepts,
                    }

        updated_scenes = {block.scene_num for block in event.blocks}
        for addr in list(self.slots.keys()):
            if addr not in new_slot_addrs:
                slot_scene = self.slots[addr].get("scene_num")
                if slot_scene in updated_scenes:
                    clip = self._find_clip_by_name(addr)
                    if clip is not None:
                        t = clip.parent()
                        if t is not None:
                            t.remove(clip)
                    self.slots.pop(addr, None)

    def _update_clip_duration(self, slot_addr: str, duration_sec: float) -> None:
        clip = self._find_clip_by_name(slot_addr)
        if clip is None:
            return
        rate = 24
        new_duration = otio.opentime.RationalTime(duration_sec * rate, rate)
        clip.source_range = otio.opentime.TimeRange(
            start_time=clip.source_range.start_time,
            duration=new_duration,
        )

    def _merge_clip(self, event: MergeIntoOTIO) -> None:
        slot_addr = event.slot_id
        clip = self._find_clip_by_name(slot_addr)
        if clip is None:
            return
        rate = 24
        new_duration = otio.opentime.RationalTime(event.duration_sec * rate, rate)
        clip.source_range = otio.opentime.TimeRange(
            start_time=clip.source_range.start_time,
            duration=new_duration,
        )
        clip.media_reference = otio.schema.ExternalReference(
            target_url=event.artifact_uri,
            available_range=clip.source_range,
        )
        if slot_addr in self.slots:
            self.slots[slot_addr]["status"] = "delivered"
            self.slots[slot_addr]["artifact_uri"] = event.artifact_uri
            self.slots[slot_addr]["measured_sec"] = event.duration_sec

    def _adjust_slot_duration(self, event: DurationAdjusted) -> None:
        # Determine both audio and video canonical slot addresses
        audio_addr = f"A1:{event.scene_num}:{event.block_id}"
        video_addr = f"V1:{event.scene_num}:{event.block_id}"
        
        slot_addrs = [audio_addr, video_addr]
        if event.slot_id and event.slot_id not in slot_addrs:
            slot_addrs.append(event.slot_id)
            if event.slot_id.startswith("A1:"):
                partner = event.slot_id.replace("A1:", "V1:", 1)
                if partner not in slot_addrs:
                    slot_addrs.append(partner)
            elif event.slot_id.startswith("V1:"):
                partner = event.slot_id.replace("V1:", "A1:", 1)
                if partner not in slot_addrs:
                    slot_addrs.append(partner)

        rate = 24
        new_duration = otio.opentime.RationalTime(event.measured_sec * rate, rate)
        for slot_addr in slot_addrs:
            clip = self._find_clip_by_name(slot_addr)
            if clip is not None:
                clip.source_range = otio.opentime.TimeRange(
                    start_time=clip.source_range.start_time,
                    duration=new_duration,
                )
            if slot_addr in self.slots:
                self.slots[slot_addr]["measured_sec"] = event.measured_sec
                self.slots[slot_addr]["status"] = "measured"

    def _delete_scene(self, event: DeleteScene) -> None:
        scene_num = event.scene_num
        to_remove = [
            addr for addr, slot in self.slots.items()
            if slot["scene_num"] == scene_num
        ]
        for addr in to_remove:
            clip = self._find_clip_by_name(addr)
            if clip is not None:
                track = clip.parent()
                if track is not None:
                    track.remove(clip)
            self.slots.pop(addr, None)

    def _reorder_scenes(self, event: ReorderScenes) -> None:
        if not self.timeline.tracks:
            return

        for track in self.timeline.tracks:
            scene_to_clips: dict[int, list[otio.schema.Clip]] = defaultdict(list)
            for child in list(track):
                if isinstance(child, otio.schema.Clip):
                    parts = child.name.split(":")
                    if len(parts) >= 2:
                        try:
                            scene_num = int(parts[1])
                            scene_to_clips[scene_num].append(child)
                        except ValueError:
                            pass  # Ignore invalid/non-integer scene numbers

            new_clips: list[otio.schema.Clip] = []
            for scene_num in event.new_order:
                clips = scene_to_clips.get(scene_num, [])
                clips.sort(key=lambda c: c.name)
                new_clips.extend(clips)

            track.clear()
            for clip in new_clips:
                track.append(clip)

        new_slots: dict[str, dict] = {}
        for track in self.timeline.tracks:
            for child in track:
                if isinstance(child, otio.schema.Clip) and child.name in self.slots:
                    new_slots[child.name] = self.slots[child.name]
        for name, val in self.slots.items():
            if name not in new_slots:
                new_slots[name] = val
        self.slots = new_slots

    def _remove_clip(self, event: DeleteFromOTIO) -> None:
        clip = self._find_clip_by_name(event.slot_id)
        if clip is not None:
            track = clip.parent()
            if track is not None:
                track.remove(clip)
        if event.slot_id in self.slots:
            self.slots[event.slot_id]["status"] = "scripted"
            self.slots[event.slot_id]["artifact_uri"] = None

    def _find_clip_by_name(self, name: str) -> Optional[otio.schema.Clip]:
        for track in self.timeline.tracks:
            for child in track:
                if isinstance(child, otio.schema.Clip) and child.name == name:
                    return child
        return None

    def all_slots_filled(self) -> bool:
        if not self.slots:
            return False
        return all(s.get("status") == "delivered" for s in self.slots.values())

    def get_timeline_duration_sec(self) -> float:
        dur = self.timeline.duration()
        return dur.value / dur.rate if dur and dur.rate else 0.0

    def validate_no_overlaps(self) -> tuple[bool, Optional[str]]:
        for track in self.timeline.tracks:
            children = list(track)
            for i in range(len(children) - 1):
                a, b = children[i], children[i + 1]
                if isinstance(a, otio.schema.Transition) or isinstance(b, otio.schema.Transition):
                    continue
                try:
                    ra = a.trimmed_range_in_parent()
                    rb = b.trimmed_range_in_parent()
                except Exception:
                    continue
                if ra is None or rb is None:
                    continue
                if not (ra.end_time_inclusive() <= rb.start_time):
                    return (
                        False,
                        f"Overlap on {track.name}: {a.name} ({ra}) vs {b.name} ({rb})",
                    )
        return True, None

    def validate_track_alignment(self) -> tuple[bool, Optional[str]]:
        if not self.timeline.tracks:
            return True, None
        track_durations = []
        for track in self.timeline.tracks:
            try:
                d = track.duration()
                if d is not None:
                    track_durations.append(d)
            except Exception:
                continue
        if not track_durations:
            return True, None
        max_dur = max(track_durations, key=lambda rt: rt.value)
        timeline_dur = self.timeline.duration()
        if timeline_dur is None:
            return False, "Timeline has no duration"
        if abs(timeline_dur.value - max_dur.value) > 0.5:
            return (
                False,
                f"Track misalignment: timeline {timeline_dur.value:.2f}s "
                f"!= max track {max_dur.value:.2f}s",
            )
        return True, None

    def validate_clip_media(self) -> tuple[bool, Optional[str]]:
        for track in self.timeline.tracks:
            for child in track:
                if not isinstance(child, otio.schema.Clip):
                    continue
                if isinstance(child.media_reference, otio.schema.MissingReference):
                    return False, f"Clip {child.name} has no media reference"
                try:
                    _ = child.trimmed_range()
                except Exception as e:
                    return False, f"Clip {child.name} invalid range: {e}"
        return True, None

    def summary(self) -> str:
        total = len(self.slots)
        measured = sum(1 for s in self.slots.values() if s["status"] == "measured")
        delivered = sum(1 for s in self.slots.values() if s["status"] == "delivered")
        scripted = sum(1 for s in self.slots.values() if s["status"] == "scripted")
        scenes = len({s["scene_num"] for s in self.slots.values()}) if total > 0 else 0
        return (
            f"OTIO: {scenes} scenes, {total} slots, "
            f"{measured} measured, {delivered} delivered, {scripted} scripted"
        )


# ===========================================================================
# 6.3 Job Projection
# ===========================================================================

class JobState:

    def __init__(self, job_id: str, job_type: str, slot_id: str) -> None:
        self.job_id: str = job_id
        self.job_type: str = job_type
        self.slot_id: str = slot_id
        self.status: str = "pending"
        self.params: dict[str, Any] = {}
        self.artifact_uri: Optional[str] = None
        self.duration_sec: Optional[float] = None
        self.error_message: Optional[str] = None
        self.requeue_count: int = 0
        self.created_at: float = 0.0
        self.completed_at: Optional[float] = None
        self.vm_instance_id: Optional[str] = None

    @property
    def attempts(self) -> int:
        return self.requeue_count + 1



class Jobs(Projection):
    SCRIPT_RESOLVABLE_TYPES: set[str] = {"gap_unexpected", "voice_mismatch"}

    def __init__(self) -> None:
        super().__init__()
        self.jobs: dict[str, JobState] = {}
        self.reconciliation_complete: bool = False
        self.dirty_blocks: set[str] = set()
        self.clean_blocks: set[str] = set()
        self.block_attempts: dict[str, int] = defaultdict(int)
        self.spent_usd: float = 0.0
        self.production_failures: list[dict[str, Any]] = []

    def apply(self, event: Effect) -> None:
        if event.kind in ("queue_job", "queue_audio_job", "queue_video_job"):
            self._on_queue(cast(QueueJob, event))
        elif event.kind in ("job_started", "audio_job_started", "video_job_started"):
            self._on_start(cast(JobStarted, event))
        elif event.kind in ("job_completed", "audio_job_completed", "video_job_completed"):
            self._on_complete(cast(JobCompleted, event))
        elif event.kind in ("job_failed", "audio_job_failed", "video_job_failed"):
            self._on_fail(cast(JobFailed, event))
        elif event.kind in ("job_requeued", "audio_job_requeued", "video_job_requeued"):
            self._on_requeue(cast(JobRequeued, event))
        elif event.kind == "reconciliation_complete":
            self.reconciliation_complete = True
            self.dirty_blocks.clear()
        elif event.kind == "reconciliation_failed":
            self.reconciliation_complete = False
        elif event.kind == "production_failed":
            pf_event = cast(ProductionFailed, event)
            self.production_failures.append({
                "slot_id": pf_event.slot_id,
                "failure_type": pf_event.failure_type,
                "expected": pf_event.expected,
                "actual": pf_event.actual,
                "suggested_fix": pf_event.suggested_fix.model_dump() if pf_event.suggested_fix else {},
            })
        elif event.kind == "update_script":
            us_event = cast(UpdateScript, event)
            self._resolve_failures_on_script_update(us_event)
            self._sync_from_otio(us_event)

    def _on_queue(self, event: Any) -> None:
        job_id = event.job_id
        if job_id not in self.jobs:
            if event.kind == "queue_audio_job" or event.__class__.__name__ == "QueueAudioJob":
                job_type = "tts"
            elif event.kind == "queue_video_job" or event.__class__.__name__ == "QueueVideoJob":
                job_type = "ltx"
            else:
                job_type = getattr(event, "job_type", "tts")
            
            job = JobState(
                job_id=job_id,
                job_type=job_type,
                slot_id=getattr(event, "slot_id", ""),
            )
            job.params = getattr(event, "params", {})
            job.created_at = getattr(event, "timestamp", 0.0)
            self.jobs[job_id] = job
            block_id = getattr(event, "slot_id", None)
            if block_id:
                self.dirty_blocks.add(block_id)
                self.clean_blocks.discard(block_id)
                if job_type == "tts":
                    self.block_attempts[block_id] += 1

    def _on_start(self, event: JobStarted) -> None:
        job = self.jobs.get(event.job_id)
        if job:
            job.status = "running"
            job.vm_instance_id = getattr(event, "vm_instance_id", None)

    def _on_complete(self, event: JobCompleted) -> None:
        job = self.jobs.get(event.job_id)
        if job:
            job.status = "completed"
            job.artifact_uri = getattr(event, "artifact_uri", None)
            job.duration_sec = getattr(event, "duration_sec", None)
            job.completed_at = getattr(event, "timestamp", None)
            block_id = job.slot_id
            if block_id in self.dirty_blocks:
                self.dirty_blocks.discard(block_id)
                self.clean_blocks.add(block_id)

    def _on_fail(self, event: JobFailed) -> None:
        job = self.jobs.get(event.job_id)
        if job:
            job.status = "failed"
            job.error_message = getattr(event, "error_message", "unknown")

    def _on_requeue(self, event: JobRequeued) -> None:
        job = self.jobs.get(event.job_id)
        if job:
            job.status = "pending"
            job.requeue_count += 1
            job.error_message = None
            if event.new_params is not None:
                job.params.update(cast(dict[str, Any], event.new_params))
            if job.slot_id:
                self.dirty_blocks.add(job.slot_id)
                self.clean_blocks.discard(job.slot_id)

    def _sync_from_otio(self, event: UpdateScript) -> None:
        updated_block_ids = {f"A1:{b.scene_num}:{b.block_id}" for b in event.blocks}
        for bid in updated_block_ids:
            self.dirty_blocks.add(bid)
            self.clean_blocks.discard(bid)

    def _resolve_failures_on_script_update(self, event: UpdateScript) -> None:
        updated_block_ids = {f"A1:{b.scene_num}:{b.block_id}" for b in event.blocks}
        self.production_failures = [
            f for f in self.production_failures
            if not (
                f.get("slot_id") in updated_block_ids
                and f.get("failure_type") in self.SCRIPT_RESOLVABLE_TYPES
            )
        ]

    def has_pending_or_running_jobs(self, job_type: Optional[str] = None) -> bool:
        for job in self.jobs.values():
            if job.status in ("pending", "running"):
                if job_type is None or job.job_type == job_type:
                    return True
        return False

    def pending_jobs(self, job_type: Optional[str] = None) -> list[JobState]:
        return [
            j for j in self.jobs.values()
            if j.status == "pending" and (job_type is None or j.job_type == job_type)
        ]

    def block_attempts_exceeded(self, block_id: str, max_attempts: int = 5) -> bool:
        return self.block_attempts.get(block_id, 0) >= max_attempts

    def budget_exceeded(self, max_budget_usd: float = 10.0) -> bool:
        return self.spent_usd >= max_budget_usd

    def is_block_clean(self, block_id: str) -> bool:
        return block_id in self.clean_blocks

    def all_blocks_clean(self, block_ids: list[str]) -> bool:
        return all(self.is_block_clean(bid) for bid in block_ids)

    def summary(self) -> str:
        by_status: dict[str, int] = defaultdict(int)
        by_type: dict[str, int] = defaultdict(int)
        for job in self.jobs.values():
            by_status[job.status] += 1
            by_type[job.job_type] += 1
        return (
            f"Jobs: {len(self.jobs)} total, "
            f"pending={by_status['pending']}, running={by_status['running']}, "
            f"completed={by_status['completed']}, failed={by_status['failed']} | "
            f"tts={by_type['tts']}, ltx={by_type['ltx']} | "
            f"reconciled={'yes' if self.reconciliation_complete else 'no'} | "
            f"dirty={len(self.dirty_blocks)} clean={len(self.clean_blocks)} | "
            f"spent=${self.spent_usd:.4f}"
        )


# ===========================================================================
# 6.4 VM Projection
# ===========================================================================

class VMRecord:

    def __init__(self, instance_id: str, status: str = "active", role: str = "", offer_id: str = "", worker_url: str = "", hourly_rate_usd: float = 0.0, started_at: float = 0.0, gpu_type: str = "") -> None:
        self.instance_id: str = instance_id
        self.status: str = status
        self.role: str = role
        self.offer_id: str = offer_id
        self.worker_url: str = worker_url
        self.hourly_rate_usd: float = hourly_rate_usd
        self.started_at: float = started_at
        self.observed_status: Optional[str] = None
        self.gpu_type: str = gpu_type


class VMs(Projection):

    def __init__(self) -> None:
        super().__init__()
        self.vms: dict[str, VMRecord] = {}

    @property
    def active(self) -> dict[str, VMRecord]:
        return self.vms

    def apply(self, event: Effect) -> None:
        if event.kind == "vm_allocated":
            vm_alloc = cast(VMAllocated, event)
            self.vms[vm_alloc.instance_id] = VMRecord(
                instance_id=vm_alloc.instance_id,
                status="active",
                role=getattr(vm_alloc, "role", ""),
                offer_id=getattr(vm_alloc, "offer_id", ""),
                worker_url=getattr(vm_alloc, "worker_url", ""),
                hourly_rate_usd=getattr(vm_alloc, "cost_per_hour", 0.0),
                started_at=getattr(vm_alloc, "timestamp", 0.0),
                gpu_type=getattr(vm_alloc, "gpu_type", ""),
            )
        elif event.kind == "vm_deallocated":
            vm_dealloc = cast(VMDeallocated, event)
            rec = self.vms.get(vm_dealloc.instance_id)
            if rec:
                rec.status = "destroyed"
        elif event.kind == "vm_observed":
            vm_obs = cast(VMObserved, event)
            rec = self.vms.get(vm_obs.instance_id)
            if rec:
                rec.observed_status = getattr(vm_obs, "observed_status", None)
                if rec.observed_status == "not_found" and rec.status == "active":
                    rec.status = "observed_gone"

    def active_vms(self, role: Optional[str] = None) -> list[VMRecord]:
        return [
            v for v in self.vms.values()
            if v.status == "active" and (role is None or v.role == role)
        ]

    def estimated_hourly_cost(self) -> float:
        return sum(v.hourly_rate_usd for v in self.active_vms())

    def summary(self) -> str:
        active = len(self.active_vms())
        total = len(self.vms)
        cost_hr = self.estimated_hourly_cost()
        roles: dict[str, int] = defaultdict(int)
        for v in self.active_vms():
            roles[v.role] += 1
        role_str = ", ".join(f"{k}={v}" for k, v in roles.items())
        return f"VMs: {active}/{total} active, ${cost_hr:.4f}/hr ({role_str})"


# ===========================================================================
# 6.5 Budget Projection
# ===========================================================================

class BudgetProjection(Projection):

    def __init__(self) -> None:
        super().__init__()
        self.budget_cap_usd: float = 0.0
        self.spent_usd: float = 0.0
        self.vm_costs: dict[str, float] = {}
        self.exceeded: bool = False

    @property
    def budget_usd(self) -> float:
        return self.budget_cap_usd
        self.exceeded_at: Optional[float] = None

    def apply(self, event: Effect) -> None:
        if event.kind == "budget_set":
            self.budget_cap_usd = getattr(event, "budget_usd", 0.0)
            self.exceeded = False
        elif event.kind == "vm_deallocated":
            instance_id = getattr(event, "instance_id", "")
            cost = getattr(event, "final_cost", 0.0)
            if instance_id:
                self.vm_costs[instance_id] = self.vm_costs.get(instance_id, 0.0) + cost
            self.spent_usd += cost
            if not self.exceeded and self.budget_cap_usd > 0 and self.spent_usd > self.budget_cap_usd:
                self.exceeded = True
                self.exceeded_at = getattr(event, "timestamp", 0.0)
        elif event.kind == "budget_exceeded":
            self.exceeded = True
            self.spent_usd = getattr(event, "spent_usd", self.spent_usd)
        elif event.kind == "pipeline_aborted":
            if getattr(event, "reason", "") == "budget_exceeded":
                self.exceeded = True

    def remaining_usd(self) -> float:
        return self.budget_cap_usd - self.spent_usd

    def summary(self) -> str:
        pct = (self.spent_usd / self.budget_cap_usd * 100) if self.budget_cap_usd > 0 else 0.0
        status = "EXCEEDED" if self.exceeded else "OK"
        return f"Budget: ${self.spent_usd:.2f} / ${self.budget_cap_usd:.2f} ({pct:.1f}%, {status})"


# ===========================================================================
# 6.6 State Projection
# ===========================================================================

class PhaseChangeRecord:

    def __init__(self, from_phase: str, to_phase: str, reason: str, at_sequence: int) -> None:
        self.from_phase: str = from_phase
        self.to_phase: str = to_phase
        self.reason: str = reason
        self.at_sequence: int = at_sequence


class StateProjection(Projection):

    def __init__(self, loop_buffer_size: int = 5) -> None:
        super().__init__()
        self.current_phase: str = "init"
        self.phase_history: list[PhaseChangeRecord] = []
        self.recent_effects: dict[str, deque[Effect]] = defaultdict(
            lambda: deque(maxlen=loop_buffer_size)
        )
        self.loop_buffer_size: int = loop_buffer_size
        self.config: dict = {}

    @property
    def phase(self) -> str:
        return self.current_phase


    def apply(self, event: Effect) -> None:
        agent = getattr(event, "agent", None)
        if agent:
            self.recent_effects[agent].append(event)

        if event.kind == "pipeline_started":
            self.current_phase = "init"
            self.phase_history.clear()
            self.recent_effects.clear()
            self.config = getattr(event, "config", {})

        elif event.kind == "reconciliation_complete":
            self._record_phase_change("audio_reconcile")
        elif event.kind == "pipeline_complete":
            self._record_phase_change("done")
        elif event.kind == "pipeline_aborted":
            self._record_phase_change("aborted")
        elif event.kind == "merge_into_otio":
            if getattr(event, "track_name", "") == "V1_Video" and self.current_phase == "audio_reconcile":
                self._record_phase_change("video_production")


    def _record_phase_change(self, to_phase: str, reason: str = "") -> None:
        if self.current_phase != to_phase:
            rec = PhaseChangeRecord(
                from_phase=self.current_phase,
                to_phase=to_phase,
                reason=reason,
                at_sequence=self.last_sequence,
            )
            self.phase_history.append(rec)
            self.current_phase = to_phase

    def get_recent_events(self, n: int) -> list[Effect]:
        all_events = []
        for agent_deque in self.recent_effects.values():
            all_events.extend(list(agent_deque))
        all_events.sort(key=lambda e: getattr(e, "timestamp", 0), reverse=True)
        return all_events[:n]

    def detect_duplicate_loop(self, agent: str, threshold: int = 5) -> tuple[bool, str]:
        buf = self.recent_effects.get(agent, deque())
        if len(buf) < threshold:
            return False, "insufficient history"
        kinds = [getattr(e, "kind", "") for e in buf]
        if len(set(kinds)) == 1:
            return True, f"{agent} produced {kinds[0]} {len(buf)} times"
        return False, "effects vary"

    def get_recent_kinds(self, agent: str) -> list[str]:
        return [getattr(e, "kind", "") for e in self.recent_effects.get(agent, [])]

    def summary(self) -> str:
        tx_count = len(self.phase_history)
        return f"Phase: {self.current_phase}, {tx_count} phase changes, {len(self.recent_effects)} agents tracked"


# ===========================================================================
# 6.7 Projection Response Schemas
# ===========================================================================

class OTIOSlotState(BaseModel):
    scene_num: int
    block_id: str
    speaker: str
    text: str
    scripted_sec: float
    measured_sec: float | None = None
    status: Literal["scripted", "measured", "delivered"] = "scripted"
    artifact_uri: str | None = None
    visual_notes: str = ""
    visual_concepts: str = ""


class OTIOResponse(BaseModel):
    scenes: int
    total_slots: int
    measured_slots: int
    delivered_slots: int
    dirty_slots: int
    duration_sec: float
    slots: dict[str, OTIOSlotState]


class JobResponseItem(BaseModel):
    job_id: str
    job_type: Literal["tts", "ltx"]
    slot_id: str
    status: Literal["pending", "running", "completed", "failed"]
    params: dict = Field(default_factory=dict)
    artifact_uri: str | None = None
    duration_sec: float | None = None
    error_message: str | None = None
    requeue_count: int = 0
    created_at: float = 0.0
    completed_at: float | None = None
    vm_instance_id: str | None = None


class JobResponse(BaseModel):
    jobs: dict[str, JobResponseItem]
    reconciliation_complete: bool = False
    dirty_blocks: list[str] = Field(default_factory=list)
    clean_blocks: list[str] = Field(default_factory=list)
    block_attempts: dict[str, int] = Field(default_factory=dict)
    spent_usd: float = 0.0
    production_failures: list[dict] = Field(default_factory=list)


class VMResponseItem(BaseModel):
    instance_id: str
    status: Literal["active", "destroyed", "observed_gone"]
    role: Literal["tts", "ltx", ""]
    offer_id: str = ""
    worker_url: str = ""
    hourly_rate_usd: float = 0.0
    started_at: float = 0.0
    observed_status: str | None = None
    gpu_type: str = ""


class VMResponse(BaseModel):
    vms: dict[str, VMResponseItem]
    active_count: int
    total_count: int
    estimated_hourly_cost_usd: float
    role_breakdown: dict[str, int] = Field(default_factory=dict)


class PhaseChangeItem(BaseModel):
    from_phase: str
    to_phase: str
    reason: str
    at_sequence: int


class StateResponse(BaseModel):
    current_phase: str = "init"
    phase_changes: list[PhaseChangeItem] = Field(default_factory=list)
    agents_tracked: list[str] = Field(default_factory=list)
    latest_sequence: int = 0
    recent_effects: dict[str, list[EffectUnion]] = Field(default_factory=dict)
    config: dict = Field(default_factory=dict)


class BudgetResponse(BaseModel):
    budget_cap_usd: float = 0.0
    spent_usd: float = 0.0
    remaining_usd: float = 0.0
    exceeded: bool = False
    vm_costs: dict[str, float] = Field(default_factory=dict)


class GlobalStateResponse(BaseModel):
    timestamp: float
    otio: OTIOResponse
    jobs: JobResponse
    vms: VMResponse
    state: StateResponse
    budget: BudgetResponse
    latest_sequence: int
