#!/usr/bin/env python3
"""
Apply all V7.1 architecture fixes based on external review comments.

Strategy: read the doc, apply targeted replacements, write back.
Each fix is documented inline with its issue number from the review.
"""

from pathlib import Path
import re

DOC = Path("/Users/orpington/Documents/economy-documentary-work/ARCHITECTURE_V7.1.md")
text = DOC.read_text()

replacements_made = []

def rep(old, new, label):
    global text
    if old in text:
        text = text.replace(old, new, 1)
        replacements_made.append(f"✅ {label}")
    else:
        replacements_made.append(f"⚠️  NOT FOUND: {label}")

# ===================================================================
# FIX 1: Principles table count (already done via StrReplaceFile)
# ===================================================================

# ===================================================================
# FIX 2: HumanInstruction.agent → target_agent (already done)
# ===================================================================

# ===================================================================
# FIX 3: DurationAdjusted - add slot_id
# ===================================================================
rep(
    '''class DurationAdjusted(Effect):
    """Measured duration within tolerance; OTIO slot updated with authoritative value.

    The Audio Agent computes delta = measured_sec - scripted_sec. If
    |delta| <= max(15% * scripted_sec, 0.25s) the block passes. The OTIO
    Projection updates the slot's source_range to match measured_sec.

    Note: delta_sec and tolerance_sec are computed by projections, not stored
    in the effect. This prevents stale derived values if the tolerance formula
    changes.
    """
    kind: Literal["duration_adjusted"] = "duration_adjusted"
    block_id: str
    scene_num: int
    voice_role: str
    scripted_sec: float''',
    '''class DurationAdjusted(Effect):
    """Measured duration within tolerance; OTIO slot updated with authoritative value.

    The Audio Agent computes delta = measured_sec - scripted_sec. If
    |delta| <= max(15% * scripted_sec, 0.25s) the block passes. The OTIO
    Projection updates the slot's source_range to match measured_sec.

    **V7.1 fix:** Added `slot_id` so OTIOProjection can resolve the clip
    unambiguously. `block_id` alone does not include the track prefix.

    Note: delta_sec and tolerance_sec are computed by projections, not stored
    in the effect. This prevents stale derived values if the tolerance formula
    changes.
    """
    kind: Literal["duration_adjusted"] = "duration_adjusted"
    block_id: str
    slot_id: str = Field(..., description="Canonical slot address, e.g. 'A1:3:2'")
    scene_num: int
    voice_role: str
    scripted_sec: float''',
    "DurationAdjusted +slot_id"
)

# ===================================================================
# FIX 4: MergeIntoOTIO - remove start_time from effect payload
# ===================================================================
rep(
    '''    start_time: float = Field(..., ge=0.0, description="Timeline start in seconds (computed by projection from preceding clips)")''',
    '''    # V7.1 fix: start_time is computed by OTIOProjection from preceding clips,
    # not stored in the effect. Storing it invites stale-data bugs.
    # start_time: float  # REMOVED — projection-derived''',
    "MergeIntoOTIO start_time removed"
)

# ===================================================================
# FIX 5: Parser - remove phantom effect types, unify discriminant to "kind"
# ===================================================================
rep(
    '''class _UpdateScriptEffect(BaseModel):
    effect_type: Literal["UpdateScript"]
    narration_v1: str
    narration_v2: str = ""
    narration_v3: str = ""
    visual_notes: str = ""
    dopamine_hook: str = ""
    pronunciation_hints: str = ""
    duration_sec: int = 30
    scene_num: int = 1

    @field_validator("narration_v1", mode="before")
    @classmethod
    def _v1_must_be_real(cls, v: Any) -> Any:
        if isinstance(v, str) and len(v.strip()) < 5:
            raise ValueError("narration_v1 must contain actual script text, not a placeholder")
        return v''',
    '''class _UpdateScriptEffect(BaseModel):
    """Parser model for UpdateScript extraction.

    **V7.1 fix:** Aligned with event schema (§3.2.1). The parser extracts a
    list of ScriptBlock objects, not flat narration fields. The handler maps
    parser output to `UpdateScript(blocks=[...])` before event store append.
    """
    kind: Literal["update_script"] = "update_script"
    blocks: list[ScriptBlock] = Field(..., min_length=1)

    @field_validator("blocks", mode="before")
    @classmethod
    def _blocks_must_have_text(cls, v: Any) -> Any:
        if isinstance(v, list) and v:
            for b in v:
                if isinstance(b, dict) and len(b.get("text", "").strip()) < 5:
                    raise ValueError("Each block must contain actual script text")
        return v''',
    "_UpdateScriptEffect aligned with event schema"
)

