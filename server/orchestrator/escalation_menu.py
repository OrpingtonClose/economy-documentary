"""Canonical escalation action menu for the documentary pipeline.

Closes GitHub issues #61, #73, #76, #77, #102, #103.

This module defines the **formal, typed action menu** that the production
supervisor chooses from when an escalation happens.  It replaces the ad-hoc
round-robin fall-through that the PAG run revealed -- during that run the
supervisor made zero LLM calls and required full human intervention to
handle 5 extension-clip decisions and 9 regenerations.

Core types::

    EscalationAction   -- dataclass + Literal enum of 8 canonical actions
    EscalationContext  -- diagnostic snapshot passed to supervisor_escalate

Every escalation path in the pipeline must resolve to one of the 8 typed
actions.  The ``supervisor_escalate(context)`` helper in
``agents.production_supervisor`` consults Gemini (via google-genai with
structured output) to pick the right action and validates the response
against this schema.

The module is deliberately dependency-free of ADK / litellm / google-genai
so it can be imported by tests and telemetry code without pulling in the
full model stack.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional, get_args

# ---------------------------------------------------------------------------
# Canonical action names (Literal enum) + signatures
# ---------------------------------------------------------------------------

ActionName = Literal[
    # Creative / timeline actions (PR-0 menu)
    "regenerate_clip",
    "generate_extension_clip",
    "speed_up_narration",
    "trim_narration",
    "freeze_frame_fill",
    "replace_with_brand_card",
    "rewrite_scene",
    "abort_run",
    # Ops / deployment actions (PR-2 — deployment planner participation)
    "recycle_worker",
    "provision_extra_worker",
    "wait_for_worker_recovery",
    "freeze_batch_and_replan",
]

ACTION_NAMES: tuple[str, ...] = tuple(get_args(ActionName))

# Canonical subsets so callers / tests can reason about the two families
# without re-listing them.
CREATIVE_ACTION_NAMES: tuple[str, ...] = (
    "regenerate_clip",
    "generate_extension_clip",
    "speed_up_narration",
    "trim_narration",
    "freeze_frame_fill",
    "replace_with_brand_card",
    "rewrite_scene",
    "abort_run",
)
OPS_ACTION_NAMES: tuple[str, ...] = (
    "recycle_worker",
    "provision_extra_worker",
    "wait_for_worker_recovery",
    "freeze_batch_and_replan",
)

# Which escalation level each action belongs to (per spec).
ACTION_LEVELS: dict[str, int] = {
    "regenerate_clip": 1,
    "generate_extension_clip": 1,
    "speed_up_narration": 1,
    "trim_narration": 2,
    "freeze_frame_fill": 2,
    "replace_with_brand_card": 2,
    "rewrite_scene": 3,
    "abort_run": 3,
    # Ops actions
    "wait_for_worker_recovery": 1,    # cheapest — just wait
    "recycle_worker": 2,              # destroy + reprovision a single worker
    "provision_extra_worker": 2,      # add capacity
    "freeze_batch_and_replan": 3,     # structural — halt in-flight work + replan
}

# Required parameter names and expected types per action.
ACTION_SIGNATURES: dict[str, dict[str, type]] = {
    "regenerate_clip": {"clip_id": str, "prompt_delta": str, "seed_delta": int},
    "generate_extension_clip": {"scene_id": str, "duration_needed": float},
    "speed_up_narration": {"scene_id": str, "speed_factor": float},
    "trim_narration": {"scene_id": str, "max_cut_sec": float},
    "freeze_frame_fill": {"scene_id": str, "duration_needed": float},
    "replace_with_brand_card": {"scene_id": str},
    "rewrite_scene": {"scene_id": str, "guidance": str},
    "abort_run": {"reason": str},
    # Ops actions
    "recycle_worker": {"worker_url": str, "reason": str},
    "provision_extra_worker": {"role": str, "count": int},
    "wait_for_worker_recovery": {"worker_url": str, "timeout_sec": float},
    "freeze_batch_and_replan": {"reason": str},
}

# Hard bounds enforced in __post_init__.
MAX_SPEED_FACTOR: float = 1.15
MAX_PROVISION_COUNT: int = 4          # don't let the agent spin up a farm
MAX_WAIT_TIMEOUT_SEC: float = 1800.0  # 30 minutes — hard cap on "just wait"
OPS_VALID_ROLES: tuple[str, ...] = ("tts", "video", "whisperx")


class EscalationActionError(ValueError):
    """Raised when an EscalationAction fails signature/bounds validation."""


@dataclass
class EscalationAction:
    """A typed, canonical recovery action.

    Every escalation path in the pipeline must produce one of these.
    The ``action`` field is a Literal discriminator; each variant requires
    a specific set of parameters (see ``ACTION_SIGNATURES``) validated in
    ``__post_init__``.

    This is intentionally implemented as a plain dataclass + Literal
    (rather than a Pydantic model) so it can be imported anywhere without
    pulling in heavy dependencies.
    """

    action: ActionName

    # Target IDs (one of clip_id / scene_id is used depending on action).
    clip_id: Optional[str] = None
    scene_id: Optional[str] = None

    # Per-action parameters (creative menu).
    prompt_delta: Optional[str] = None
    seed_delta: Optional[int] = None
    duration_needed: Optional[float] = None
    speed_factor: Optional[float] = None
    max_cut_sec: Optional[float] = None
    guidance: Optional[str] = None
    reason: Optional[str] = None

    # Per-action parameters (ops menu).
    worker_url: Optional[str] = None
    role: Optional[str] = None
    count: Optional[int] = None
    timeout_sec: Optional[float] = None

    # Audit metadata populated by ``supervisor_escalate``.
    llm_model: str = ""
    llm_reasoning: str = ""

    def __post_init__(self) -> None:
        if self.action not in ACTION_LEVELS:
            raise EscalationActionError(
                f"Unknown action '{self.action}'. "
                f"Valid actions: {ACTION_NAMES}"
            )

        sig = ACTION_SIGNATURES[self.action]
        for field_name, expected_type in sig.items():
            value = getattr(self, field_name)
            if value is None:
                raise EscalationActionError(
                    f"Action '{self.action}' requires field '{field_name}' "
                    f"(expected {expected_type.__name__})"
                )
            # Reject empty strings for required str fields -- otherwise
            # to_dict() would silently drop them and break the round-trip
            # guarantee that test_action_menu_parses_all_signatures relies on.
            if expected_type is str and isinstance(value, str) and value == "":
                raise EscalationActionError(
                    f"Action '{self.action}' field '{field_name}' must be "
                    f"a non-empty string"
                )
            # Allow int → float coercion (common from JSON).
            if expected_type is float and isinstance(value, int) and not isinstance(value, bool):
                setattr(self, field_name, float(value))
                continue
            # Allow str-ish integers for seed_delta.
            if expected_type is int and isinstance(value, float) and value.is_integer():
                setattr(self, field_name, int(value))
                continue
            if not isinstance(value, expected_type):
                raise EscalationActionError(
                    f"Action '{self.action}' field '{field_name}' must be "
                    f"{expected_type.__name__}, got {type(value).__name__}"
                )

        # Action-specific bounds.
        if self.action == "speed_up_narration":
            sf = self.speed_factor
            assert sf is not None  # for type checkers
            if not (1.0 < sf <= MAX_SPEED_FACTOR):
                raise EscalationActionError(
                    f"speed_up_narration: speed_factor must be in "
                    f"(1.0, {MAX_SPEED_FACTOR}], got {sf}"
                )
        if self.action == "generate_extension_clip":
            assert self.duration_needed is not None
            if self.duration_needed <= 0:
                raise EscalationActionError(
                    "generate_extension_clip: duration_needed must be > 0"
                )
        if self.action == "freeze_frame_fill":
            assert self.duration_needed is not None
            if self.duration_needed <= 0:
                raise EscalationActionError(
                    "freeze_frame_fill: duration_needed must be > 0"
                )
        if self.action == "trim_narration":
            assert self.max_cut_sec is not None
            if self.max_cut_sec <= 0:
                raise EscalationActionError(
                    "trim_narration: max_cut_sec must be > 0"
                )
        if self.action == "regenerate_clip":
            if self.seed_delta == 0:
                raise EscalationActionError(
                    "regenerate_clip: seed_delta must be non-zero"
                )
        if self.action == "provision_extra_worker":
            assert self.count is not None
            if self.count <= 0 or self.count > MAX_PROVISION_COUNT:
                raise EscalationActionError(
                    f"provision_extra_worker: count must be in "
                    f"[1, {MAX_PROVISION_COUNT}], got {self.count}"
                )
            assert self.role is not None
            if self.role not in OPS_VALID_ROLES:
                raise EscalationActionError(
                    f"provision_extra_worker: role must be one of "
                    f"{OPS_VALID_ROLES}, got {self.role!r}"
                )
        if self.action == "wait_for_worker_recovery":
            assert self.timeout_sec is not None
            if self.timeout_sec <= 0 or self.timeout_sec > MAX_WAIT_TIMEOUT_SEC:
                raise EscalationActionError(
                    f"wait_for_worker_recovery: timeout_sec must be in "
                    f"(0, {MAX_WAIT_TIMEOUT_SEC}], got {self.timeout_sec}"
                )

    @property
    def level(self) -> int:
        """Escalation level (1, 2, or 3)."""
        return ACTION_LEVELS[self.action]

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a compact dict (drops None / empty values)."""
        return {
            k: v
            for k, v in asdict(self).items()
            if v is not None and v != ""
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EscalationAction":
        """Build an action from a JSON-ish dict.

        Only the fields relevant to ``data["action"]`` are required;
        stray keys are ignored.  Validation happens in ``__post_init__``.
        """
        if not isinstance(data, dict):
            raise EscalationActionError(
                f"Expected dict, got {type(data).__name__}"
            )
        action = data.get("action")
        if action is None:
            raise EscalationActionError("Missing 'action' field")
        sig = ACTION_SIGNATURES.get(action)
        if sig is None:
            raise EscalationActionError(f"Unknown action '{action}'")

        kwargs: dict[str, Any] = {"action": action}
        for field_name in sig:
            if field_name not in data:
                raise EscalationActionError(
                    f"Action '{action}' missing required field '{field_name}'"
                )
            kwargs[field_name] = data[field_name]

        # Optional audit fields.
        if "llm_model" in data:
            kwargs["llm_model"] = data["llm_model"]
        if "llm_reasoning" in data:
            kwargs["llm_reasoning"] = data["llm_reasoning"]

        return cls(**kwargs)


# ---------------------------------------------------------------------------
# EscalationContext
# ---------------------------------------------------------------------------

@dataclass
class EscalationContext:
    """Diagnostic context passed to supervisor_escalate.

    Everything the supervisor needs to pick the right canonical action.
    """

    failing_artifact: str
    """Short identifier of what failed (e.g. ``"clip s003_p002"``)."""

    artifact_descriptor: dict[str, Any] = field(default_factory=dict)
    """Domain-specific descriptor (clip prompt, scene metadata, QA result, etc.)."""

    timeline_state_snapshot: dict[str, Any] = field(default_factory=dict)
    """Snapshot of timeline durations / gaps / per-scene status."""

    user_original_prompt: str = ""
    """The user's original pipeline prompt (for narrative-preservation tradeoffs)."""

    budget_remaining: float = 0.0
    """Remaining compute budget in dollars (0 = unbounded)."""

    escalation_history: list[dict[str, Any]] = field(default_factory=list)
    """Prior escalations in this run (each dict: action, outcome, timestamp)."""

    high_cost: bool = False
    """Hint: use Gemini Pro instead of Flash when True.

    Set for expensive targets (e.g. 10-second LTX clips, full scene rewrites)
    where the ~10x price premium is justified by avoiding a wrong action.
    """


# ---------------------------------------------------------------------------
# Menu prompt (rendered into the supervisor LLM prompt)
# ---------------------------------------------------------------------------

ACTION_MENU_DESCRIPTION = """\
CANONICAL ESCALATION ACTIONS -- you MUST choose EXACTLY ONE.

Level 1 (cheap, targeted fixes -- prefer these):
  - regenerate_clip(clip_id, prompt_delta, seed_delta)
      Regenerate a single clip. ``prompt_delta`` is natural-language
      corrective guidance (e.g. "emphasise kitchen setting, avoid
      outdoor landscapes"). ``seed_delta`` is a non-zero integer to
      perturb the seed (e.g. +7, -13).
  - generate_extension_clip(scene_id, duration_needed)
      Create a short clip to fill remaining narration time in a scene
      (typical use: 0.5-3.0 seconds).
  - speed_up_narration(scene_id, speed_factor)
      Time-stretch narration audio. ``speed_factor`` MUST be in
      (1.0, 1.15]. Use ONLY when <=15% compression is enough.

Level 2 (surgical edits, acceptable quality cost):
  - trim_narration(scene_id, max_cut_sec)
      Cut up to ``max_cut_sec`` seconds from the end of narration.
  - freeze_frame_fill(scene_id, duration_needed)
      Hold the last frame for ``duration_needed`` seconds.
  - replace_with_brand_card(scene_id)
      Replace scene with a static brand/title card. Heavy narrative cost.

Level 3 (structural / terminal -- last resort):
  - rewrite_scene(scene_id, guidance)
      Ask the scenario director to regenerate the scene's narration +
      visual brief. Expensive -- use only when cheaper actions have
      failed OR the failure is clearly structural.
  - abort_run(reason)
      Stop the pipeline entirely. Only when no safe recovery is possible.

Ops / deployment actions (use when the failure root-cause is infra --
worker VRAM, stage timeouts, cost overruns, etc.  Consult the
read-tools ``read_worker_health`` / ``read_stage_timing`` /
``read_vast_cost_snapshot`` before picking these.):
  - wait_for_worker_recovery(worker_url, timeout_sec)  [L1]
      Pause the escalating caller and wait for an in-flight worker's
      self-healing retry to land. ``timeout_sec`` must be in
      (0, 1800]. Cheapest ops action; prefer this when a worker just
      went briefly unresponsive.
  - recycle_worker(worker_url, reason)  [L2]
      Destroy + reprovision a single degraded worker.  Use when the
      worker is consistently failing (repeated consecutive QA fails,
      VRAM pressure, OOM) and waiting will not help.
  - provision_extra_worker(role, count)  [L2]
      Add capacity for a stage (``role`` in {"tts", "video", "whisperx"},
      ``count`` in [1, 4]).  Use when stage timing says the fleet is
      saturated rather than broken.
  - freeze_batch_and_replan(reason)  [L3]
      Halt in-flight video/audio/production work and ask the orchestrator
      to replan remaining scenes.  Structural ops intervention.  Use
      only when no per-worker action can rescue the batch.

Decision rule: pick the cheapest action that resolves the failure while
preserving the narrative. Prefer L1 over L2 over L3. If a ``speed_factor``
of 1.14 would reach the target duration, use ``speed_up_narration`` rather
than ``trim_narration``. Do NOT abort unless no L1/L2 action is viable --
round-robin fall-through with an abort is exactly the #102 regression.
"""


# JSON schema for Gemini structured output.  Kept flat (Gemini's
# structured-output API does not support oneOf); per-action signature
# validation happens in ``EscalationAction.__post_init__`` after parsing.
ESCALATION_ACTION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTION_NAMES)},
        # Creative menu fields.
        "clip_id": {"type": "string"},
        "scene_id": {"type": "string"},
        "prompt_delta": {"type": "string"},
        "seed_delta": {"type": "integer"},
        "duration_needed": {"type": "number"},
        "speed_factor": {"type": "number"},
        "max_cut_sec": {"type": "number"},
        "guidance": {"type": "string"},
        "reason": {"type": "string"},
        # Ops menu fields.
        "worker_url": {"type": "string"},
        "role": {"type": "string", "enum": list(OPS_VALID_ROLES)},
        "count": {"type": "integer"},
        "timeout_sec": {"type": "number"},
        # Audit.
        "llm_reasoning": {"type": "string"},
    },
    "required": ["action"],
}


