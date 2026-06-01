---
{
  "title": "Projections",
  "section": "6",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

<- [[A. Appendix EventStoreDB Migration Path|A. Appendix: EventStoreDB Migration Path]] | [[00 - Index|Index]] | [[07 - Agent Environment & Tools|Agent Environment & Tools]] ->

# Projections


Projections are **incremental read models** rebuilt from the event log. Each projection tracks `last_sequence` and processes only new events on every `tick`. If the DB files are lost, replaying from backups through every projection reconstructs the entire pipeline state. Projections never emit events — they are pure consumers (Section 6.1 enforces this absolutely).

---

### 6.1 Projection Base Class

#### 6.1.1 Abstract base with tick() and apply(effect) interface

All projections inherit from `Projection`, an abstract base class that defines two operations:

- `tick(store)`: fetch events newer than `last_sequence`, apply each, increment `last_sequence`.
- `apply(event)`: mutate the projection's internal state in response to a single event.

```python
from abc import ABC, abstractmethod
from collections import deque
from typing import Any, Protocol


class Effect(Protocol):
    """Protocol for effects — projections read kind and payload fields."""
    kind: str


class Projection(ABC):
    """Abstract base for all incremental read models.

    Subclasses implement ``apply()`` to define how each event kind mutates state.
    The ``tick()`` method is final — it handles event fetching and sequence tracking.
    """

    def __init__(self) -> None:
        self.last_sequence: int = 0

    def tick(self, store: EventStore) -> int:
        """Fetch events since ``last_sequence`` and apply each.

        V7.1 fix: Uses SQLite EventStore API (store.read_since) instead of
        the V7 ESDB bare function. Returns the number of events processed.
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
        """Mutate projection state in response to a single event.

        Must be implemented by every concrete projection.
        """
        ...

    def summary(self) -> str:
        """Return a human-readable summary for agent prompts.

        Subclasses override to produce O(1) summaries regardless of event log length.
        """
        return f"{self.__class__.__name__}(last_sequence={self.last_sequence})"
```

#### 6.1.2 last_sequence tracking for incremental updates

`last_sequence` is the waterline. On each `tick`, the projection calls `read_since(self.last_sequence)`, which returns all events with `sequence > last_sequence` ordered by `sequence`. After applying each event, `last_sequence` advances to that event's sequence number. If `tick` processes zero events, `last_sequence` is unchanged and no state mutation occurs.

This design guarantees idempotent `tick` calls: calling `tick` twice with no new events is a no-op. It also makes projections deterministic and replay-safe: reconstructing a projection from an empty state by calling `tick` in a loop until no events remain produces the same state as a projection that has been incrementally updated since run start.

---

### 6.1.3 OTIO Schema Definition

The pipeline uses **OpenTimelineIO (OTIO) 0.16+** (`pip install opentimelineio>=0.16.0`) as the canonical timeline representation. OTIO is an interchange format — it describes edits, not media files. The pipeline uses OTIO's core schema objects (`Timeline`, `Stack`, `Sequence`, `Clip`, `ExternalReference`, `MissingReference`) with custom metadata under the `documentary` namespace.

#### OTIO Object Hierarchy

```
otio.schema.Timeline(name="Documentary")
└── tracks (otio.schema.Stack)
    ├── track[0]: otio.schema.Sequence(name="A1_Narration")
    │   ├── clip[0]: otio.schema.Clip(name="A1:1:1", media_reference=MissingReference)
    │   ├── clip[1]: otio.schema.Clip(name="A1:1:2", media_reference=ExternalReference)
    │   └── ...
    ├── track[1]: otio.schema.Sequence(name="V1_Video")
    │   ├── clip[0]: otio.schema.Clip(name="V1:1:1", media_reference=MissingReference)
    │   └── ...
    └── track[2]: otio.schema.Sequence(name="A2_Music")
        └── ...
```

| OTIO Object | Purpose | Pipeline Mapping |
|---|---|---|
| `Timeline` | Root container | One per run, named `"Documentary"` |
| `Stack` | Track container | `timeline.tracks`, holds all sequences |
| `Sequence` | A single track | `A1_Narration`, `V1_Video`, or `A2_Music` |
| `Clip` | A single slot | One per narration block or video segment |
| `MissingReference` | Placeholder (no media yet) | Initial state after `UpdateScript` |
| `ExternalReference` | Points to actual media file | Set by `MergeIntoOTIO` after approval |

#### Track Layout

| Index | Name | Content | Producer |
|---|---|---|---|
| 0 | `A1_Narration` | Narration audio per block | Scenario Agent (blocks), Audio Agent (media) |
| 1 | `V1_Video` | Video clips per block | Video Agent |
| 2 | `A2_Music` | Background music tracks | Assembly Agent |

Tracks are fixed at pipeline start. No new tracks are created during a run. The `A2_Music` track may remain empty until assembly.

#### Slot Addressing Scheme

Every slot in the timeline has a **canonical slot address** (also called `slot_id`):

```
{track_short}:{scene_num}:{phrase_idx}
```

| Component | Example | Meaning |
|---|---|---|
| `track_short` | `A1`, `V1`, `A2` | Abbreviated track name |
| `scene_num` | `1`, `2`, `3` | 1-based scene index |
| `phrase_idx` | `1`, `2` | 1-based block index within the scene |

**Examples:**
- `A1:3:2` — Audio narration, scene 3, block 2
- `V1:3:2` — Video clip for scene 3, block 2
- `A2:5:1` — Background music for scene 5, block 1

The slot address is stored as `clip.name` on every `otio.schema.Clip`. This allows `Timeline._find_clip_by_name(slot_addr)` to resolve a slot address to its clip in O(tracks × clips).

#### Time Representation

OTIO uses `RationalTime` (value / rate) for all time values. The pipeline uses a **fixed 24 fps rate**:

```python
rate = 24  # frames per second
duration_rt = otio.opentime.RationalTime(duration_sec * rate, rate)
```

All durations in the OTIO timeline are stored as `RationalTime` at 24 fps. When the Assembly Agent exports the final MP4, it renders at the target frame rate (24 fps for film, 30 fps for broadcast).

#### Custom Metadata Namespace

The pipeline stores pipeline-specific metadata under `clip.metadata["documentary"]`:

```python
clip.metadata["documentary"] = {
    "scene_num": 3,
    "phrase_idx": 2,
    "speaker": "narrator",
    "status": "scripted",        # scripted | measured | delivered | dirty
    "text": "In 1929, the crash came...",
    "scripted_sec": 4.5,
    "measured_sec": None,        # set after WhisperX
    "artifact_uri": None,        # set after MergeIntoOTIO
}
```

This metadata is NOT used by OTIO itself — it is read by the `Timeline` and returned in `OTIOResponse.slots` (§6.7.1). The OTIO file (`.otio` JSON) can be opened in any OTIO-compatible tool; the `documentary` metadata is preserved as extra fields.

#### Clip Lifecycle: MissingReference → ExternalReference

```
UpdateScript creates clip
  │
  ▼
otio.schema.Clip(
    name="A1:3:2",
    media_reference=otio.schema.MissingReference(),
    source_range=TimeRange(start=0s, duration=4.5s)
)
  │
  ├── MergeIntoOTIO (after Audio Agent approval)
  │     │
  │     ▼
  │   clip.media_reference = ExternalReference(
  │       target_url="http://provisioner:8081/artifacts/abc/audio/A1:3:2.wav",
  │       available_range=TimeRange(start=0s, duration=4.6s)
  │   )
  │   clip.metadata["documentary"]["status"] = "delivered"
  │   clip.metadata["documentary"]["artifact_uri"] = "http://provisioner:8081/artifacts/abc/audio/A1:3:2.wav"
  │
  └── DurationAdjusted (after reconciliation passes)
        │
        ▼
      clip.source_range.duration = RationalTime(4.6 * 24, 24)
      clip.metadata["documentary"]["measured_sec"] = 4.6
      clip.metadata["documentary"]["status"] = "measured"
```

**DeleteFromOTIO** removes the clip from its track entirely (used during re-reconciliation or scene deletion).

---

### 6.2 OTIO Projection

#### 6.2.1 Timeline construction from script + merge + adjust events

`Timeline` builds an OpenTimelineIO `schema.Timeline` from three event families:

- **Script events** (`UpdateScript`, `DeleteScene`, `ReorderScenes`): define narration blocks with speaker, text, and target duration.
- **Merge events** (`MergeIntoOTIO`): insert approved media clips into timeline slots.
- **Adjust events** (`DurationAdjusted`): update a slot's duration after measured audio passes tolerance.

**V7 critical fix:** `_build_from_script` now performs an **upsert/deep merge** instead of wiping the entire timeline. Unchanged blocks preserve `measured_sec` and `status`. Only changed or new blocks are marked dirty.

```python
from typing import Optional
import opentimelineio as otio


class Timeline(Projection):
    """Builds and validates an OpenTimelineIO timeline from events.

    The timeline is the authoritative structure for the documentary.
    It contains one or more tracks (e.g., "A1_Narration", "V1_Video"),
    each composed of clips aligned to scene slots.
    """

    def __init__(self) -> None:
        super().__init__()
        self.timeline: otio.schema.Timeline = otio.schema.Timeline(
            name="Documentary", global_start_time=otio.opentime.RationalTime(0, 24)
        )
        self.slots: dict[str, dict] = {}  # slot_addr -> {scene_num, speaker, text, duration}

    def apply(self, event: Effect) -> None:
        match event.kind:
            case "update_script":
                self._build_from_script(event)
            case "merge_into_otio":
                self._merge_clip(event)
            case "duration_adjusted":
                self._adjust_slot_duration(event)
            case "delete_scene":
                self._delete_scene(event)
            case "reorder_scenes":
                self._reorder_scenes(event)
            case "delete_from_otio":
                self._remove_clip(event)

    def _build_from_script(self, event: UpdateScript) -> None:
        """Upsert narration blocks from script event. Preserves measured state.

        Creates or updates slots. Unchanged blocks keep measured_sec and status.
        Changed blocks are marked dirty. Absent blocks are removed.
        """
        track_name = "A1_Narration"
        # Ensure track exists
        if not self.timeline.tracks:
            track = otio.schema.Sequence(name=track_name)
            self.timeline.tracks.append(track)
        else:
            track = self.timeline.tracks[0]

        # Compute new set of slot addresses
        new_slot_addrs = set()
        for block in event.blocks:
            slot_addr = f"A1:{block.scene_num}:{block.block_id}"  # V7.1: short form, not full track_name
            new_slot_addrs.add(slot_addr)

            existing = self.slots.get(slot_addr)
            if existing is not None:
                # Compare textual content and target timing (tolerance for float)
                unchanged = (
                    existing.get("text") == block.text
                    and existing.get("speaker") == block.speaker
                    and abs(existing.get("scripted_sec", 0.0) - block.duration_sec) < 0.001
                )
                if unchanged:
                    # Preserve measured_sec, status, artifact_uri
                    continue
                # Block changed — mark dirty, clear measurements
                existing["text"] = block.text
                existing["speaker"] = block.speaker
                existing["scripted_sec"] = block.duration_sec
                existing["measured_sec"] = None
                existing["status"] = "scripted"
                existing["artifact_uri"] = None
                # Update clip duration in timeline
                self._update_clip_duration(slot_addr, block.duration_sec)
            else:
                # New block
                rate = 24
                duration_rt = otio.opentime.RationalTime(
                    block.duration_sec * rate, rate
                )
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
                }

        # Remove slots no longer present in the script
        for addr in list(self.slots.keys()):
            if addr not in new_slot_addrs:
                clip = self._find_clip_by_name(addr)
                if clip is not None:
                    t = clip.parent()
                    if t is not None:
                        t.remove(clip)
                self.slots.pop(addr, None)

    def _update_clip_duration(self, slot_addr: str, duration_sec: float) -> None:
        """Update an existing clip's duration without rebuilding the track."""
        clip = self._find_clip_by_name(slot_addr)
        if clip is None:
            return
        rate = 24
        new_duration = otio.opentime.RationalTime(duration_sec * rate, rate)
        clip.source_range = otio.opentime.TimeRange(
            start_time=clip.source_range.start_time,
            duration=new_duration,
        )

    def _merge_clip(self, event: Effect) -> None:
        """Replace MissingReference with an ExternalReference to the produced artifact."""
        slot_addr = event.slot_id
        clip = self._find_clip_by_name(slot_addr)
        if clip is None:
            return
        clip.media_reference = otio.schema.ExternalReference(
            target_url=event.artifact_uri,
            available_range=clip.source_range,
        )
        if slot_addr in self.slots:
            self.slots[slot_addr]["status"] = "delivered"
            self.slots[slot_addr]["artifact_uri"] = event.artifact_uri

    def _adjust_slot_duration(self, event: Effect) -> None:
        """Update a slot's duration after reconciliation passes tolerance."""
        slot_addr = event.block_id
        clip = self._find_clip_by_name(slot_addr)
        if clip is None:
            return
        rate = 24
        new_duration = otio.opentime.RationalTime(event.measured_sec * rate, rate)
        clip.source_range = otio.opentime.TimeRange(
            start_time=clip.source_range.start_time,
            duration=new_duration,
        )
        if slot_addr in self.slots:
            self.slots[slot_addr]["measured_sec"] = event.measured_sec
            self.slots[slot_addr]["status"] = "measured"

    def _delete_scene(self, event: Effect) -> None:
        """Remove all slots belonging to a scene."""
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
        """Reorder tracks according to new_order.

        new_order[i] is the scene_num that should occupy position i+1.
        All clips belonging to a scene move with it. Clips are reinserted
        into the track in the new scene order while preserving their
        relative phrase order within each scene.
        """
        track_name = "A1_Narration"
        if not self.timeline.tracks:
            return
        track = self.timeline.tracks[0]

        # Group clips by scene_num
        scene_to_clips: dict[int, list[otio.schema.Clip]] = defaultdict(list)
        for child in list(track):
            if isinstance(child, otio.schema.Clip):
                # slot_addr format: "A1_Narration:scene_num:block_id"
                parts = child.name.split(":")
                if len(parts) >= 2:
                    try:
                        scene_num = int(parts[1])
                        scene_to_clips[scene_num].append(child)
                    except ValueError:
                        pass

        # Build new clip order
        new_clips: list[otio.schema.Clip] = []
        for scene_num in event.new_order:
            clips = scene_to_clips.get(scene_num, [])
            # Sort clips within scene by block_id for stable ordering
            clips.sort(key=lambda c: c.name)
            new_clips.extend(clips)

        # Rebuild track
        track.clear_children()
        for clip in new_clips:
            track.append(clip)

        # Rebuild slots dict to match new order
        new_slots: dict[str, dict] = {}
        for clip in new_clips:
            if clip.name in self.slots:
                new_slots[clip.name] = self.slots[clip.name]
        self.slots = new_slots

    def _remove_clip(self, event: Effect) -> None:
        """Remove a clip from the timeline (e.g., rejected media)."""
        clip = self._find_clip_by_name(event.slot_id)
        if clip is not None:
            track = clip.parent()
            if track is not None:
                track.remove(clip)

    def _find_clip_by_name(self, name: str) -> Optional[otio.schema.Clip]:
        for track in self.timeline.tracks:
            for child in track:
                if isinstance(child, otio.schema.Clip) and child.name == name:
                    return child
        return None

    def all_slots_filled(self) -> bool:
        """Return True if every narration slot has a delivered audio clip."""
        slots = getattr(self, "slots", {})
        if not slots:
            return False
        return all(s.get("status") == "delivered" for s in slots.values())

    def get_timeline_duration_sec(self) -> float:
        """Return timeline duration in seconds."""
        dur = self.timeline.duration()
        return dur.value / dur.rate if dur and dur.rate else 0.0

    def summary(self) -> str:
        total = len(self.slots)
        measured = sum(1 for s in self.slots.values() if s["status"] == "measured")
        delivered = sum(1 for s in self.slots.values() if s["status"] == "delivered")
        dirty = sum(1 for s in self.slots.values() if s["status"] == "dirty")
        scenes = len({s["scene_num"] for s in self.slots.values()})
        return (
            f"OTIO: {scenes} scenes, {total} slots, "
            f"{measured} measured, {delivered} delivered, {dirty} dirty"
        )
```


#### 6.2.2 Validation: no_overlaps, track_alignment, clip_media

Three validation methods support agent decision-making. Each returns `(bool, Optional[str])`: `True` with no message on success, `False` with a descriptive error on failure.

```python
    def validate_no_overlaps(self) -> tuple[bool, Optional[str]]:
        """Check that no two clips on the same track overlap in time.

        Transitions are skipped — overlapping a Transition with a Clip
        is valid OTIO behavior.
        """
        for track in self.timeline.tracks:
            children = list(track)
            for i in range(len(children) - 1):
                a, b = children[i], children[i + 1]
                if isinstance(a, otio.schema.Transition) or isinstance(
                    b, otio.schema.Transition
                ):
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
        """Check that all tracks have the same duration.

        A documentary has one coherent timeline — all tracks must span
        the same time range. Returns False if the max track duration
        differs from the timeline duration.
        """
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
        """Check that every clip has a valid media reference.

        A clip passes if it has a non-MissingReference media target
        and its trimmed_range resolves without exception.
        """
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
```

#### 6.2.3 Slot addressing scheme (track:scene:slot)

Every slot in the timeline has a canonical address of the form `track_name:scene_num:block_id`. Example: `"A1_Narration:3:block_b"` identifies block `block_b` in scene 3 on the A1 (audio narration) track. This addressing scheme is used in:

- `QueueJob.slot_id` — the slot a job targets
- `MergeIntoOTIO.slot_id` — where to insert the produced clip
- `DurationAdjusted.block_id` — which slot's duration changed


The `Timeline._find_clip_by_name()` method resolves a slot address to its `otio.schema.Clip` by iterating tracks and matching `clip.name == slot_addr`.

---

### 6.3 Job Projection

#### 6.3.1 Job lifecycle tracking (pending → running → completed/failed)

`Jobs` tracks the state of every job in the pipeline. A job passes through the lifecycle: `pending` → `running` → `completed` or `failed`. Jobs can be requeued (return to `pending` with updated parameters).

**V7 critical fix:** `production_failures` is now consumed when `UpdateScript` fixes the affected blocks. This prevents infinite script-rewrite loops.

```python
from collections import defaultdict


class JobState:
    """Mutable record for a single job's current state."""

    def __init__(self, job_id: str, job_type: str, slot_id: str) -> None:
        self.job_id: str = job_id
        self.job_type: str = job_type          # "tts" | "ltx"
        self.slot_id: str = slot_id
        self.status: str = "pending"           # pending | running | completed | failed
        self.params: dict[str, Any] = {}
        self.artifact_uri: Optional[str] = None
        self.duration_sec: Optional[float] = None
        self.error_message: Optional[str] = None
        self.requeue_count: int = 0
        self.created_at: float = 0.0
        self.completed_at: Optional[float] = None


class Jobs(Projection):
    """Tracks job lifecycle, reconciliation state, budget, and production failures.

    V7 additions:
    - ``dirty_blocks`` / ``clean_blocks``: per-block authority tracking
    - ``block_attempts``: per-block retry counter, bounded by max_attempts
    - ``spent_usd``: cumulative budget accumulator
    - ``production_failures``: list of unrecoverable production failures
      that have not yet been resolved. Cleared when UpdateScript fixes them.
    """

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
        match event.kind:
            # --- Job lifecycle ---
            case "queue_job":
                self._on_queue(event)
            case "job_started":
                self._on_start(event)
            case "job_completed":
                self._on_complete(event)
            case "job_failed":
                self._on_fail(event)
            case "job_requeued":
                self._on_requeue(event)
            # --- Reconciliation state ---
            case "reconciliation_complete":
                self.reconciliation_complete = True
                self.dirty_blocks.clear()
            case "reconciliation_failed":
                self.reconciliation_complete = False
            # --- Budget ---
            case "budget_set":
                pass  # Budget handles this
            case "budget_exceeded":
                pass  # Handled by append guard
            case "pipeline_aborted":
                pass
            # --- Production failures ---
            case "production_failed":
                self.production_failures.append({
                    "slot_id": getattr(event, "slot_id", ""),
                    "failure_type": getattr(event, "failure_type", ""),
                    "expected": getattr(event, "expected", ""),
                    "actual": getattr(event, "actual", ""),
                    "suggested_fix": getattr(event, "suggested_fix", ""),
                })
            # --- Failure resolution ---
            case "update_script":
                self._resolve_failures_on_script_update(event)
                # Also sync dirty/clean from Timeline
                self._sync_from_otio(event)
    def _on_queue(self, event: Effect) -> None:
        job_id = event.job_id
        if job_id not in self.jobs:
            job = JobState(
                job_id=job_id,
                job_type=event.job_type,
                slot_id=getattr(event, "slot_id", ""),
            )
            job.params = getattr(event, "params", {})
            job.created_at = getattr(event, "timestamp", 0.0)
            self.jobs[job_id] = job
            # Track attempt for TTS jobs (block-level retry counting)
            block_id = getattr(event, "slot_id", None)
            if block_id and event.job_type == "tts":
                self.block_attempts[block_id] += 1

    def _on_start(self, event: Effect) -> None:
        job = self.jobs.get(event.job_id)
        if job:
            job.status = "running"

    def _on_complete(self, event: Effect) -> None:
        job = self.jobs.get(event.job_id)
        if job:
            job.status = "completed"
            job.artifact_uri = getattr(event, "artifact_uri", None)
            job.duration_sec = getattr(event, "duration_sec", None)
            job.completed_at = getattr(event, "timestamp", None)
            # Mark block clean on successful completion
            block_id = job.slot_id
            if block_id in self.dirty_blocks:
                self.dirty_blocks.discard(block_id)
                self.clean_blocks.add(block_id)

    def _on_fail(self, event: Effect) -> None:
        job = self.jobs.get(event.job_id)
        if job:
            job.status = "failed"
            job.error_message = getattr(event, "error_message", "unknown")

    def _on_requeue(self, event: Effect) -> None:
        job = self.jobs.get(event.job_id)
        if job:
            job.status = "pending"
            job.requeue_count += 1
            job.error_message = None
            if getattr(event, "new_params", None):
                job.params.update(event.new_params)
            # Requeueing a block resets it to dirty
            if job.slot_id:
                self.dirty_blocks.add(job.slot_id)
                self.clean_blocks.discard(job.slot_id)

    def _sync_from_otio(self, event: UpdateScript) -> None:
        """Sync dirty/clean blocks from Timeline after script update.

        The Timeline has already marked changed blocks as dirty
        (status='scripted') and unchanged blocks retain their measured state.
        We derive dirty/clean sets from the script update directly:
        - Updated block_ids are dirty
        - All other known blocks are clean
        """
        updated_block_ids = {f"A1_Narration:{b.scene_num}:{b.block_id}" for b in event.blocks}
        # Mark updated blocks dirty
        for bid in updated_block_ids:
            self.dirty_blocks.add(bid)
            self.clean_blocks.discard(bid)
        # All production failures for resolved blocks are consumed

    def _resolve_failures_on_script_update(self, event: UpdateScript) -> None:
        """Remove production failures for blocks that were fixed by UpdateScript.

        When the parser extracts UpdateScript from Scenario Agent output to fix a voice_mismatch
        or gap_unexpected, the failures for the affected blocks are consumed
        and removed from the list. This prevents infinite script-rewrite loops.
        """
        updated_block_ids = {f"A1_Narration:{b.scene_num}:{b.block_id}" for b in event.blocks}
        self.production_failures = [
            f for f in self.production_failures
            if not (
                f.get("slot_id") in updated_block_ids
                and f.get("failure_type") in self.SCRIPT_RESOLVABLE_TYPES
            )
        ]

    # --- Query methods for agents ---

    def has_pending_or_running_jobs(self, job_type: Optional[str] = None) -> bool:
        """Return True if any job matches status and optional type filter."""
        for job in self.jobs.values():
            if job.status in ("pending", "running"):
                if job_type is None or job.job_type == job_type:
                    return True
        return False

    def pending_jobs(self, job_type: Optional[str] = None) -> list[JobState]:
        """Return all pending jobs, optionally filtered by type."""
        return [
            j for j in self.jobs.values()
            if j.status == "pending" and (job_type is None or j.job_type == job_type)
        ]

    def block_attempts_exceeded(self, block_id: str, max_attempts: int = 5) -> bool:
        """Check if a block has exceeded its per-block attempt limit."""
        return self.block_attempts.get(block_id, 0) >= max_attempts

    def budget_exceeded(self, max_budget_usd: float = 10.0) -> bool:
        """Check if cumulative spend exceeds the per-run budget."""
        return self.spent_usd >= max_budget_usd

    def is_block_clean(self, block_id: str) -> bool:
        """Return True if a block has measured audio and is authoritative."""
        return block_id in self.clean_blocks

    def all_blocks_clean(self, block_ids: list[str]) -> bool:
        """Return True if every block in the list is clean."""
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
            f"tts={by_type['tts']}, video={by_type['video']} | "
            f"reconciled={'yes' if self.reconciliation_complete else 'no'} | "
            f"dirty={len(self.dirty_blocks)} clean={len(self.clean_blocks)} | "
            f"spent=${self.spent_usd:.4f}"
        )
```

#### 6.3.2 Reconciliation state: complete flag, dirty/clean block tracking

The `reconciliation_complete` flag is set by `ReconciliationComplete` and cleared by `ReconciliationFailed`. Agents read this flag to determine whether to begin video generation. Transition from `AUDIO_RECONCILE` to `VIDEO_PRODUCTION` is emergent: when `reconciliation_complete == True` and no dirty blocks remain, the Video Agent may begin work.

Dirty/clean tracking enables partial reconciliation after script back-edges. When `voice_mismatch` routes from `VIDEO_PRODUCTION` back to `SCRIPT`, the Scenario Agent fixes the script; the parser extracts `UpdateScript`. The `Timeline` marks changed blocks **dirty** (status="scripted", need re-TTS) and unchanged blocks **clean** (retain measured_sec). The `Jobs` syncs its `dirty_blocks`/`clean_blocks` sets from the `Timeline` on every `UpdateScript`. This avoids discarding the entire audio pipeline for a single-scene typo fix.

| Field | Type | Meaning |
|---|---|---|
| `reconciliation_complete` | `bool` | `True` when all blocks have measured audio within tolerance |
| `dirty_blocks` | `set[str]` | Slot addresses needing re-reconciliation |
| `clean_blocks` | `set[str]` | Slot addresses with authoritative measured audio |

#### 6.3.3 Attempt counter per block, budget accumulator per run

Per-block attempt counting prevents any single narration block from consuming infinite retries. Each time a `QueueJob` event targets a TTS slot, `block_attempts[slot_id]` increments. When `block_attempts[slot_id] >= max_attempts` (default 5), the parser extracts `ReconciliationFailed` from Audio Agent output with `failure_type="duration_unrecoverable"`, triggering a back-edge to `SCRIPT`.

Per-run budget tracking prevents aggregate runaway. `spent_usd` is a projection field that the operator may inspect via `GET /` on any agent. Budget enforcement is manual: the operator monitors spend and issues `PipelineAborted` with `reason="budget_exceeded"` if the run exceeds `max_run_budget_usd` (default $10.00). No automatic cost-tracking events are emitted.

#### 6.3.4 Production failures list

`production_failures` collects all `ProductionFailed` events that have not yet been resolved. Each entry is a dictionary with `slot_id`, `failure_type`, `expected`, `actual`, and `suggested_fix`. Agents use this list to detect unrecoverable errors: failures with `failure_type` in `{gap_unexpected, voice_mismatch}` trigger the script back-edge; all other types either requeue in the current phase or halt with `ClarificationRequest`.

**Critical V7 fix:** `UpdateScript` events now consume resolved failures. When the parser extracts `UpdateScript` from Scenario Agent output to fix a `voice_mismatch` or `gap_unexpected`, the `Jobs` removes the matching failures from the list. This prevents infinite script-rewrite loops.


---

### 6.4 VM Projection

#### 6.4.1 VM inventory: instance_id → {status, role, cost, worker_url}

`VMs` maintains a pure read model of the VM fleet. It applies `VMAllocated`, `VMDeallocated`, `VMObserved`, and `VMProvisionFailed` events. Each VM record tracks:

| Field | Type | Meaning |
|---|---|---|
| `status` | `str` | `active`, `destroyed`, `provisioning`, `failed` |
| `role` | `str` | `tts`, `video`, or `whisperx` — the job type this VM serves |
| `offer_id` | `str` | Vast.ai offer ID used for provisioning |
| `worker_url` | `str` | HTTP endpoint of the VM agent process |
| `hourly_rate_usd` | `float` | Cost per hour for this instance |
| `started_at` | `float` | Unix timestamp of allocation |
| `observed_status` | `str` | Last status from `VMObserved` (may differ from event-derived status) |

```python
from dataclasses import dataclass, field


@dataclass
class VMRecord:
    """Read-only record of a single VM's state."""

    instance_id: str
    status: str = "active"
    role: str = ""
    offer_id: str = ""
    worker_url: str = ""
    hourly_rate_usd: float = 0.0
    started_at: float = 0.0
    observed_status: Optional[str] = None


class VMs(Projection):
    """Pure read model of the VM fleet. No polling, no event emission.

    The parser extracts ``VMObserved`` from the Provisioner's text when Vast.ai state diverges
    from event-derived state; this projection applies them passively.
    """

    def __init__(self) -> None:
        super().__init__()
        self.vms: dict[str, VMRecord] = {}

    def apply(self, event: Effect) -> None:
        match event.kind:
            case "vm_allocated":
                self.vms[event.instance_id] = VMRecord(
                    instance_id=event.instance_id,
                    status="active",
                    role=getattr(event, "role", ""),
                    offer_id=getattr(event, "offer_id", ""),
                    worker_url=getattr(event, "worker_url", ""),
                    hourly_rate_usd=getattr(event, "cost_per_hour", 0.0),
                    started_at=getattr(event, "timestamp", 0.0),
                )
            case "vm_deallocated":
                rec = self.vms.get(event.instance_id)
                if rec:
                    rec.status = "destroyed"
            case "vm_observed":
                rec = self.vms.get(event.instance_id)
                if rec:
                    rec.observed_status = getattr(event, "observed_status", None)
                    # If Vast.ai reports the instance gone but events say active,
                    # update status to reflect reality (Provisioner handles cleanup).
                    if rec.observed_status == "not_found" and rec.status == "active":
                        rec.status = "observed_gone"
            case "vm_provision_failed":
                # No VM record created — failure is logged by the Provisioner.
                pass

    def active_vms(self, role: Optional[str] = None) -> list[VMRecord]:
        """Return VMs with status == 'active', optionally filtered by role."""
        return [
            v for v in self.vms.values()
            if v.status == "active" and (role is None or v.role == role)
        ]

    def estimated_hourly_cost(self) -> float:
        """Sum of hourly rates for all active VMs."""
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
```

#### 6.4.2 Pure read model — no polling, no event emission

`VMs` has no `poll_vastai()` method. Vast.ai drift detection lives in the Provisioner agent. The Provisioner queries the GSA for `VMs` state, runs Vast.ai CLI commands via its bash tool, compares Vast.ai reality against projection state, and describes divergence in its natural language output. The parser extracts `VMObserved` effects from that text. This preserves the projection invariant: projections are read models only; they consume events, they do not produce them.

---

### 6.5 Budget Projection

#### 6.5.1 Budget cap, spent, and remaining tracking

`Budget` tracks the pipeline's financial state. It applies `BudgetSet`, `VMDeallocated`, and `PipelineAborted` events. The projection computes `remaining_usd` dynamically and flags when the budget is exceeded.

```python
class BudgetProjection(Projection):
    """Tracks budget cap, cumulative spend, and per-run cost accrual.

    The budget cap is set once per run via BudgetSet (typically at pipeline
    start). All subsequent VM costs are accumulated from `VMDeallocated.final_cost`
    when VMs are destroyed.
    """

    def __init__(self) -> None:
        super().__init__()
        self.budget_cap_usd: float = 0.0
        self.spent_usd: float = 0.0
        self.vm_costs: dict[str, float] = {}  # instance_id -> accumulated cost
        self.exceeded: bool = False
        self.exceeded_at: Optional[float] = None

    def apply(self, event: Effect) -> None:
        match event.kind:
            case "budget_set":
                self.budget_cap_usd = getattr(event, "budget_usd", 0.0)
                self.exceeded = False
            case "vm_deallocated":
                instance_id = getattr(event, "instance_id", "")
                cost = getattr(event, "final_cost", 0.0)
                if instance_id:
                    self.vm_costs[instance_id] = self.vm_costs.get(instance_id, 0.0) + cost
                self.spent_usd += cost
                if not self.exceeded and self.budget_cap_usd > 0 and self.spent_usd > self.budget_cap_usd:
                    self.exceeded = True
                    self.exceeded_at = getattr(event, "timestamp", 0.0)
            case "pipeline_aborted":
                if getattr(event, "reason", "") == "budget_exceeded":
                    self.exceeded = True

    def remaining_usd(self) -> float:
        """Return remaining budget. Negative if exceeded."""
        return self.budget_cap_usd - self.spent_usd

    def summary(self) -> str:
        pct = (self.spent_usd / self.budget_cap_usd * 100) if self.budget_cap_usd > 0 else 0.0
        status = "EXCEEDED" if self.exceeded else "OK"
        return (
            f"Budget: ${self.spent_usd:.2f} / ${self.budget_cap_usd:.2f} "
            f"({pct:.1f}%, {status})"
        )
```

**Budget enforcement.** The handler checks `Budget.exceeded` before appending any new effect that would incur cost (e.g., `VMAllocated`). If exceeded, the handler rejects the effect and returns a `ClarificationRequest` to the agent. This is a soft guard — the agent can still produce text from which the parser extracts `PipelineAborted` or `HumanInstruction` effects.

---

### 6.6 State Projection

#### 6.6.1 Current phase + transition history (descriptive only)

`StateProjection` tracks the emergent pipeline phase for human observation and the full history of phase changes. It does not enforce anything — agents decide what to do based on their own reading of projections.

```python
@dataclass
class PhaseChangeRecord:
    """A single phase change (descriptive, not a transition)."""

    from_phase: str
    to_phase: str
    reason: str
    at_sequence: int


class StateProjection(Projection):
    """Tracks emergent pipeline phase and phase change history.

    Also maintains a ring buffer of recent effects per agent for loop
    detection. Agents check this buffer on every turn.
    """

    def __init__(self, loop_buffer_size: int = 5) -> None:
        super().__init__()
        self.current_phase: str = "init"
        self.phase_history: list[PhaseChangeRecord] = []
        # Ring buffer: last N effects per agent (agent_name -> deque[Effect])
        self.recent_effects: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=loop_buffer_size)
        )
        self.loop_buffer_size: int = loop_buffer_size

    def apply(self, event: Effect) -> None:
        # Record effect in agent's ring buffer for loop detection
        agent = getattr(event, "agent", None) or getattr(event, "source_agent", "unknown")
        if agent:
            self.recent_effects[agent].append(event)

        match event.kind:
            case "pipeline_started":
                self.current_phase = "init"
                self.phase_history.clear()
                self.recent_effects.clear()
            case "reconciliation_complete":
                if self.current_phase != "audio_reconcile":
                    self._record_phase_change("audio_reconcile")
            case "pipeline_complete":
                self._record_phase_change("done")
            case "pipeline_aborted":
                self._record_phase_change("aborted")
            case "merge_into_otio":
                # Crude phase inference for observation
                if event.track_name == "V1_Video" and self.current_phase == "audio_reconcile":
                    self._record_phase_change("video_production")

    def _record_phase_change(self, to_phase: str, reason: str = "") -> None:
        rec = PhaseChangeRecord(
            from_phase=self.current_phase,
            to_phase=to_phase,
            reason=reason,
            at_sequence=getattr(self, "last_sequence", 0),
        )
        self.phase_history.append(rec)
        self.current_phase = to_phase

    def get_recent_events(self, n: int) -> list[Effect]:
        """Return the last N effects across all agents."""
        all_events = []
        for agent_deque in self.recent_effects.values():
            all_events.extend(list(agent_deque))
        all_events.sort(key=lambda e: getattr(e, "timestamp", 0), reverse=True)
        return all_events[:n]

    def summary(self) -> str:
        tx_count = len(self.phase_history)
        return (
            f"Phase: {self.current_phase}, "
            f"{tx_count} phase changes, "
            f"{len(self.recent_effects)} agents tracked"
        )
```

#### 6.6.2 Loop detection buffer (last N effects per agent)

The `recent_effects` dictionary maps agent name to a `deque` of that agent's last `loop_buffer_size` effects (default 5). On every `apply`, the effect is appended to the deque for its agent. Because `deque` has `maxlen`, old effects are automatically evicted — the buffer is a fixed-size ring buffer with O(1) append and no allocation on overflow.

Agents use this buffer to detect two loop conditions:

1. **Duplicate effects**: all N entries in a deque are the same `kind` with the same key parameters.
2. **No progress**: after N effects from an agent, no projection state has changed (OTIO, jobs, or VM state delta is empty).

```python
    def detect_duplicate_loop(self, agent: str, threshold: int = 5) -> tuple[bool, str]:
        """Check if an agent has produced the same effect kind N times in a row.

        Returns (is_looping, reason).
        """
        buf = self.recent_effects.get(agent, deque())
        if len(buf) < threshold:
            return False, "insufficient history"
        kinds = [getattr(e, "kind", "") for e in buf]
        if len(set(kinds)) == 1:
            return True, f"{agent} produced {kinds[0]} {len(buf)} times"
        return False, "effects vary"

    def get_recent_kinds(self, agent: str) -> list[str]:
        """Return the list of recent effect kinds for an agent."""
        return [getattr(e, "kind", "") for e in self.recent_effects.get(agent, [])]
```

When either condition triggers, the parser extracts `AgentLoopDetected` from agent output with context (agent name, effect history, projection delta) and halts; the parser extracts `ClarificationRequest` for human review. The threshold is configurable per agent (default 5) via the agent's config table.

---

### 6.7 Projection Response Schemas

Projection classes (§6.2–§6.6) are mutable event consumers with methods. They are **not** Pydantic models and cannot be serialized directly over HTTP. The Global State Agent (§2.4.2) returns a `GlobalStateResponse` containing the serializable state of every projection. This section defines the Pydantic response schemas.

#### 6.7.1 OTIO Slot State

```python
class OTIOSlotState(BaseModel):
    """Serializable state of a single slot in the OTIO timeline."""
    scene_num: int
    block_id: str
    speaker: str
    text: str
    scripted_sec: float
    measured_sec: float | None = None
    status: Literal["scripted", "measured", "delivered"] = "scripted"
    # V7.1 fix: Removed "dirty" — the projection never produces it.
    # "scripted" IS the dirty state (blocks awaiting reconciliation).
    artifact_uri: str | None = None


class OTIOResponse(BaseModel):
    """Serializable OTIO projection state for GSA GET /."""
    scenes: int = Field(..., description="number of distinct scene numbers")
    total_slots: int
    measured_slots: int
    delivered_slots: int
    dirty_slots: int
    duration_sec: float = Field(..., description="total timeline duration in seconds")
    slots: dict[str, OTIOSlotState] = Field(..., description="slot_addr -> state")
```

#### 6.7.2 Job Response

```python
class JobResponseItem(BaseModel):
    """Serializable state of a single job."""
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


class JobResponse(BaseModel):
    """Serializable Job projection state for GSA GET /."""
    jobs: dict[str, JobResponseItem] = Field(..., description="job_id -> state")
    reconciliation_complete: bool = False
    dirty_blocks: list[str] = Field(default_factory=list, description="slot_addrs needing work")
    clean_blocks: list[str] = Field(default_factory=list, description="slot_addrs with measured audio")
    block_attempts: dict[str, int] = Field(default_factory=dict, description="slot_addr -> attempt count")
    spent_usd: float = 0.0
    production_failures: list[dict] = Field(default_factory=list)
```

#### 6.7.3 VM Response

```python
class VMResponseItem(BaseModel):
    """Serializable state of a single VM."""
    instance_id: str
    status: Literal["active", "destroyed", "observed_gone"]
    role: Literal["tts", "ltx", ""]
    offer_id: str = ""
    worker_url: str = ""
    hourly_rate_usd: float = 0.0
    started_at: float = 0.0
    observed_status: str | None = None


class VMResponse(BaseModel):
    """Serializable VM projection state for GSA GET /."""
    vms: dict[str, VMResponseItem] = Field(..., description="instance_id -> state")
    active_count: int
    total_count: int
    estimated_hourly_cost_usd: float
    role_breakdown: dict[str, int] = Field(default_factory=dict, description="role -> active count")
```

#### 6.7.4 State Response

```python
class PhaseChangeItem(BaseModel):
    """Serializable phase change record."""
    from_phase: str
    to_phase: str
    reason: str
    at_sequence: int


class StateResponse(BaseModel):
    """Serializable State projection state for GSA GET /."""
    current_phase: str = "init"
    phase_changes: list[PhaseChangeItem] = Field(default_factory=list)
    agents_tracked: list[str] = Field(default_factory=list)
    latest_sequence: int = 0
```

#### 6.7.5 Budget Response

```python
class BudgetResponse(BaseModel):
    """Serializable Budget projection state for GSA GET /."""
    budget_cap_usd: float = 0.0
    spent_usd: float = 0.0
    remaining_usd: float = 0.0
    exceeded: bool = False
    vm_costs: dict[str, float] = Field(default_factory=dict, description="instance_id -> accumulated cost")
```

#### 6.7.6 GlobalStateResponse (updated)

```python
class GlobalStateResponse(BaseModel):
    """Response from GET / on the Global State Agent."""
    timestamp: float
    otio: OTIOResponse          # §6.7.1
    jobs: JobResponse           # §6.7.2
    vms: VMResponse             # §6.7.3
    state: StateResponse        # §6.7.4
    budget: BudgetResponse      # §6.7.5
    latest_sequence: int        # highest event sequence number included
```

**Size estimate.** A typical mid-run documentary produces ~50 slots, 20 jobs, 3 VMs, and 5 phase changes. Serialized JSON:
- `OTIOResponse`: ~15 KB (slots contain full narration text)
- `JobResponse`: ~4 KB
- `VMResponse`: ~1 KB
- `StateResponse`: ~0.5 KB
- `BudgetResponse`: ~0.5 KB
- **Total: ~21 KB** (before compression; ~3.5 KB with gzip)

---