# Replace the entire _EffectUnion and surrounding text
rep(
    '''_EffectUnion = Annotated[
    Union[
        _NoOpEffect, _UpdateScriptEffect, _GenerateNarrationAudioEffect,
        _RenderVideoSegmentEffect, _VMAllocatedEffect, _VMDeallocatedEffect,
        _VMProvisionFailedEffect, _MergeIntoOTIOEffect, _JobStartedEffect,
        _JobCompletedEffect, _JobFailedEffect, _JobQuestionReceivedEffect,
        _JobQuestionAnsweredEffect, _QAPassedEffect, _QAFailedEffect,
        _JobRequeuedEffect,
    ],
    Field(discriminator="effect_type"),
]''',
    '''# V7.1 fix: Removed 6 phantom effect types (_GenerateNarrationAudioEffect,
# _RenderVideoSegmentEffect, _QAPassedEffect, _QAFailedEffect,
# _JobQuestionReceivedEffect, _JobQuestionAnsweredEffect) that do not exist
# in the canonical 32-effect schema. Added all real effect types.
# Discriminant unified to `kind` (was `effect_type`) to match event store.
_EffectUnion = Annotated[
    Union[
        _NoOpEffect,
        _UpdateScriptEffect, _DeleteSceneEffect, _ReorderScenesEffect,
        _QueueJobEffect, _JobStartedEffect, _JobCompletedEffect,
        _JobFailedEffect, _JobRequeuedEffect, _JobApprovedEffect,
        _DurationAdjustedEffect, _ReconciliationFailedEffect,
        _ReconciliationCompleteEffect,
        _VMAllocatedEffect, _VMDeallocatedEffect, _VMProvisionFailedEffect,
        _MergeIntoOTIOEffect, _DeleteFromOTIOEffect,
        _PipelineStartedEffect, _PipelineCompleteEffect,
        _PipelineAbortedEffect, _BudgetSetEffect, _BudgetExceededEffect,
        _VASTGlobalStateObservedEffect,
        _ExecuteRawBashEffect, _HumanInstructionEffect,
        _ClarificationRequestEffect, _AgentLoopDetectedEffect,
        _ProductionFailedEffect, _MeasurementRequestedEffect,
        _AudioMeasuredEffect, _VideoMeasuredEffect,
    ],
    Field(discriminator="kind"),
]''',
    "_EffectUnion - phantom types removed, discriminant=kind"
)

# Fix the explanatory text about discriminator
rep(
    "- `effect_type: Literal[\"<kind>\"]` — discriminant for union dispatch",
    "- `kind: Literal[\"<kind>\"]` — discriminant for union dispatch (matches event store `kind`)",
    "Parser discriminator text fixed"
)

# ===================================================================
# FIX 6: Slot addressing - standardize on short form
# ===================================================================
# We need to update _build_from_script to use short form
rep(
    '''slot_addr = f"{track_name}:{block.scene_num}:{block.block_id}"''',
    '''slot_addr = f"A1:{block.scene_num}:{block.block_id}"  # V7.1: short form, not full track_name''',
    "Slot addressing short form in _build_from_script"
)

# Also fix the _reorder_scenes split comment if needed
# The split on ':' should work with short form A1:3:block_id

