"""Self-operating pipeline state machine — declarative graph edition.

The state graph is a compact, alteration-possible transition matrix.
Each state maps to a list of (guard_name, next_state) pairs evaluated
in declaration order.  The first guard that returns True wins.

Guards are pure functions over projections, registered by name.
Adding a state  = add a key.  Adding a transition  = add a tuple.
Changing priority  = reorder the list.

The graph makes interdependencies between OTIO, Job, and VM state
explicit and central — not buried in class methods.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from statemachine import State, StateChart

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transition graph primitives
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Edge:
    """One directed edge in the state graph.

    The guard names a predicate in the registry.
    The first True edge in a state's edge-list wins.
    """

    guard: str
    to: str
    desc: str = ""


# The compact transition matrix: current_state -> ordered edge list.
# This is the SINGLE source of truth for state topology.
# ---------------------------------------------------------------------------
STATE_GRAPH: dict[str, list[Edge]] = {
    "init": [
        Edge("always", "script", "Pipeline started"),
    ],
    "script": [
        Edge("A1_has_slots_no_gaps", "audio_video",
             "A1_Narration has slots and no gaps → enter production"),
        Edge("default", "script",
             "Still writing / refining script"),
    ],
    "audio_video": [
        # Back-edges first — they are escape hatches and must win over forward
        Edge("script_blame_failures", "script",
             "gap_unexpected or voice_mismatch → back to SCRIPT"),
        # Forward only when everything is clean
        Edge("all_media_produced", "assembly",
             "ReconciliationComplete ∧ no active jobs ∧ all OTIO slots delivered"),
        # Self-loops: two distinct sub-phases of AUDIO_VIDEO
        Edge("reconciliation_active", "audio_video",
             "TTS pending/running OR ReconciliationFailed retries OR not started"),
        Edge("video_pending", "audio_video",
             "ReconciliationComplete ∧ LTX jobs still active"),
        # Catch-all — should not normally be reached
        Edge("default", "audio_video",
             "Waiting for work"),
    ],
    "assembly": [
        Edge("output_valid", "done",
             "MP4 exists ∧ OTIO validates ∧ zero production failures"),
        Edge("default", "assembly",
             "Still assembling or validation failed"),
    ],
    "done": [],
    "aborted": [],
}


# ---------------------------------------------------------------------------
# Predicate registry — pure functions (projections) -> bool
# ---------------------------------------------------------------------------
# Each predicate receives keyword args: otio, jobs, vms, state_proj, cfg.
# They MUST NOT mutate anything.
# ---------------------------------------------------------------------------

Predicate = Callable[..., bool]
PREDICATES: dict[str, Predicate] = {}


def _pred(name: str) -> Callable[[Predicate], Predicate]:
    """Decorator: register a predicate by name."""
    def decorator(fn: Predicate) -> Predicate:
        PREDICATES[name] = fn
        return fn
    return decorator


# ---- helpers used by predicates (pure) ------------------------------------

def _A1_has_slots(otio: dict[str, Any]) -> bool:
    narration = otio.get("tracks", {}).get("A1_Narration", {})
    return any(
        s.get("status") != "gap" for s in narration.get("slots", [])
    )


def _A1_has_gaps(otio: dict[str, Any]) -> bool:
    narration = otio.get("tracks", {}).get("A1_Narration", {})
    return any(
        s.get("status") in ("pending", "in_progress", "failed")
        for s in narration.get("slots", [])
    )


def _all_slots_filled(otio: dict[str, Any]) -> bool:
    for track in otio.get("tracks", {}).values():
        for slot in track.get("slots", []):
            if slot.get("status") == "gap":
                continue
            if slot.get("status") != "delivered":
                return False
    return True


def _has_active_jobs(jobs: dict[str, Any]) -> bool:
    return any(
        j.get("status") in ("pending", "running")
        for j in jobs.values()
    )


def _has_active_jobs_of_type(jobs: dict[str, Any], job_type: str) -> bool:
    return any(
        j.get("status") in ("pending", "running") and j.get("job_type") == job_type
        for j in jobs.values()
    )


def _any_jobs_of_type(jobs: dict[str, Any], job_type: str) -> bool:
    return any(j.get("job_type") == job_type for j in jobs.values())


def _reconciliation_failed_and_pending(jobs: dict[str, Any]) -> bool:
    last_failed = jobs.get("last_reconciliation_failed")
    if last_failed is None:
        return False
    failed_ids = {f.get("block_id") for f in last_failed.get("failures", [])}
    for job in jobs.get("jobs", {}).values():
        if job.get("block_id") in failed_ids:
            if job.get("status") in ("pending", "running"):
                return True
    return False


def _has_script_errors(jobs_dict: dict[str, Any], failures: list[dict]) -> bool:
    for job in jobs_dict.values():
        if job.get("status") == "failed":
            err = job.get("error", "").lower()
            if any(word in err for word in ("script", "narration", "text")):
                return True
    for pf in failures:
        if pf.get("failure_type") in ("gap_unexpected", "voice_mismatch"):
            return True
    return False


def _mp4_exists(cfg: dict[str, Any]) -> bool:
    return os.path.exists(os.path.join(cfg.get("output_dir", "/tmp"), "final_documentary.mp4"))


def _otio_validates(otio: dict[str, Any]) -> bool:
    for name in ("validate_no_overlaps", "validate_track_alignment", "validate_clip_media"):
        validator = getattr(otio, name, None)
        if validator:
            ok, _ = validator()
            if not ok:
                return False
    return True


# ---- predicates -----------------------------------------------------------

@_pred("always")
def _always(**_kwargs: Any) -> bool:
    return True


@_pred("default")
def _default(**_kwargs: Any) -> bool:
    return True


@_pred("A1_has_slots_no_gaps")
def _pred_A1_has_slots_no_gaps(otio: dict[str, Any], **_kw: Any) -> bool:
    return _A1_has_slots(otio) and not _A1_has_gaps(otio)


@_pred("script_blame_failures")
def _pred_script_blame_failures(jobs: dict[str, Any], **_kw: Any) -> bool:
    return _has_script_errors(
        jobs.get("jobs", {}),
        jobs.get("production_failures", []),
    )


@_pred("all_media_produced")
def _pred_all_media_produced(otio: dict[str, Any], jobs: dict[str, Any], **_kw: Any) -> bool:
    return (
        jobs.get("reconciliation_complete", False)
        and not _has_active_jobs(jobs.get("jobs", {}))
        and _all_slots_filled(otio)
    )


@_pred("reconciliation_active")
def _pred_reconciliation_active(jobs: dict[str, Any], **_kw: Any) -> bool:
    if jobs.get("reconciliation_complete", False):
        return False
    return (
        _has_active_jobs_of_type(jobs.get("jobs", {}), "tts")
        or _reconciliation_failed_and_pending(jobs)
        or not _any_jobs_of_type(jobs.get("jobs", {}), "tts")
    )


@_pred("video_pending")
def _pred_video_pending(jobs: dict[str, Any], **_kw: Any) -> bool:
    return (
        jobs.get("reconciliation_complete", False)
        and _has_active_jobs_of_type(jobs.get("jobs", {}), "ltx")
    )


@_pred("output_valid")
def _pred_output_valid(otio: dict[str, Any], jobs: dict[str, Any], cfg: dict[str, Any], **_kw: Any) -> bool:
    if not _mp4_exists(cfg):
        return False
    if jobs.get("production_failures", []):
        return False
    return _otio_validates(otio)


# ---------------------------------------------------------------------------
# StateChart executor — interprets the graph
# ---------------------------------------------------------------------------

class PipelineStateMachine(StateChart):
    """Tick-driven state machine that evaluates the declarative STATE_GRAPH."""

    # python-statemachine still needs concrete State objects
    init = State(initial=True)
    script = State()
    audio_video = State()
    assembly = State()
    done = State(final=True)
    aborted = State(final=True)

    # We still wire a single tick event; the guard dispatches via the graph.
    # Escapes to aborted are declared last so operational transitions win.
    tick = (
        init.to(script)
        | script.to.itself()
        | script.to(audio_video)
        | audio_video.to.itself()
        | audio_video.to(script)
        | audio_video.to(assembly)
        | assembly.to.itself()
        | assembly.to(done)
        # Escape hatch
        | init.to(aborted, cond="guard_escape")
        | script.to(aborted, cond="guard_escape")
        | audio_video.to(aborted, cond="guard_escape")
        | assembly.to(aborted, cond="guard_escape")
    )

    def __init__(
        self,
        *,
        otio_projection: Any,
        job_projection: Any,
        vm_projection: Any,
        state_projection: Any,
        output_dir: str = "/tmp/documentary-pipeline",
    ) -> None:
        super().__init__()
        self.otio = otio_projection
        self.jobs = job_projection
        self.vms = vm_projection
        self.state_proj = state_projection
        self._cfg = {"output_dir": output_dir}

        # Loop detection
        self._last_sig: str = ""
        self._repeat: int = 0

    # ------------------------------------------------------------------
    # Graph evaluation — the heart of the machine
    # ------------------------------------------------------------------

    def _eval_graph(self) -> str | None:
        """Evaluate STATE_GRAPH for the current state.  Return next state or None."""
        current = self.current_state_name
        edges = STATE_GRAPH.get(current, [])
        if not edges:
            return None  # terminal

        # Build kwargs once per tick
        kw = {
            "otio": self.otio,
            "jobs": self.jobs,
            "vms": self.vms,
            "state_proj": self.state_proj,
            "cfg": self._cfg,
        }

        for edge in edges:
            pred = PREDICATES.get(edge.guard)
            if pred is None:
                logger.warning("Unknown guard %r on edge %s -> %s", edge.guard, current, edge.to)
                continue
            try:
                if pred(**kw):
                    logger.debug("GRAPH: %s --[%s:%s]--> %s", current, edge.guard, edge.desc, edge.to)
                    return edge.to
            except Exception:
                logger.exception("Guard %r raised; skipping edge to %s", edge.guard, edge.to)

        return None  # no edge matched (should not happen if default is present)

    # ------------------------------------------------------------------
    # python-statemachine integration
    # ------------------------------------------------------------------

    def on_transition(self, event: str, source: State, target: State) -> None:
        """Called by python-statemachine on EVERY tick transition attempt.

        We override the target if the graph says something different.
        This lets the declarative graph override the static transition table.
        """
        graph_target = self._eval_graph()
        if graph_target and graph_target != target.id:
            # Graph disagrees with static wiring — this shouldn't happen
            # if the static table is kept permissive (many self-loops).
            logger.debug("Graph override: %s -> %s (was %s)", source.id, graph_target, target.id)

    # Custom guards referenced by the static tick table.
    # Each just asks the graph what the next state should be.

    def _guard_for_target(self, target_name: str, **_kw: Any) -> bool:
        """Return True iff the graph says we should go to target_name."""
        return self._eval_graph() == target_name

    def _guard_init_to_script(self, **_kw: Any) -> bool:
        return self._guard_for_target("script")

    def _guard_script_to_audio_video(self, **_kw: Any) -> bool:
        return self._guard_for_target("audio_video")

    def _guard_script_to_script(self, **_kw: Any) -> bool:
        return self._guard_for_target("script")

    def _guard_audio_video_to_assembly(self, **_kw: Any) -> bool:
        return self._guard_for_target("assembly")

    def _guard_audio_video_to_script(self, **_kw: Any) -> bool:
        return self._guard_for_target("script")

    def _guard_audio_video_to_self(self, **_kw: Any) -> bool:
        return self._guard_for_target("audio_video")

    def _guard_assembly_to_done(self, **_kw: Any) -> bool:
        return self._guard_for_target("done")

    def _guard_assembly_to_self(self, **_kw: Any) -> bool:
        return self._guard_for_target("assembly")

    def guard_escape(self, **_kw: Any) -> bool:
        """Escape to ABORTED on budget exceeded or loop detected."""
        # Budget check
        spent = getattr(self.jobs, "spent_usd", 0.0)
        limit = getattr(self._cfg, "max_budget_usd", float("inf"))
        if spent > limit:
            logger.warning("BUDGET EXCEEDED: $%.2f / $%.2f — aborting", spent, limit)
            return True
        # Loop detection via state projection
        recent = getattr(self.state_proj, "get_recent_effects", lambda n: [])()
        if any(e.get("kind") == "agent_loop_detected" for e in recent):
            return True
        return False

    # Bind guard names for python-statemachine cond="..."
    guard_init_to_script = _guard_init_to_script
    guard_script_to_audio_video = _guard_script_to_audio_video
    guard_script_to_script = _guard_script_to_script
    guard_audio_video_to_assembly = _guard_audio_video_to_assembly
    guard_audio_video_to_script = _guard_audio_video_to_script
    guard_audio_video_to_self = _guard_audio_video_to_self
    guard_assembly_to_done = _guard_assembly_to_done
    guard_assembly_to_self = _guard_assembly_to_self

    # ------------------------------------------------------------------
    # State entry actions
    # ------------------------------------------------------------------

    def on_enter_script(self) -> None:
        logger.info("STATE: SCRIPT")

    def on_enter_audio_video(self) -> None:
        logger.info("STATE: AUDIO_VIDEO")

    def on_enter_assembly(self) -> None:
        logger.info("STATE: ASSEMBLY")

    def on_enter_done(self) -> None:
        logger.info("STATE: DONE — pipeline complete")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def current_state_name(self) -> str:
        for s in self.configuration:
            return s.id
        return "unknown"

    def detect_loop(self, effect_kind: str, agent: str) -> bool:
        sig = f"{agent}:{effect_kind}"
        if sig == self._last_sig:
            self._repeat += 1
        else:
            self._last_sig = sig
            self._repeat = 1
        return self._repeat >= 3


# ---------------------------------------------------------------------------
# Watcher loop
# ---------------------------------------------------------------------------

async def run_state_machine_watcher(
    machine: PipelineStateMachine,
    projections: list[Any],
    tick_interval: float = 1.0,
) -> None:
    logger.info("Watcher loop starting (interval=%ss)", tick_interval)
    while True:
        try:
            for proj in projections:
                if hasattr(proj, "tick"):
                    proj.tick()
            machine.tick()
        except Exception:
            logger.exception("Watcher loop error")
        await asyncio.sleep(tick_interval)


# ---------------------------------------------------------------------------
# State instructions for prompt injection
# ---------------------------------------------------------------------------

STATE_INSTRUCTIONS: dict[str, str] = {
    "init": (
        "You are initializing the documentary. "
        "Write a narration script with scenes, speakers, and timing. "
        "Each scene has one or more voice lines (phrases). "
        "Use bash to write files. Report with Kind: update_script."
    ),
    "script": (
        "You are refining the narration script. "
        "Focus on pacing, speaker consistency, and duration targets. "
        "Each voice line becomes a slot on the A1_Narration track. "
        "Use bash to read and write. Report with Kind: update_script."
    ),
    "audio_video": (
        "You are producing media. This state has TWO phases:\n"
        "PHASE 1 — Audio Reconciliation (authoritative OTIO is born here):\n"
        "  Generate TTS audio (Kind: queue_job + audio_generated).\n"
        "  Measure with WhisperX (Kind: audio_measured).\n"
        "  Compare measured vs scripted duration.\n"
        "  If within tolerance (±15%% or ±0.25s): adjust OTIO (Kind: duration_adjusted).\n"
        "  If outside tolerance: requeue with modified text (Kind: job_requeued).\n"
        "  When ALL blocks pass: emit Kind: reconciliation_complete.\n"
        "PHASE 2 — Video Production (only after reconciliation_complete):\n"
        "  Generate LTX video using measured durations as LAW.\n"
        "  Judge output. Approve (Kind: job_approved) or reject (Kind: job_requeued).\n"
        "  Merge approved clips (Kind: merge_into_otio)."
    ),
    "assembly": (
        "You are assembling the final film. Use ffmpeg via bash. "
        "Combine all clips from the OTIO timeline into final_documentary.mp4. "
        "Verify the output duration matches the timeline."
    ),
    "done": "Pipeline complete. No further action needed.",
}


def get_state_instruction(state: str) -> str:
    return STATE_INSTRUCTIONS.get(state, "Unknown state. Proceed with caution.")
