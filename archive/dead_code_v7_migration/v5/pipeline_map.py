"""Pipeline Map — computed dependency graph for agent guidance.

The PipelineMap is a read-only projection that builds a compact,
human-readable map of what exists, what's missing, and what
*typically* comes next.  It is purely advisory.  Agents may ignore it.

The map is computed heuristically from the event log:
- "Scripted" means an UpdateScript or AudioGenerated/VideoCompleted exists
- "Queued" means QueueJob exists for this block
- "Generated" means AudioCompleted/VideoCompleted exists
- "Measured" means AudioMeasured exists
- "Approved" means JobApproved exists
- "Merged" means MergeIntoOTIO exists

The "← EXPECTED" markers are computed from a simple pattern table:
  scripted → queued → generated → measured → adjusted/approved → merged
These patterns are defaults, not rules. Agents are free to deviate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Heuristic pattern table — the "good will" typical flow
# ---------------------------------------------------------------------------
# This is the ONLY place the "expected sequence" is defined.
# It is advisory.  Agents may skip steps, reorder, or invent new ones.
# ---------------------------------------------------------------------------

AUDIO_SEQUENCE = [
    "scripted",
    "queued",
    "generated",
    "measured",
    "judged",      # duration_adjusted OR reconciliation_failed
    "approved",
    "merged",
]

VIDEO_SEQUENCE = [
    "scripted",    # from authoritative OTIO (post-reconciliation)
    "queued",
    "generated",
    "approved",
    "merged",
]


# ---------------------------------------------------------------------------
# Block state tracker
# ---------------------------------------------------------------------------

@dataclass
class BlockMap:
    """The advisory map for one narration or video block."""

    block_id: str
    track: str
    scene_num: int
    phrase_idx: int
    voice: str = ""
    text: str = ""
    duration_scripted: float = 0.0

    # Effect presence flags (what has happened)
    has_scripted: bool = False
    has_queued: bool = False
    has_generated: bool = False
    has_measured: bool = False
    has_judged: bool = False
    has_approved: bool = False
    has_merged: bool = False

    # Retry / failure tracking
    retry_count: int = 0
    last_failure: str = ""

    @property
    def current_stage(self) -> str:
        """Return the latest stage reached."""
        if self.has_merged:
            return "merged"
        if self.has_approved:
            return "approved"
        if self.has_judged:
            return "judged"
        if self.has_measured:
            return "measured"
        if self.has_generated:
            return "generated"
        if self.has_queued:
            return "queued"
        if self.has_scripted:
            return "scripted"
        return "gap"

    @property
    def expected_next(self) -> str | None:
        """Return the *typical* next stage, or None if at end."""
        seq = AUDIO_SEQUENCE if self.track == "A1_Narration" else VIDEO_SEQUENCE
        cur = self.current_stage
        try:
            idx = seq.index(cur)
            if idx + 1 < len(seq):
                return seq[idx + 1]
        except ValueError:
            pass
        return None

    @property
    def is_complete(self) -> bool:
        return self.has_merged

    def to_prompt_line(self) -> str:
        """One-line summary for agent context."""
        exp = self.expected_next
        exp_mark = f" ← EXPECTED: {exp}" if exp and not self.is_complete else ""
        fail = f" [FAIL: {self.last_failure}]" if self.last_failure else ""
        retry = f" (retry {self.retry_count})" if self.retry_count else ""
        return (
            f"  {self.block_id}: {self.current_stage}{retry}{fail}{exp_mark}"
        )


# ---------------------------------------------------------------------------
# Pipeline Map projection
# ---------------------------------------------------------------------------

class PipelineMap:
    """Computed advisory map.  Not a state machine.  Not enforced."""

    def __init__(self) -> None:
        self.blocks: dict[str, BlockMap] = {}
        self.reconciliation_complete: bool = False
        self.last_sequence: int = 0

    # ------------------------------------------------------------------
    # Event application (incremental)
    # ------------------------------------------------------------------

    def apply(self, event: dict[str, Any]) -> None:
        kind = event.get("kind", "")
        payload = event.get("payload", {})

        match kind:
            case "update_script":
                self._on_script_update(payload)
            case "audio_generated":
                self._on_audio_generated(payload)
            case "audio_completed":
                self._on_audio_completed(payload)
            case "video_completed":
                self._on_video_completed(payload)
            case "queue_job":
                self._on_queued(payload)
            case "audio_measured":
                self._on_measured(payload)
            case "duration_adjusted":
                self._on_judged(payload, passed=True)
            case "reconciliation_failed":
                self._on_judged(payload, passed=False)
            case "job_approved":
                self._on_approved(payload)
            case "merge_into_otio":
                self._on_merged(payload)
            case "job_requeued":
                self._on_requeued(payload)
            case "reconciliation_complete":
                self.reconciliation_complete = True
            case "delete_from_otio" | "delete_scene":
                self._on_delete(payload)

    def _get_block(self, block_id: str, track: str) -> BlockMap:
        if block_id not in self.blocks:
            self.blocks[block_id] = BlockMap(
                block_id=block_id, track=track,
                scene_num=0, phrase_idx=0,
            )
        return self.blocks[block_id]

    def _on_script_update(self, p: dict) -> None:
        for voice in p.get("voices", []):
            block_id = f"A1:{p['scene_num']}:{voice.get('phrase_idx', 0)}"
            b = self._get_block(block_id, "A1_Narration")
            b.scene_num = p["scene_num"]
            b.phrase_idx = voice.get("phrase_idx", 0)
            b.voice = voice.get("voice", "")
            b.text = voice.get("text", "")
            b.duration_scripted = voice.get("duration_sec", 0.0)
            b.has_scripted = True

    def _on_queued(self, p: dict) -> None:
        track = "A1_Narration" if p.get("job_type") == "tts" else "V1_Video"
        bid = f"{track}:{p['scene_num']}:{p.get('phrase_idx', 0)}"
        b = self._get_block(bid, track)
        b.has_queued = True

    def _on_audio_generated(self, p: dict) -> None:
        bid = f"A1:{p['scene_num']}:{p['phrase_idx']}"
        b = self._get_block(bid, "A1_Narration")
        b.has_generated = True

    def _on_audio_completed(self, p: dict) -> None:
        bid = f"A1:{p.get('scene_num', 0)}:{p.get('phrase_idx', 0)}"
        b = self._get_block(bid, "A1_Narration")
        b.has_generated = True

    def _on_video_completed(self, p: dict) -> None:
        bid = f"V1:{p.get('scene_num', 0)}:{p.get('phrase_idx', 0)}"
        b = self._get_block(bid, "V1_Video")
        b.has_generated = True

    def _on_measured(self, p: dict) -> None:
        bid = f"A1:{p['scene_num']}:{p['phrase_idx']}"
        b = self._get_block(bid, "A1_Narration")
        b.has_measured = True

    def _on_judged(self, p: dict, passed: bool) -> None:
        bid = f"A1:{p['scene_num']}:{p.get('phrase_idx', 0)}"
        b = self._get_block(bid, "A1_Narration")
        b.has_judged = True
        if not passed:
            b.last_failure = p.get("failure_type", "reconciliation_failed")

    def _on_approved(self, p: dict) -> None:
        # Find block by job_id (would need job->block mapping in practice)
        for b in self.blocks.values():
            b.has_approved = True  # simplified

    def _on_merged(self, p: dict) -> None:
        track = p.get("track", "A1_Narration")
        bid = f"{track}:{p['scene_num']}:{p['phrase_idx']}"
        b = self._get_block(bid, track)
        b.has_merged = True

    def _on_requeued(self, p: dict) -> None:
        # Find block by job_id
        for b in self.blocks.values():
            b.retry_count += 1
            b.has_queued = False  # will be re-queued
            b.has_generated = False
            b.has_measured = False
            b.has_judged = False
            b.has_approved = False

    def _on_delete(self, p: dict) -> None:
        # Remove blocks
        to_remove = [
            bid for bid, b in self.blocks.items()
            if b.scene_num == p.get("scene_num")
        ]
        for bid in to_remove:
            del self.blocks[bid]

    # ------------------------------------------------------------------
    # Summary for agent prompts
    # ------------------------------------------------------------------

    def summary(self) -> str:
        """Compact human-readable map for agent context."""
        lines: list[str] = []
        lines.append("=== PIPELINE MAP (advisory — ignore if you have a better plan) ===")

        # Group by scene
        scenes: dict[int, list[BlockMap]] = {}
        for b in self.blocks.values():
            scenes.setdefault(b.scene_num, []).append(b)

        for scene_num in sorted(scenes):
            lines.append(f"\nScene {scene_num}:")
            for b in sorted(scenes[scene_num], key=lambda x: x.phrase_idx):
                lines.append(b.to_prompt_line())

        # Global stats
        total = len(self.blocks)
        complete = sum(1 for b in self.blocks.values() if b.is_complete)
        audio_done = self.reconciliation_complete
        lines.append(f"\nStats: {complete}/{total} blocks merged | Reconciliation: {'done' if audio_done else 'in progress'}")

        return "\n".join(lines)

    def tick(self, event_store: Any) -> None:
        """Incremental update from event store."""
        events = event_store.read_since(self.last_sequence)
        for ev in events:
            self.apply(ev)
            self.last_sequence = ev.get("sequence", self.last_sequence)


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pm = PipelineMap()
    pm.apply({
        "kind": "update_script",
        "payload": {
            "scene_num": 1,
            "voices": [
                {"phrase_idx": 0, "voice": "narrator", "text": "In 1924...", "duration_sec": 4.5},
                {"phrase_idx": 1, "voice": "narrator", "text": "The economy...", "duration_sec": 3.2},
            ],
        },
    })
    pm.apply({
        "kind": "queue_job",
        "payload": {"job_type": "tts", "scene_num": 1, "phrase_idx": 0},
    })
    print(pm.summary())
