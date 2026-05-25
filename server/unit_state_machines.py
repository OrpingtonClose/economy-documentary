"""Per-unit state machine definitions.

Each agent has its own state machine. The instructor enforces it.
No code outside this file knows about state machines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from effects import Effect


@dataclass
class StateMachine:
    """A per-unit state machine."""

    unit_id: str
    states: set[str] = field(default_factory=set)
    initial_state: str = "IDLE"
    transitions: dict[tuple[str, str], str] = field(default_factory=dict)
    # (current_state, effect_type) -> next_state
    valid_effects: dict[str, set[str]] = field(default_factory=dict)
    # state -> set of valid effect types

    def get_valid_effects(self, state: str) -> set[str]:
        return self.valid_effects.get(state, set())

    def transition(self, current_state: str, effect_type: str) -> str:
        key = (current_state, effect_type)
        return self.transitions.get(key, current_state)


# ---------------------------------------------------------------------------
# Scenario Agent State Machine
# ---------------------------------------------------------------------------

SCENARIO_MACHINE = StateMachine(
    unit_id="scenario",
    states={"IDLE", "HAS_SCENARIO"},
    initial_state="IDLE",
    transitions={
        ("IDLE", "UpdateScript"): "HAS_SCENARIO",
        ("HAS_SCENARIO", "UpdateScript"): "HAS_SCENARIO",
    },
    valid_effects={
        "IDLE": {"UpdateScript", "NoOp"},
        "HAS_SCENARIO": {"UpdateScript", "NoOp"},
    },
)

# ---------------------------------------------------------------------------
# Audio Agent State Machine
# ---------------------------------------------------------------------------

AUDIO_MACHINE = StateMachine(
    unit_id="audio",
    states={"IDLE", "PENDING_AUDIO", "HAS_AUDIO"},
    initial_state="IDLE",
    transitions={
        ("IDLE", "GenerateNarrationAudio"): "PENDING_AUDIO",
        ("PENDING_AUDIO", "GenerateNarrationAudio"): "PENDING_AUDIO",
        ("PENDING_AUDIO", "NoOp"): "PENDING_AUDIO",
    },
    valid_effects={
        "IDLE": {"GenerateNarrationAudio", "NoOp"},
        "PENDING_AUDIO": {"GenerateNarrationAudio", "NoOp"},
        "HAS_AUDIO": {"NoOp"},
    },
)

# ---------------------------------------------------------------------------
# Video Agent State Machine
# ---------------------------------------------------------------------------

VIDEO_MACHINE = StateMachine(
    unit_id="video",
    states={"IDLE", "PENDING_VIDEO", "HAS_VIDEO"},
    initial_state="IDLE",
    transitions={
        ("IDLE", "RenderVideoSegment"): "PENDING_VIDEO",
        ("PENDING_VIDEO", "RenderVideoSegment"): "PENDING_VIDEO",
        ("PENDING_VIDEO", "NoOp"): "PENDING_VIDEO",
    },
    valid_effects={
        "IDLE": {"RenderVideoSegment", "NoOp"},
        "PENDING_VIDEO": {"RenderVideoSegment", "NoOp"},
        "HAS_VIDEO": {"NoOp"},
    },
)

# ---------------------------------------------------------------------------
# Assembly Agent State Machine
# ---------------------------------------------------------------------------

ASSEMBLY_MACHINE = StateMachine(
    unit_id="assembly",
    states={"IDLE", "MERGING", "COMPLETE"},
    initial_state="IDLE",
    transitions={
        ("IDLE", "MergeIntoOTIO"): "MERGING",
        ("MERGING", "ExecuteRawBash"): "COMPLETE",
        ("MERGING", "MergeIntoOTIO"): "MERGING",
    },
    valid_effects={
        "IDLE": {"MergeIntoOTIO", "NoOp"},
        "MERGING": {"MergeIntoOTIO", "ExecuteRawBash", "NoOp"},
        "COMPLETE": {"NoOp"},
    },
)

# ---------------------------------------------------------------------------
# Provisioner Agent State Machine
# ---------------------------------------------------------------------------

PROVISIONER_MACHINE = StateMachine(
    unit_id="provisioner",
    states={"IDLE", "PROVISIONING", "EXECUTING"},
    initial_state="IDLE",
    transitions={
        ("IDLE", "ExecuteRawBash"): "PROVISIONING",
        ("PROVISIONING", "ExecuteRawBash"): "EXECUTING",
        ("EXECUTING", "ExecuteRawBash"): "IDLE",
        ("IDLE", "NoOp"): "IDLE",
    },
    valid_effects={
        "IDLE": {"ExecuteRawBash", "NoOp"},
        "PROVISIONING": {"ExecuteRawBash", "NoOp"},
        "EXECUTING": {"ExecuteRawBash", "NoOp"},
    },
)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

MACHINES: dict[str, StateMachine] = {
    "scenario": SCENARIO_MACHINE,
    "audio": AUDIO_MACHINE,
    "video": VIDEO_MACHINE,
    "assembly": ASSEMBLY_MACHINE,
    "provisioner": PROVISIONER_MACHINE,
}


def get_machine(unit_id: str) -> StateMachine:
    return MACHINES.get(unit_id, StateMachine(unit_id=unit_id))