# ---------------------------------------------------------------------------
# Hard invariant (per #102 acceptance criterion)
# ---------------------------------------------------------------------------

class EscalationInvariantViolation(AssertionError):
    """Raised when the escalation invariant fails.

    Invariant: any run that had at least one escalation MUST have made at
    least one supervisor LLM call.  A violation means we fell back to
    round-robin with zero reasoning -- the exact regression that #61,
    #73, #102 close.
    """


def assert_escalation_invariant(
    escalations_per_run: int,
    llm_calls_per_run: int,
) -> None:
    """Hard end-of-run invariant check.

    Args:
        escalations_per_run: Number of escalations triggered this run.
        llm_calls_per_run: Number of supervisor LLM calls this run.

    Raises:
        EscalationInvariantViolation: if ``escalations_per_run > 0`` but
            ``llm_calls_per_run <= 0``.
    """
    if escalations_per_run > 0 and llm_calls_per_run <= 0:
        raise EscalationInvariantViolation(
            f"Escalation invariant violated: escalations_per_run="
            f"{escalations_per_run} but llm_calls_per_run="
            f"{llm_calls_per_run}. Supervisor must make at least one "
            f"LLM call per escalation. (#102 acceptance criterion)"
        )


__all__ = [
    "ActionName",
    "ACTION_NAMES",
    "CREATIVE_ACTION_NAMES",
    "OPS_ACTION_NAMES",
    "ACTION_LEVELS",
    "ACTION_SIGNATURES",
    "ACTION_MENU_DESCRIPTION",
    "ESCALATION_ACTION_JSON_SCHEMA",
    "MAX_SPEED_FACTOR",
    "MAX_PROVISION_COUNT",
    "MAX_WAIT_TIMEOUT_SEC",
    "OPS_VALID_ROLES",
    "EscalationAction",
    "EscalationActionError",
    "EscalationContext",
    "EscalationInvariantViolation",
    "assert_escalation_invariant",
]
