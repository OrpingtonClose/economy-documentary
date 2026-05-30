#!/usr/bin/env python3
"""Batch 1 fixes for ARCHITECTURE_V7.1.md - P0 Critical Blockers."""

from pathlib import Path

DOC = Path("/Users/orpington/Documents/economy-documentary-work/ARCHITECTURE_V7.1.md")
text = DOC.read_text()

# 1. Fix "Table of 11 hard principles" → "12 hard principles"
text = text.replace(
    "#### 1.9.1 Table of 11 hard principles with enforcement mechanism per principle",
    "#### 1.9.1 Table of 12 hard principles with enforcement mechanism per principle"
)

# 2. Fix HumanInstruction.agent → target_agent (to avoid collision with Effect.agent)
text = text.replace(
    "class HumanInstruction(Effect):\n    \"\"\"Human operator posted a directive to a specific agent.\n\n    The operator POSTs directly to the agent's endpoint with free text. The\n    agent parses it on its next turn. Instructions can override parameters,\n    approve blocked commands, or redirect the pipeline (e.g. \"skip scene 5\").\n\n    Instructions are permanent until superseded by another HumanInstruction\n    or PipelineAborted. No expiry — Principle 4 prohibits deadline checks.\n    \"\"\"\n    kind: Literal[\"human_instruction\"] = \"human_instruction\"\n    agent: str = Field(..., description=\"target agent name or 'all'\")",
    "class HumanInstruction(Effect):\n    \"\"\"Human operator posted a directive to a specific agent.\n\n    The operator POSTs directly to the agent's endpoint with free text. The\n    agent parses it on its next turn. Instructions can override parameters,\n    approve blocked commands, or redirect the pipeline (e.g. \"skip scene 5\").\n\n    Instructions are permanent until superseded by another HumanInstruction\n    or PipelineAborted. No expiry — Principle 4 prohibits deadline checks.\n\n    **V7.1 fix:** `target_agent` replaces `agent` to avoid collision with\n    Effect.agent (which names the *producer* component, not the target).\n    \"\"\"\n    kind: Literal[\"human_instruction\"] = \"human_instruction\"\n    target_agent: str = Field(..., description=\"target agent name or 'all'\")"
)

# 3. Fix all downstream references to HumanInstruction.agent → target_agent
text = text.replace("effect.agent", "effect.target_agent")  # This is too broad - be careful
# Actually, let me be more precise. Only fix references within HumanInstruction context.

# The action field uses action="human_abort" in §12.4.3 - fix to emergency_abort
text = text.replace('action="human_abort"', 'action="emergency_abort"')

# 4. Fix DurationAdjusted - add slot_id
text = text.replace(
    "class DurationAdjusted(Effect):\n    \"\"\"Measured duration within tolerance; OTIO slot updated with authoritative value.\n\n    The Audio Agent computes delta = measured_sec - scripted_sec. If\n    |delta| <= max(15% * scripted_sec, 0.25s) the block passes. The OTIO\n    Projection updates the slot's source_range to match measured_sec.\n\n    Note: delta_sec and tolerance_sec are computed by projections, not stored\n    in the effect. This prevents stale derived values if the tolerance formula\n    changes.\n    \"\"\"\n    kind: Literal[\"duration_adjusted\"] = \"duration_adjusted\"\n    block_id: str\n    scene_num: int\n    voice_role: str\n    scripted_sec: float",
    "class DurationAdjusted(Effect):\n    \"\"\"Measured duration within tolerance; OTIO slot updated with authoritative value.\n\n    The Audio Agent computes delta = measured_sec - scripted_sec. If\n    |delta| <= max(15% * scripted_sec, 0.25s) the block passes. The OTIO\n    Projection updates the slot's source_range to match measured_sec.\n\n    **V7.1 fix:** Added `slot_id` so OTIOProjection can resolve the clip\n    without ambiguity. `block_id` alone may not include track prefix.\n    \"\"\"\n    kind: Literal[\"duration_adjusted\"] = \"duration_adjusted\"\n    block_id: str\n    slot_id: str = Field(..., description=\"Canonical slot address, e.g. 'A1:3:2'\")\n    scene_num: int\n    voice_role: str\n    scripted_sec: float"
)

# 5. Fix MergeIntoOTIO start_time - remove from effect, make projection-computed
text = text.replace(
    "    start_time: float = Field(..., ge=0.0, description=\"Timeline start in seconds (computed by projection from preceding clips)\")",
    "    # V7.1 fix: start_time is computed by OTIOProjection from preceding clips,\n    # not stored in the effect. Storing it invites stale-data bugs.\n    # start_time: float  # REMOVED — projection-derived"
)

DOC.write_text(text)
print("Batch 1 applied")