# ===================================================================
# FIX 7: derive_situations checks "dirty" → check "scripted"
# ===================================================================
rep(
    '''dirty = [addr for addr, slot in otio.slots.items() if slot.get("status") == "dirty"]''',
    '''dirty = [addr for addr, slot in otio.slots.items() if slot.get("status") == "scripted"]  # V7.1: "scripted" is the dirty state''',
    "derive_situations dirty→scripted"
)

# Also fix the _build_from_script to set "scripted" instead of mentioning "dirty"
# Actually it already says: "new blocks are marked dirty (status='scripted')" which is confusing
rep(
    "new blocks are marked dirty (status=\"scripted\")",
    "new blocks are marked status='scripted' (the 'dirty' state for reconciliation)",
    "Clarify 'scripted' as dirty state"
)

# ===================================================================
# FIX 8: EventStore._seen rebuild on restart
# ===================================================================
rep(
    '''    def __init__(self, log_dir: str) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._stores: dict[str, Path] = {}   # run_id -> file path
        self._seqs: dict[str, int] = {}      # run_id -> last sequence
        self._seen: dict[str, set[str]] = {} # run_id -> seen effect_ids''',
    '''    def __init__(self, log_dir: str) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._stores: dict[str, Path] = {}   # run_id -> file path
        self._seqs: dict[str, int] = {}      # run_id -> last sequence
        self._seen: dict[str, set[str]] = {} # run_id -> seen effect_ids
        # V7.1 fix: Pre-populate _seen for any existing run files so restart
        # does not create duplicates.
        self._rebuild_seen()''',
    "EventStore __init__ +_rebuild_seen"
)

# Add _rebuild_seen method after read_all
rep(
    '''    def replay(self, run_id: str) -> list[EventRecord]:
        """Full replay from sequence 0."""
        return self.read_all(run_id)

---''',
    '''    def replay(self, run_id: str) -> list[EventRecord]:
        """Full replay from sequence 0."""
        return self.read_all(run_id)

    def _rebuild_seen(self) -> None:
        """Scan all existing JSONL files to populate _seen sets."""
        for path in self.log_dir.glob("events_*.jsonl"):
            run_id = path.stem.replace("events_", "")
            seen: set[str] = set()
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = EventRecord.model_validate_json(line)
                        seen.add(str(record.effect.effect_id))
                    except Exception:
                        continue
            self._seen[run_id] = seen
            self._seqs[run_id] = max(
                (r.seq for r in self.read_all(run_id)), default=0
            )

---''',
    "EventStore _rebuild_seen added"
)

# ===================================================================
# FIX 9: Remove ScriptProposed and FinalComposition from startup sequence
# ===================================================================
rep(
    "the parser extracts `ScriptProposed`.",
    "the parser extracts `UpdateScript`.",
    "ScriptProposed→UpdateScript (startup)"
)
rep(
    "The parser extracts `FinalComposition` from Assembly Agent output and the run is complete.",
    "The parser extracts `PipelineComplete` from Assembly Agent output and the run is complete.",
    "FinalComposition→PipelineComplete"
)

# ===================================================================
# FIX 10: Remove reconciliation_partial from permitted effects
# ===================================================================
rep(
    "reconciliation_failed, reconciliation_partial, reconciliation_complete,",
    "reconciliation_failed, reconciliation_complete,",
    "Remove reconciliation_partial from Audio Agent"
)

# ===================================================================
# FIX 11: Fix human_abort → emergency_abort in data flow
# ===================================================================
rep(
    'action="human_abort"',
    'action="emergency_abort"',
    "human_abort→emergency_abort"
)

# ===================================================================
# FIX 12: Fix JobRequeued diagram - remove adjusted_text
# ===================================================================
# Need to find the data flow diagram text. Search for "adjusted_text" in §12.
# Actually the review says §12.2.4 shows JobRequeued carrying adjusted_text.
# Let me search for it.

# I'll handle this in a second pass after writing.

print("\n".join(replacements_made))
print(f"\nTotal: {len([r for r in replacements_made if r.startswith('✅')])} applied, {len([r for r in replacements_made if r.startswith('⚠️')])} not found")

DOC.write_text(text)
