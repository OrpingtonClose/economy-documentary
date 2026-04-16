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

from google.adk.agents import SequentialAgent
from google.adk.agents.callback_context import CallbackContext
from google.genai import types as genai_types

from agents.assembler_agent import assembler_agent
from agents.audio_agent import audio_agent
from agents.production_supervisor import production_supervisor
from agents.scenario_director import scenario_director
from agents.visual_director import visual_director
from callbacks.approval_gate import (
    is_stage_approved,
    mark_stage_ready,
    wait_for_approval,
)
from callbacks.state_manager import build_pipeline_state
from testing.simulation_bridge import create_agent_callback, is_simulation_active
from tools.otio_tools import _timeline_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Approval-gate wrappers that compose with existing sub-agent callbacks
# ---------------------------------------------------------------------------
# We monkey-patch the sub-agents' after_agent_callback and
# before_agent_callback to inject approval gate logic.  This avoids
# editing every individual agent file and keeps the gate logic central.

_orig_scenario_after = scenario_director.after_agent_callback
_orig_audio_before = audio_agent.before_agent_callback
_orig_visual_after = visual_director.after_agent_callback
_orig_production_before = production_supervisor.before_agent_callback
_orig_production_after = production_supervisor.after_agent_callback
_orig_assembly_before = assembler_agent.before_agent_callback


def _scenario_after_with_gate_sync(callback_context):
    """After scenario_director: run original callback, then mark ready (sync)."""
    result = None
    if _orig_scenario_after:
        result = _orig_scenario_after(callback_context)
    mark_stage_ready("scenario")
    logger.info("APPROVAL GATE: scenario stage ready — waiting for human approval")
    approved = wait_for_approval("scenario")
    if not approved:
        logger.error("APPROVAL GATE: timed out waiting for scenario approval")
    return result


async def _scenario_after_with_gate(callback_context):
    """After scenario_director: async wrapper to keep event loop free."""
    return await asyncio.to_thread(_scenario_after_with_gate_sync, callback_context)


def _audio_before_with_gate_sync(callback_context):
    """Before audio_agent: wait for approval + TTS worker, then run original (sync)."""
    if not is_stage_approved("scenario"):
        logger.info("APPROVAL GATE: audio waiting for scenario approval...")
        approved = wait_for_approval("scenario")
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
        logger.info("TTS worker ready — audio stage proceeding")
    except Exception as exc:
        logger.error("TTS worker not available: %s", exc)
        return genai_types.Content(
            role="model",
            parts=[genai_types.Part(
                text=f"ERROR: TTS worker provisioning failed: {exc}"
            )],
        )

    if _orig_audio_before:
        return _orig_audio_before(callback_context)
    return None


async def _audio_before_with_gate(callback_context):
    """Before audio_agent: async wrapper to keep event loop free for heartbeats.

    The deterministic_audio_callback does heavy blocking I/O (SSH tunnels
    to Vast.ai, TTS inference, file downloads, B2 uploads) that can take
    10+ minutes.  Running it in a thread keeps the asyncio event loop free
    so the unified SSE endpoint can emit heartbeat comments and serve
    dashboard REST polls.
    """
    return await asyncio.to_thread(_audio_before_with_gate_sync, callback_context)


def _visual_after_with_gate_sync(callback_context):
    """After visual_director: run original callback, then mark prompts ready (sync)."""
    result = None
    if _orig_visual_after:
        result = _orig_visual_after(callback_context)
    mark_stage_ready("prompts")
    logger.info("APPROVAL GATE: prompts stage ready — waiting for human approval")
    approved = wait_for_approval("prompts")
    if not approved:
        logger.error("APPROVAL GATE: timed out waiting for prompts approval")
    return result


async def _visual_after_with_gate(callback_context):
    """After visual_director: async wrapper to keep event loop free."""
    return await asyncio.to_thread(_visual_after_with_gate_sync, callback_context)


def _production_before_with_gate_sync(callback_context):
    """Before production_supervisor: wait for approval + video worker (sync)."""
    if not is_stage_approved("prompts"):
        logger.info("APPROVAL GATE: production waiting for prompts approval...")
        approved = wait_for_approval("prompts")
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

    if _orig_production_before:
        return _orig_production_before(callback_context)
    return None


async def _production_before_with_gate(callback_context):
    """Before production_supervisor: async wrapper to keep event loop free.

    Video generation does heavy blocking I/O (SSH tunnels, LTX inference,
    B2 uploads) that can take 30+ minutes.  Running in a thread keeps the
    event loop free for SSE heartbeats.
    """
    return await asyncio.to_thread(_production_before_with_gate_sync, callback_context)


def _production_after_with_gate_sync(callback_context):
    """After production_supervisor: run original callback, mark clips ready (sync)."""
    result = None
    if _orig_production_after:
        result = _orig_production_after(callback_context)
    mark_stage_ready("clips")
    logger.info("APPROVAL GATE: clips stage ready — waiting for human approval")
    approved = wait_for_approval("clips")
    if not approved:
        logger.error("APPROVAL GATE: timed out waiting for clips approval")
    return result


async def _production_after_with_gate(callback_context):
    """After production_supervisor: async wrapper to keep event loop free."""
    return await asyncio.to_thread(_production_after_with_gate_sync, callback_context)


def _assembly_before_with_gate_sync(callback_context):
    """Before assembler_agent: wait for clips approval, then run original (sync)."""
    if not is_stage_approved("clips"):
        logger.info("APPROVAL GATE: assembly waiting for clips approval...")
        approved = wait_for_approval("clips")
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


async def _assembly_before_with_gate(callback_context):
    """Before assembler_agent: async wrapper to keep event loop free.

    Assembly does FFmpeg rendering which can take several minutes.
    """
    return await asyncio.to_thread(_assembly_before_with_gate_sync, callback_context)


# Wire approval gates into sub-agents
scenario_director.after_agent_callback = _scenario_after_with_gate
audio_agent.before_agent_callback = _audio_before_with_gate
visual_director.after_agent_callback = _visual_after_with_gate
production_supervisor.before_agent_callback = _production_before_with_gate
production_supervisor.after_agent_callback = _production_after_with_gate
assembler_agent.before_agent_callback = _assembly_before_with_gate


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
        audio_agent,
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
    if not state.get("_workers_provisioned"):
        import threading
        from worker_provisioner import get_provisioner

        provisioner = get_provisioner()

        def _start_provisioning_bg():
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
        "ADHD-friendly documentary pipeline: scenario generation with "
        "evaluate-optimize loop, TTS narration with WhisperX alignment, "
        "iterative visual planning with LoRA selection, GPU video production, "
        "and final assembly. All phases validated by Timeline Guardian. "
        "Each stage pauses for human approval before the next one begins."
    ),
    sub_agents=[
        scenario_director,
        audio_agent,
        visual_director,
        production_supervisor,
        assembler_agent,
    ],
    before_agent_callback=_init_pipeline_state,
    after_agent_callback=_cleanup_pipeline_state,
)
