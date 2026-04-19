"""
Master pipeline -- SequentialAgent assembly for the documentary pipeline.

Architecture::

    SequentialAgent("documentary_pipeline")
    ├── LoopAgent("scenario_director")        # script generation + ADHD eval
    │   └── [APPROVAL GATE: human approves scenario]
    ├── Agent("audio_agent")                  # TTS + WhisperX alignment
    ├── LoopAgent("visual_director")          # visual planning loop
    │   ├── Agent("content_analyst")
    │   ├── Agent("visual_concepter")
    │   └── Agent("coherence_evaluator")
    │   └── [APPROVAL GATE: human approves visual prompts]
    ├── Agent("production_supervisor")        # GPU video generation
    │   └── [APPROVAL GATE: human approves clips]
    └── Agent("assembler_agent")              # final assembly

Data flows via session state (blackboard pattern):
  - scenario_director -> state["scenes"]
  - audio_agent -> state["whisperx_alignment"]
  - visual_director -> state["content_analysis"], state["visual_concepts"]
  - production_supervisor -> OTIO timeline clips
  - assembler_agent -> final documentary output

Human-in-the-loop gates (AG-UI approval workflow):
  Each stage pauses after completion and waits for human approval
  on the dashboard before the next stage proceeds.  The pipeline
  polls .approval_state.json on disk until the human clicks "Approve".
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from google.adk.agents import Agent, SequentialAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.loop_agent import LoopAgent
from google.genai import types as genai_types

from agents.assembler_agent import assembler_agent
from agents.audio_agent import audio_agent
from agents.intent_extractor import (
    BRIEF_INTENT_KEY,
    IntentExtractionError,
    run_intent_extractor,
)
from agents.production_supervisor import production_supervisor
from agents.scenario_director import scenario_director
from agents.scenario_refiner import scenario_refiner
from agents.timing_evaluator import timing_evaluator
from agents.visual_director import visual_director
from callbacks.approval_gate import (
    is_stage_approved,
    mark_stage_ready,
    wait_for_approval,
)
from callbacks.consistency_gate import wire_consistency_checks_into_agents
from callbacks.intent_gate import (
    GATE_ATTEMPT_KEY,
    GATE_CRITIQUE_KEY,
    MAX_GATE_ATTEMPTS,
    IntentGateHalt,
    reset_intent_gate,
    run_preflight_gate,
)
from callbacks.intent_verifier import verify_and_log
from callbacks.state_manager import build_pipeline_state, safe_state_dict
from contracts import (
    ASSEMBLY_CONTRACT,
    AUDIO_CONTRACT,
    PRODUCTION_CONTRACT,
    SCENARIO_CONTRACT,
    VISUAL_DIRECTION_CONTRACT,
    ContractViolation,
    validate_postconditions,
    validate_preconditions,
)
from testing.simulation_bridge import create_agent_callback, is_simulation_active
from tools.otio_tools import _timeline_path

logger = logging.getLogger(__name__)


def _validate_preconditions_or_abort(
    contract,
    callback_context: CallbackContext,
) -> Optional[genai_types.Content]:
    """Validate stage preconditions; return Content to abort if violated.

    This is the graph-level precondition guard learned from the Strands
    migration.  It runs BEFORE each agent starts, preventing wasted GPU
    time when upstream state is missing or placeholder.

    Returns None if preconditions pass, or Content with an error message
    if they fail (which causes ADK to skip the agent).
    """
    try:
        validate_preconditions(contract, safe_state_dict(callback_context.state))
        return None
    except ContractViolation as cv:
        logger.error(
            "stage=<%s> | CONTRACT precondition FAILED — skipping agent: %s",
            contract.name, cv,
        )
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(
                text=f"CONTRACT VIOLATION: {cv}"
            )],
        )


def _validate_postconditions_and_log(
    contract,
    callback_context: CallbackContext,
) -> None:
    """Validate stage postconditions after completion; log but don't block.

    Postcondition violations are logged as errors for visibility but do
    not abort — the downstream stage's precondition check will catch the
    missing output and halt cleanly.
    """
    try:
        validate_postconditions(contract, safe_state_dict(callback_context.state))
        logger.info(
            "stage=<%s> | CONTRACT postconditions PASSED",
            contract.name,
        )
    except ContractViolation as cv:
        logger.error(
            "stage=<%s> | CONTRACT postcondition FAILED: %s",
            contract.name, cv,
        )


# ---------------------------------------------------------------------------
# Timing feedback loop (R3 from deep audit — fixes ~30-40% of duration
# compliance failures).  Wraps audio generation + timing evaluation +
# scenario refinement in a LoopAgent so that if audio overshoots the
# duration budget, the scenario is automatically refined and audio
# regenerated.  Max 3 iterations; the refiner's before_agent_callback
# returns Content (skipping the LLM) when timing passes, and sets
# actions.escalate=True so the LoopAgent exits immediately.
# ---------------------------------------------------------------------------
timing_loop = LoopAgent(
    name="timing_loop",
    description=(
        "Audio generation with timing feedback: generates TTS narration, "
        "evaluates duration compliance, and refines scene text if the "
        "total duration deviates from the target budget by more than 15%."
    ),
    max_iterations=3,
    sub_agents=[
        audio_agent,
        timing_evaluator,
        scenario_refiner,
    ],
)


# ---------------------------------------------------------------------------
# Approval-gate wrappers that compose with existing sub-agent callbacks
# ---------------------------------------------------------------------------
# We monkey-patch the sub-agents' after_agent_callback and
# before_agent_callback to inject approval gate logic.  This avoids
# editing every individual agent file and keeps the gate logic central.

_orig_scenario_after = scenario_director.after_agent_callback
_orig_timing_loop_before = timing_loop.before_agent_callback
_orig_timing_loop_after = timing_loop.after_agent_callback
_orig_visual_after = visual_director.after_agent_callback
_orig_production_before = production_supervisor.before_agent_callback
_orig_production_after = production_supervisor.after_agent_callback
_orig_assembly_before = assembler_agent.before_agent_callback


def _scenario_after_postconditions(callback_context):
    """After each scenario_director iteration: validate postconditions.

    The approval gate + R0 constraint gate fire later on the outer
    ``scenario_with_gate`` LoopAgent so the human only approves (and we
    only evaluate R0) on the *final* scenario draft, not on intermediate
    refiner-loop iterations.
    """
    result = None
    if _orig_scenario_after:
        result = _orig_scenario_after(callback_context)
    _validate_postconditions_and_log(SCENARIO_CONTRACT, callback_context)
    return result


def _preflight_gate_before(callback_context):
    """Evaluate the R0 constraint gate (INTENT-02) and drive the outer loop.

    Runs as a pure-callback sub-agent — no LLM call.  Behaviour:

    * Gate passes → set ``actions.escalate=True`` so the outer
      ``scenario_with_gate`` LoopAgent exits immediately and downstream
      stages (audio, visual, production) proceed.  INTENT-05 worker
      provisioning is unblocked by the same call
      (see :func:`callbacks.intent_gate.run_preflight_gate`).
    * Gate fails with retries left → leave ``escalate`` unset so the
      LoopAgent re-enters ``scenario_director`` on the next iteration;
      the critique written under ``GATE_CRITIQUE_KEY`` is visible to
      the director's instruction.
    * Gate halts (retries exhausted) → emit a plain-English
      ``halt_fired`` narrator event, set ``escalate=True``, and return
      an error Content so the SequentialAgent short-circuits.
    """
    state = callback_context.state
    try:
        verdict = run_preflight_gate(state, max_attempts=MAX_GATE_ATTEMPTS)
    except IntentGateHalt as halt:
        callback_context.actions.escalate = True
        logger.error("INTENT-02: gate halted after max attempts — %s", halt)
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text=f"HALT: {halt}")],
        )

    if verdict.passed:
        callback_context.actions.escalate = True
        logger.info(
            "INTENT-02: gate PASSED on attempt %d — unblocking downstream "
            "stages and lazy GPU provisioning",
            verdict.attempt,
        )
        try:
            from agents.chat_narrator import emit_narrator_event

            emit_narrator_event(
                "stage_completed",
                fields={
                    "stage": "scenario",
                    "checkpoint": "intent_gate",
                    "message": (
                        "Understood your brief, drafted the scenario, "
                        "passed the constraint check — booking GPUs now."
                    ),
                },
            )
        except Exception as exc:  # pragma: no cover -- best-effort narration
            logger.debug("INTENT-02: narrator emit on pass failed: %s", exc)
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(text="INTENT GATE PASSED")],
        )

    logger.warning(
        "INTENT-02: gate FAILED on attempt %d — retrying scenario_director",
        verdict.attempt,
    )
    return genai_types.Content(
        role="model",
        parts=[genai_types.Part(
            text="INTENT GATE FAILED — retrying with critique"
        )],
    )


def _scenario_stage_after(callback_context):
    """After the full scenario_with_gate loop: R0 verify + approval gate."""
    # INTENT-04: re-verify R0 constraints on the final scenario artefact.
    record = verify_and_log("scenario", callback_context.state)
    if not record.passed:
        logger.error(
            "INTENT-04 scenario verification FAILED after gate: %s",
            "; ".join(record.failures),
        )
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(
                text=(
                    "HALT: scenario artefact drifted from R0 after the "
                    "constraint gate — " + "; ".join(record.failures)
                )
            )],
        )
    mark_stage_ready("scenario")
    logger.info("APPROVAL GATE: scenario stage ready — waiting for human approval")
    approved = wait_for_approval("scenario", state=callback_context.state)
    if not approved:
        logger.error("APPROVAL GATE: timed out waiting for scenario approval")
    return None


def _timing_loop_before_with_gate(callback_context):
    """Before timing_loop: approval gate + TTS worker + contract check.

    The timing_loop wraps audio_agent + timing_evaluator + scenario_refiner.
    All pre-checks (contracts, approval, worker binding) happen once before
    the loop starts, not on every iteration.

    ORDERING IS CRITICAL: the contract check must run AFTER the TTS worker
    is ready because AUDIO_CONTRACT.required_services includes a health
    check to TTS_WORKER_URL.  If the check runs before provisioning
    completes, it fails and silently skips the entire audio stage.
    """
    if not is_stage_approved("scenario"):
        logger.info("APPROVAL GATE: audio waiting for scenario approval...")
        approved = wait_for_approval("scenario", state=callback_context.state)
        if not approved:
            return genai_types.Content(
                role="model",
                parts=[genai_types.Part(
                    text="ERROR: Timed out waiting for scenario approval."
                )],
            )

    # Lazy worker binding: wait for TTS worker only when audio stage needs it.
    # The worker was started provisioning in background during _init_pipeline_state.
    try:
        from worker_provisioner import get_provisioner
        provisioner = get_provisioner()
        provisioner.wait_for_worker("tts", timeout=2700)
        logger.info("TTS worker ready — timing loop proceeding")
    except Exception as exc:
        logger.error("TTS worker not available: %s", exc)
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(
                text=f"ERROR: TTS worker provisioning failed: {exc}"
            )],
        )

    # CONTRACT: validate preconditions AFTER TTS worker is ready.
    # AUDIO_CONTRACT includes service health checks that require the
    # worker to be reachable — running this earlier would cause a
    # ContractViolation that silently skips the entire timing loop.
    abort = _validate_preconditions_or_abort(AUDIO_CONTRACT, callback_context)
    if abort is not None:
        return abort

    if _orig_timing_loop_before:
        return _orig_timing_loop_before(callback_context)
    return None


def _timing_loop_after_with_gate(callback_context):
    """After timing_loop: validate AUDIO_CONTRACT postconditions + mark audio ready."""
    result = None
    if _orig_timing_loop_after:
        result = _orig_timing_loop_after(callback_context)
    _validate_postconditions_and_log(AUDIO_CONTRACT, callback_context)
    # INTENT-04: narration-duration drift check against R0.
    audio_record = verify_and_log("audio", callback_context.state)
    if not audio_record.passed:
        logger.error(
            "INTENT-04 audio verification FAILED: %s",
            "; ".join(audio_record.failures),
        )
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(
                text=(
                    "HALT: narration duration drifted from R0 — "
                    + "; ".join(audio_record.failures)
                )
            )],
        )
    mark_stage_ready("audio")
    logger.info("APPROVAL GATE: audio stage ready — waiting for human approval")
    approved = wait_for_approval("audio", state=callback_context.state)
    if not approved:
        logger.error("APPROVAL GATE: timed out waiting for audio approval")
    return result


def _visual_after_with_gate(callback_context):
    """After visual_director: validate postconditions, then mark prompts ready."""
    result = None
    if _orig_visual_after:
        result = _orig_visual_after(callback_context)
    _validate_postconditions_and_log(VISUAL_DIRECTION_CONTRACT, callback_context)
    # INTENT-04: visual-direction coverage / aspect-ratio drift check.
    visual_record = verify_and_log("visual", callback_context.state)
    if not visual_record.passed:
        logger.error(
            "INTENT-04 visual verification FAILED: %s",
            "; ".join(visual_record.failures),
        )
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(
                text=(
                    "HALT: visual direction drifted from R0 — "
                    + "; ".join(visual_record.failures)
                )
            )],
        )
    mark_stage_ready("prompts")
    logger.info("APPROVAL GATE: prompts stage ready — waiting for human approval")
    approved = wait_for_approval("prompts", state=callback_context.state)
    if not approved:
        logger.error("APPROVAL GATE: timed out waiting for prompts approval")
    return result


def _production_before_with_gate(callback_context):
    """Before production_supervisor: approval gate + video worker + contract check.

    ORDERING IS CRITICAL: the contract check must run AFTER the video
    worker is ready because PRODUCTION_CONTRACT.required_services includes
    a health check to VIDEO_WORKER_URLS.  If the check runs before
    provisioning completes, it fails and silently skips the entire
    production stage.  Same pattern as _timing_loop_before_with_gate.
    """
    if not is_stage_approved("prompts"):
        logger.info("APPROVAL GATE: production waiting for prompts approval...")
        approved = wait_for_approval("prompts", state=callback_context.state)
        if not approved:
            return genai_types.Content(
                role="model",
                parts=[genai_types.Part(
                    text="ERROR: Timed out waiting for prompts approval."
                )],
            )

    # Lazy worker binding: wait for video worker only when production stage needs it.
    # The worker was started provisioning in background during _init_pipeline_state.
    try:
        from worker_provisioner import get_provisioner
        provisioner = get_provisioner()
        provisioner.wait_for_worker("video", timeout=2700)
        logger.info("Video worker ready — production stage proceeding")
    except Exception as exc:
        logger.error("Video worker not available: %s", exc)
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(
                text=f"ERROR: Video worker provisioning failed: {exc}"
            )],
        )

    # CONTRACT: validate preconditions AFTER video worker is ready.
    # PRODUCTION_CONTRACT includes service health checks that require the
    # worker to be reachable — running this earlier would cause a
    # ContractViolation that silently skips the entire production stage.
    abort = _validate_preconditions_or_abort(PRODUCTION_CONTRACT, callback_context)
    if abort is not None:
        return abort

    if _orig_production_before:
        return _orig_production_before(callback_context)
    return None


def _production_after_with_gate(callback_context):
    """After production_supervisor: validate postconditions, then mark clips ready."""
    result = None
    if _orig_production_after:
        result = _orig_production_after(callback_context)
    _validate_postconditions_and_log(PRODUCTION_CONTRACT, callback_context)
    # INTENT-04: per-clip timing / aspect-ratio drift check.
    prod_record = verify_and_log("production", callback_context.state)
    if not prod_record.passed:
        logger.error(
            "INTENT-04 production verification FAILED: %s",
            "; ".join(prod_record.failures),
        )
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(
                text=(
                    "HALT: clip production drifted from R0 — "
                    + "; ".join(prod_record.failures)
                )
            )],
        )
    mark_stage_ready("clips")
    logger.info("APPROVAL GATE: clips stage ready — waiting for human approval")
    approved = wait_for_approval("clips", state=callback_context.state)
    if not approved:
        logger.error("APPROVAL GATE: timed out waiting for clips approval")
    return result


def _assembly_before_with_gate(callback_context):
    """Before assembler_agent: contract check + approval gate, then run original."""
    # CONTRACT: validate preconditions BEFORE entering assembly stage
    abort = _validate_preconditions_or_abort(ASSEMBLY_CONTRACT, callback_context)
    if abort is not None:
        return abort

    if not is_stage_approved("clips"):
        logger.info("APPROVAL GATE: assembly waiting for clips approval...")
        approved = wait_for_approval("clips", state=callback_context.state)
        if not approved:
            return genai_types.Content(
                role="model",
                parts=[genai_types.Part(
                    text="ERROR: Timed out waiting for clips approval."
                )],
            )
    if _orig_assembly_before:
        return _orig_assembly_before(callback_context)
    return None


# Wire approval gates into sub-agents
scenario_director.after_agent_callback = _scenario_after_postconditions
timing_loop.before_agent_callback = _timing_loop_before_with_gate
timing_loop.after_agent_callback = _timing_loop_after_with_gate
visual_director.after_agent_callback = _visual_after_with_gate
production_supervisor.before_agent_callback = _production_before_with_gate
production_supervisor.after_agent_callback = _production_after_with_gate
assembler_agent.before_agent_callback = _assembly_before_with_gate


# ---------------------------------------------------------------------------
# INTENT-02 (#266): pre-flight constraint-gate sub-agent + outer LoopAgent
# wrapping the scenario director.  On each iteration the scenario director
# drafts (or redrafts) scenes, then the constraint_gate_agent evaluates R0.
# When the gate passes it sets ``actions.escalate=True`` so the loop exits
# and downstream stages proceed.  When it fails (with retries remaining)
# the loop re-enters scenario_director with the critique staged under
# ``GATE_CRITIQUE_KEY`` so the director's next attempt knows exactly what
# to fix.  On retry exhaustion the gate halts the pipeline.
# ---------------------------------------------------------------------------
from agents.model_config import build_model as _build_model  # noqa: E402


constraint_gate_agent = Agent(
    name="constraint_gate_agent",
    model=_build_model(),
    instruction=(
        "You are a placeholder for the R0 constraint gate.  The "
        "before_agent_callback on this agent is the real gate — it "
        "runs deterministically and either exits the outer loop on "
        "pass or feeds a targeted critique back to scenario_director "
        "on fail.  You should never be invoked."
    ),
    tools=[],
    before_agent_callback=_preflight_gate_before,
)


scenario_with_gate = LoopAgent(
    name="scenario_with_gate",
    description=(
        "Scenario director wrapped by the INTENT-02 R0 constraint gate. "
        "Redrafts up to MAX_GATE_ATTEMPTS times when the gate detects "
        "duration, required-topic, or forbidden-topic drift."
    ),
    max_iterations=MAX_GATE_ATTEMPTS,
    sub_agents=[scenario_director, constraint_gate_agent],
    after_agent_callback=_scenario_stage_after,
)


# ---------------------------------------------------------------------------
# ARCH-B2 (#138) -- wire the A5 consistency checker into every stage
# boundary, every before-agent entry, and every tool call.
#
# Invocation points (per issue #138): "stage transitions, approval-gate
# polling, tool calls that mutate artifacts". A stage that starts with a
# stale ``ledger_revision_at_birth`` triggers a drift signal which the
# ARCH-B3 (#139) re-manifestation executor consumes; on exhaustion it
# re-escalates to human L4 through :mod:`recovery`. No silent degradation.
#
# The wiring composes with the approval-gate wrappers above so gate logic
# (and contract validation) runs first, then A5 checks for drift, then B3
# drains the drift queue. Idempotent -- a second import does not double-
# chain (see ``_WIRED_ATTR`` in ``callbacks.consistency_gate``).
# ---------------------------------------------------------------------------
from types import SimpleNamespace as _SimpleNamespace  # noqa: E402

_b2_wired_agents = wire_consistency_checks_into_agents(
    # Non-ADK holder so agents don't acquire a parent before the real
    # ``documentary_pipeline`` SequentialAgent wraps them. ``_iter_agent_tree``
    # only needs a ``sub_agents`` attribute to walk the list.
    _SimpleNamespace(
        name="_arch_b2_wiring_root",
        sub_agents=[
            scenario_director,
            timing_loop,
            visual_director,
            production_supervisor,
            assembler_agent,
        ],
    )
)
logger.info(
    "ARCH-B2: consistency checks wired into %d sub-agent(s): %s",
    len(_b2_wired_agents),
    _b2_wired_agents,
)


# ---------------------------------------------------------------------------
# Simulation callback wiring
# ---------------------------------------------------------------------------

# Store original (pre-simulation) callbacks so re-wiring restores them first.
_original_callbacks: dict[str, Any] = {}
_simulation_wired = False


def _wire_simulation_callbacks(sim_callback) -> None:
    """Compose a dynamic simulation before_tool_callback onto each agent.

    The composed callback reads from ``SimulationRegistry`` at call time, so
    it always uses the *current* scenario's engine — even if
    ``deactivate_simulation()`` + ``activate_simulation(new_config)`` was
    called between runs.  This means multi-scenario test runners work without
    needing to re-wire callbacks for each scenario.

    The ``sim_callback`` parameter (from ``EnvironmentSimulationFactory``) is
    kept as a fast-path for ADK-registered tools, but the dynamic registry
    check ensures correctness even if the factory callback becomes stale.
    """
    global _simulation_wired

    agents_to_wire = [
        scenario_director,
        timing_loop,
        visual_director,
        production_supervisor,
        assembler_agent,
    ]

    # Walk sub-agents too (LoopAgent children, etc.)
    expanded = []
    for agent in agents_to_wire:
        expanded.append(agent)
        if hasattr(agent, "sub_agents"):
            for sub in agent.sub_agents:
                expanded.append(sub)

    for agent in expanded:
        # On first wiring, save the original callback; on re-wiring, restore it
        # so we don't compose on top of a previous composition.
        if agent.name in _original_callbacks:
            orig = _original_callbacks[agent.name]
        else:
            orig = agent.before_tool_callback
            _original_callbacks[agent.name] = orig

        def _compose(original_cb, agent_name):
            """Create a composed callback that checks SimulationRegistry dynamically."""
            async def _composed(callback_context, tool_name, tool_input):
                # Dynamic check: is simulation still active?
                from testing.simulation_bridge import is_simulation_active

                if is_simulation_active():
                    result = None
                    try:
                        if asyncio.iscoroutinefunction(sim_callback):
                            result = await sim_callback(callback_context, tool_name, tool_input)
                        else:
                            result = sim_callback(callback_context, tool_name, tool_input)
                    except Exception as exc:
                        logger.debug(
                            "Simulation callback error for %s.%s: %s",
                            agent_name, tool_name, exc,
                        )

                    if result is not None:
                        logger.info(
                            "Simulation intercepted ADK tool %s on %s",
                            tool_name, agent_name,
                        )
                        return result

                # No simulation match or simulation inactive — run original
                if original_cb is not None:
                    if asyncio.iscoroutinefunction(original_cb):
                        return await original_cb(callback_context, tool_name, tool_input)
                    return original_cb(callback_context, tool_name, tool_input)
                return None

            return _composed

        agent.before_tool_callback = _compose(orig, agent.name)
        logger.debug("Simulation callback composed onto agent: %s", agent.name)

    _simulation_wired = True


def _reset_simulation_wiring() -> None:
    """Restore original callbacks and allow re-wiring.

    Called by ``deactivate_simulation()`` so the next ``activate_simulation()``
    can wire a fresh callback from the new scenario's config.
    """
    global _simulation_wired
    _simulation_wired = False


# ---------------------------------------------------------------------------
# Pipeline-level callbacks
# ---------------------------------------------------------------------------

def _init_pipeline_state(
    callback_context: CallbackContext,
) -> Optional[genai_types.Content]:
    """Ensure session state has pipeline keys before the pipeline starts.

    AG-UI creates sessions without initial state, so the first time the
    pipeline runs we inject the keys that all agents read/write.

    Also ensures GPU workers are provisioned and healthy before any stage
    that needs them.  This solves the chicken-and-egg problem where
    contract precondition checks run before the pipeline agent can
    provision workers.
    """
    import os

    state = callback_context.state
    if "_pipeline_key" not in state:
        for k, v in build_pipeline_state().items():
            state[k] = v
        logger.info(
            "Pipeline state initialised: pipeline_key=%s",
            state["_pipeline_key"],
        )

    # ARCH-A3 (#133): seed the Preference Ledger with R0 baseline
    # records parsed from the original brief, so every run begins with
    # a non-empty ledger rather than revision 0.  Idempotent -- re-runs
    # (AG-UI re-entry, B2 restore) that already carry R0 records skip.
    try:
        from callbacks.run_start_seed import (
            RunStartSeedError,
            seed_ledger_from_brief,
        )
        # Disable the LLM path unless explicitly enabled -- the CI env
        # has no google-genai credentials, and the heuristic baseline
        # is sufficient to satisfy the A3 contract (non-empty ledger
        # covering every canonical subject).
        _r0_use_llm = os.environ.get(
            "ARCH_A_R0_USE_LLM", ""
        ).strip().lower() in ("1", "true", "yes")
        seed_ledger_from_brief(state, use_llm=_r0_use_llm)
    except RunStartSeedError as seed_err:
        logger.error(
            "ARCH-A3 R0 seed failed: %s -- refusing to start pipeline "
            "with an empty Preference Ledger",
            seed_err,
        )
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(
                text=f"ERROR: R0 seed failed: {seed_err}"
            )],
        )

    # INTENT-01 (#265): parse the free-text brief into a typed BriefIntent
    # and stage it on the blackboard under BRIEF_INTENT_KEY.  This runs
    # BEFORE any producer agent so the pre-flight gate (INTENT-02) and
    # per-stage verifiers (INTENT-04) always have R0 constraints to check.
    reset_intent_gate()
    state[GATE_ATTEMPT_KEY] = 0
    state.pop(GATE_CRITIQUE_KEY, None)
    try:
        run_intent_extractor(state, use_llm=_r0_use_llm)
    except IntentExtractionError as intent_err:
        logger.error(
            "INTENT-01 extraction failed: %s -- refusing to start pipeline "
            "without a typed BriefIntent (R0)",
            intent_err,
        )
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(
                text=f"ERROR: intent extraction failed: {intent_err}"
            )],
        )

    # ── ADK Environment Simulation wiring ──────────────────────────────
    # When a simulation scenario is active, compose the ADK-native
    # before_tool_callback with each agent's existing callback so that
    # ADK FunctionTools are intercepted by the EnvironmentSimulationEngine.
    # Callback-called functions are already handled by the @simulated decorator.
    if is_simulation_active():
        sim_callback = create_agent_callback()
        if sim_callback:
            _wire_simulation_callbacks(sim_callback)
            logger.info("Simulation callbacks wired into all pipeline agents")

    # ── Trace capture for across-run learning ─────────────────────────
    try:
        from orchestrator.trace_capture import get_trace_capture
        capture = get_trace_capture()
        capture.start_run(
            pipeline_key=state.get("_pipeline_key", "unknown"),
            topic=state.get("topic", ""),
            metadata={
                "quick_test": state.get("quick_test", "false"),
                "fleet_mode": os.environ.get("FLEET_MODE", "false"),
            },
        )
    except Exception as tc_err:
        logger.debug("Trace capture init skipped: %s", tc_err)

    # ── OTel trace unification ────────────────────────────────────────
    try:
        from orchestrator.otel_bridge import instrument_production_agent
        instrument_production_agent()
    except Exception as otel_err:
        logger.debug("OTel bridge init skipped: %s", otel_err)

    # Inject quick-test template variables from env if not already set.
    # run_pipeline.py sets these for the CLI path; this handles the AG-UI path.
    if os.environ.get("DOCUMENTARY_QUICK_TEST", "").strip().lower() in ("1", "true", "yes"):
        if not state.get("quick_test_rules"):
            from agents.scenario_director import _QUICK_TEST_RULES
            state["quick_test"] = "true"
            state["quick_test_rules"] = _QUICK_TEST_RULES
            state["max_scene_duration"] = "15"
            state["max_words_per_scene"] = "37"
            logger.info("Quick-test mode enabled from env var")

    # Pre-compute and store timeline path so all sub-agents can find it.
    # The scenario director also sets this via create_timeline(), but that
    # write may not propagate out of the LoopAgent scope.
    topic = state.get("topic", "")
    if topic and not state.get("_timeline_path"):
        state["_timeline_path"] = _timeline_path(topic)
        logger.info("Pre-set _timeline_path=%s", state["_timeline_path"])

    # ── Parallel lazy worker provisioning ─────────────────────────────
    # Start GPU worker provisioning in a SEPARATE THREAD.
    # start_provisioning() does blocking I/O (health checks, Vast.ai API)
    # before launching per-worker threads.  Running it directly here would
    # block the uvicorn async event loop and freeze the entire server.
    # Each stage waits for only the worker it needs:
    #   - Audio stage calls wait_for_worker("tts") in its before_callback
    #   - Production stage calls wait_for_worker("video") in its before_callback
    #
    # IMPORTANT: Always reset _workers_provisioned at pipeline start.
    # B2 state restore can carry over the True flag from a previous failed
    # run (where _cleanup_pipeline_state never executed), causing this block
    # to be skipped entirely — leaving the pipeline with no workers.
    state["_workers_provisioned"] = False
    # Always provision — the reset above ensures stale B2 state can't skip this
    if True:
        import threading
        from worker_provisioner import get_provisioner

        provisioner = get_provisioner()

        def _start_provisioning_bg():
            # INTENT-05 (#269): lazy GPU — do not start booting VMs until
            # the pre-flight R0 constraint gate (INTENT-02) has passed.
            # Cancelling a run before the gate passes costs zero
            # GPU-seconds.  The gate signal is a process-wide
            # threading.Event set by run_preflight_gate on pass; we wait
            # here with a generous timeout that still bounds the life of
            # this background thread.
            try:
                from callbacks.intent_gate import wait_for_intent_gate

                lazy_gpu_timeout = float(
                    os.environ.get("INTENT_GATE_WAIT_SEC", "3600")
                )
                fired = wait_for_intent_gate(timeout_sec=lazy_gpu_timeout)
                if not fired:
                    logger.error(
                        "INTENT-05: intent gate never signalled within "
                        "%.0fs — aborting lazy GPU provisioning",
                        lazy_gpu_timeout,
                    )
                    provisioner._provision_start_error = (
                        "intent gate did not pass — workers not provisioned"
                    )
                    provisioner._specs_ready.set()
                    return
                logger.info(
                    "INTENT-05: intent gate passed — booking GPUs now"
                )
            except Exception as exc:
                logger.error(
                    "INTENT-05: wait_for_intent_gate failed: %s", exc
                )
                provisioner._provision_start_error = str(exc)
                provisioner._specs_ready.set()
                return

            try:
                provisioner.start_provisioning(
                    require_tts=True,
                    require_video=True,
                )
                logger.info(
                    "Background worker provisioning started — "
                    "VMs bootstrapping while scenario runs"
                )

                # Fleet mode: provision additional VMs via FleetScaler
                # (rolling start — first VM already provisioned above,
                # additional VMs boot in parallel and join as they come online)
                fleet_mode = os.environ.get("FLEET_MODE", "").strip().lower() in ("1", "true")
                if fleet_mode:
                    try:
                        from fleet.coordinator import get_fleet_coordinator
                        coordinator = get_fleet_coordinator()
                        if coordinator:
                            # Estimate clip count from state (visual concepts not
                            # generated yet, so use a reasonable default)
                            budget = float(os.environ.get("PRODUCTION_BUDGET", "0"))
                            est_clips = int(os.environ.get("ESTIMATED_CLIPS", "30"))
                            n = coordinator.provision_fleet(
                                num_clips=est_clips,
                                budget_ceiling=budget,
                            )
                            logger.info(
                                "Fleet mode: %d additional VMs provisioning "
                                "(rolling start — first healthy VM pulls work "
                                "immediately, others join as they boot)",
                                max(0, n - 1),
                            )
                    except Exception as fleet_err:
                        logger.warning(
                            "Fleet provisioning failed (single-worker mode): %s",
                            fleet_err,
                        )

            except Exception as exc:
                logger.error("Worker provisioning failed to start: %s", exc)
                # Store the error on the provisioner so wait_for_worker()
                # can surface a clear message instead of "No worker spec".
                provisioner._provision_start_error = str(exc)
                # Signal _specs_ready so wait_for_worker() unblocks immediately
                # and checks _provision_start_error instead of waiting 120s.
                provisioner._specs_ready.set()

        t = threading.Thread(
            target=_start_provisioning_bg,
            name="provision-launcher",
            daemon=True,
        )
        t.start()
        # Mark as "attempted" — the flag means provisioning was kicked off,
        # not that it succeeded.  If it fails, the error is stored on the
        # provisioner singleton and wait_for_worker() will surface it.
        # _cleanup_pipeline_state resets this flag so re-runs re-provision.
        state["_workers_provisioned"] = True
        logger.info(
            "Provisioning launcher thread started — "
            "scenario stage will run while VMs bootstrap"
        )

    return None


def _cleanup_pipeline_state(
    callback_context: CallbackContext,
) -> Optional[genai_types.Content]:
    """Cleanup after pipeline completes."""
    state = callback_context.state
    state["pipeline_phase"] = "completed"
    mark_stage_ready("assembly")
    logger.info(
        "Pipeline completed: pipeline_key=%s",
        state.get("_pipeline_key", "unknown"),
    )

    # Save trace capture for across-run learning
    try:
        from orchestrator.trace_capture import get_trace_capture
        capture = get_trace_capture()
        capture.end_run(
            summary=f"Pipeline completed: {state.get('_pipeline_key', 'unknown')}"
        )
        trace_path = capture.save()
        logger.info("Production trace saved: %s", trace_path)
    except Exception as tc_err:
        logger.debug("Trace capture save skipped: %s", tc_err)

    # Cleanup worker provisioner (SSH tunnels, InfraAgent)
    try:
        from worker_provisioner import get_provisioner
        provisioner = get_provisioner()
        provisioner.cleanup()
    except Exception as exc:
        logger.warning("Worker provisioner cleanup error: %s", exc)

    # Cleanup fleet coordinator if running
    try:
        from fleet.coordinator import get_fleet_coordinator, reset_fleet_coordinator
        coordinator = get_fleet_coordinator()
        if coordinator:
            coordinator.shutdown()
            reset_fleet_coordinator()
            logger.info("Fleet coordinator shut down during pipeline cleanup")
    except Exception as fleet_err:
        logger.debug("Fleet coordinator cleanup skipped: %s", fleet_err)

    # Reset provisioning flag so re-runs in the same session re-provision
    state["_workers_provisioned"] = False

    return None


pipeline_agent = SequentialAgent(
    name="documentary_pipeline",
    description=(
        "ADHD-friendly documentary pipeline: intent extraction (R0), "
        "scenario generation wrapped by the R0 constraint gate, TTS "
        "narration with timing feedback loop (audio → evaluate → refine "
        "→ re-audio), iterative visual planning with LoRA selection, "
        "GPU video production, and final assembly. Per-stage R0 "
        "verification fails closed on drift. Each stage pauses for "
        "human approval before the next one begins."
    ),
    sub_agents=[
        scenario_with_gate,
        timing_loop,
        visual_director,
        production_supervisor,
        assembler_agent,
    ],
    before_agent_callback=_init_pipeline_state,
    after_agent_callback=_cleanup_pipeline_state,
)
