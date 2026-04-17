"""
Simulation Bridge — unifies ADK-native ``EnvironmentSimulationConfig`` with
callback-called functions that bypass ADK's tool execution pipeline.

Architecture
------------

ADK's ``EnvironmentSimulationConfig`` hooks into ``before_tool_callback`` and
only intercepts calls that flow through the ADK tool pipeline.  Many pipeline
functions (TTS, video generation, GPU provisioning) are called **directly** from
deterministic callbacks, bypassing the tool pipeline entirely.

This module provides:

1. ``ToolProxy`` — lightweight adapter satisfying the ``BaseTool.name`` contract
   so the ``EnvironmentSimulationEngine`` can match against tool names.

2. ``@simulated(tool_name)`` — decorator for callback-called functions.  Before
   executing the real function it checks the simulation engine; if an injection
   matches, the mock result is returned and the real function is skipped.

3. ``SimulationRegistry`` — singleton holding the active
   ``EnvironmentSimulationEngine`` (if any).  Set once at pipeline startup from
   a scenario config; read by every ``@simulated`` wrapper.

4. ``activate_simulation(config)`` / ``deactivate_simulation()`` — entry points
   to enable/disable simulation globally.

5. ``create_agent_callback(config)`` — convenience wrapper around
   ``EnvironmentSimulationFactory.create_callback()`` for ADK-registered tools.

The result: **one** ``EnvironmentSimulationConfig`` object defines the entire
test scenario.  It works identically for ADK tools (via ``before_tool_callback``)
and for callback-called functions (via ``@simulated``).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import threading
from functools import wraps
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ToolProxy — minimal adapter for EnvironmentSimulationEngine.simulate()
# ---------------------------------------------------------------------------

class ToolProxy:
    """Minimal stand-in for ``google.adk.tools.BaseTool``.

    The simulation engine only accesses ``tool.name`` when checking injection
    configs (``match_args`` / ``injection_probability``).  Mock strategies
    (``MOCK_STRATEGY_TOOL_SPEC``) also access ``tool._get_declaration()`` and
    ``tool.description``, so we provide stubs for those too.
    """

    def __init__(
        self,
        name: str,
        description: str = "",
        parameters: Optional[dict] = None,
    ) -> None:
        self.name = name
        self.description = description or f"Simulated tool: {name}"
        self._parameters = parameters or {}

    def _get_declaration(self):
        """Return None — injection configs don't need declarations."""
        return None


# ---------------------------------------------------------------------------
# SimulationRegistry — global singleton
# ---------------------------------------------------------------------------

class SimulationRegistry:
    """Thread-safe singleton holding the active simulation engine."""

    _instance: Optional[SimulationRegistry] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._engine = None
        self._config = None
        self._scenario_name: str = ""

    @classmethod
    def get(cls) -> SimulationRegistry:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @property
    def engine(self):
        return self._engine

    @property
    def config(self):
        return self._config

    @property
    def scenario_name(self) -> str:
        return self._scenario_name

    @property
    def active(self) -> bool:
        return self._engine is not None

    def activate(self, config, scenario_name: str = "") -> None:
        """Set the active simulation config + engine."""
        from google.adk.tools.environment_simulation.environment_simulation_engine import (
            EnvironmentSimulationEngine,
        )

        self._config = config
        self._engine = EnvironmentSimulationEngine(config)
        self._scenario_name = scenario_name
        logger.info(
            "Simulation activated: scenario=%s, tools=%s",
            scenario_name,
            [c.tool_name for c in config.tool_simulation_configs],
        )

    def deactivate(self) -> None:
        """Clear the active simulation."""
        prev = self._scenario_name
        self._engine = None
        self._config = None
        self._scenario_name = ""
        logger.info("Simulation deactivated (was: %s)", prev)


def get_simulation_engine():
    """Return the active simulation engine, or None."""
    return SimulationRegistry.get().engine


def is_simulation_active() -> bool:
    """Return True if a simulation scenario is currently active."""
    return SimulationRegistry.get().active


# ---------------------------------------------------------------------------
# Public API — activate / deactivate
# ---------------------------------------------------------------------------

def activate_simulation(config, scenario_name: str = "") -> None:
    """Activate a simulation scenario globally.

    Args:
        config: An ``EnvironmentSimulationConfig`` instance.
        scenario_name: Human-readable name for logging.
    """
    SimulationRegistry.get().activate(config, scenario_name)


def deactivate_simulation() -> None:
    """Deactivate the current simulation scenario."""
    SimulationRegistry.get().deactivate()


# ---------------------------------------------------------------------------
# @simulated decorator — for callback-called functions
# ---------------------------------------------------------------------------

def _run_async(coro):
    """Run an async coroutine from synchronous context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We're inside an async context (e.g., FastAPI endpoint).
        # Use a thread to avoid "cannot run nested event loop" errors.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result(timeout=30)
    else:
        return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Post-interception hooks — create placeholder files for simulated media tools
# ---------------------------------------------------------------------------
# The old _TEST_MODE code generated actual files on disk (silent WAVs,
# solid-color MP4s).  The @simulated decorator only returns mock JSON,
# which breaks downstream code that expects files to exist (gatekeeper
# file-existence checks, probe_clip, Timeline Guardian).
#
# These hooks run AFTER the simulation engine returns a mock response,
# creating minimal placeholder files so the rest of the pipeline works.

def _post_intercept_generate_narration(call_args: Dict, result: Any) -> None:
    """Create a silent WAV placeholder after narration simulation."""
    output_dir = call_args.get("output_dir", "") or "/tmp/documentary-pipeline/audio"
    scene_num = call_args.get("scene_num", 0)
    voice_role = call_args.get("voice_role", "v1")
    wav_path = os.path.join(output_dir, f"scene_{int(scene_num):03d}_{voice_role}.wav")

    if not os.path.exists(wav_path):
        try:
            import wave
            os.makedirs(os.path.dirname(wav_path) or ".", exist_ok=True)
            sample_rate = 24000
            duration = 5.0  # default placeholder duration
            num_frames = int(sample_rate * duration)
            with wave.open(wav_path, "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(b'\x00' * (num_frames * 2))
            logger.info("Simulation placeholder: created silent WAV %s", wav_path)
        except Exception as exc:
            logger.warning("Failed to create placeholder WAV %s: %s", wav_path, exc)


def _post_intercept_generate_video_clip(call_args: Dict, result: Any) -> None:
    """Create a solid-color MP4 placeholder after video simulation."""
    import subprocess as _sp

    output_path = call_args.get("output_path", "")
    if not output_path:
        output_dir = call_args.get("output_dir", "/tmp/documentary-pipeline/video")
        scene_num = call_args.get("scene_num", 0)
        phrase_idx = call_args.get("phrase_idx", 0)
        output_path = os.path.join(output_dir, f"scene_{int(scene_num):03d}_phrase_{int(phrase_idx):03d}.mp4")

    if not os.path.exists(output_path):
        try:
            os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
            duration = call_args.get("duration_sec", 5.0)
            cmd = [
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", f"color=c=0x336699:s=1280x720:d={float(duration):.2f}:r=24",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-t", f"{float(duration):.2f}",
                output_path,
            ]
            _sp.run(cmd, capture_output=True, text=True, timeout=60)
            logger.info("Simulation placeholder: created MP4 %s", output_path)
        except Exception as exc:
            logger.warning("Failed to create placeholder MP4 %s: %s", output_path, exc)


# Registry of post-interception hooks keyed by tool_name
_POST_INTERCEPT_HOOKS: Dict[str, Callable] = {
    "generate_narration": _post_intercept_generate_narration,
    "generate_video_clip": _post_intercept_generate_video_clip,
}


# ---------------------------------------------------------------------------
# Response patchers — fix dynamic fields in static mock responses
# ---------------------------------------------------------------------------
# ADK InjectionConfig dicts are static (defined at scenario-creation time),
# so fields like wav_path and duration always reflect the defaults passed to
# _tts_success().  These patchers override dynamic fields using the actual
# call_args so downstream code (deterministic_steps.py, gatekeeper) sees
# paths and durations that match the real call.

_SECONDS_PER_WORD = 0.45  # rough TTS estimate


def _patch_generate_narration(call_args: Dict, result: dict) -> None:
    """Patch wav_path and duration in the mock narration response."""
    output_dir = call_args.get("output_dir", "") or "/tmp/documentary-pipeline/audio"
    scene_num = int(call_args.get("scene_num", 0))
    voice_role = call_args.get("voice_role", "V1")
    text = call_args.get("text", "")

    result["wav_path"] = os.path.join(
        output_dir, f"scene_{scene_num:03d}_{voice_role}.wav"
    )
    if text:
        word_count = len(text.split())
        result["duration"] = round(word_count * _SECONDS_PER_WORD, 2)
        result["text_length"] = len(text)
        result["word_count"] = word_count


def _patch_generate_video_clip(call_args: Dict, result: dict) -> None:
    """Patch output_path and duration in the mock video response."""
    output_path = call_args.get("output_path", "")
    if output_path:
        result["output_path"] = output_path
    duration_sec = call_args.get("duration_sec", 5.0)
    result["target_duration"] = round(float(duration_sec), 2)
    result["actual_duration"] = round(float(duration_sec) * 1.15, 2)
    lora_id = call_args.get("lora_id", "")
    if lora_id:
        result["lora_id"] = lora_id


# Registry of response patchers keyed by tool_name
_RESPONSE_PATCHERS: Dict[str, Callable] = {
    "generate_narration": _patch_generate_narration,
    "generate_video_clip": _patch_generate_video_clip,
}


def simulated(tool_name: str, *, description: str = ""):
    """Decorator that checks the simulation engine before calling the real function.

    If a simulation scenario is active and the engine has an injection config
    for ``tool_name`` that matches the call arguments, the mock result is
    returned (as a JSON string, matching tool return conventions).  Otherwise
    the real function executes normally.

    When a simulation intercepts a media-generating tool, a post-interception
    hook creates placeholder files on disk so downstream code (gatekeeper
    file-existence checks, probe_clip, Timeline Guardian) works correctly.

    Usage::

        @simulated("generate_narration")
        def generate_narration(scene_num, voice_role, text, ...):
            # Real implementation — only reached when no simulation matches
            ...

    Args:
        tool_name: The tool name to match in ``EnvironmentSimulationConfig``.
        description: Optional description for the ToolProxy.
    """

    def decorator(fn: Callable) -> Callable:
        proxy = ToolProxy(name=tool_name, description=description)

        @wraps(fn)
        def wrapper(*args, **kwargs):
            engine = get_simulation_engine()
            if engine is None:
                return fn(*args, **kwargs)

            # Build a dict of the call arguments keyed by parameter name
            # so the engine can match against ``match_args``.
            sig = inspect.signature(fn)
            try:
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                call_args = dict(bound.arguments)
            except TypeError:
                call_args = kwargs.copy()

            # Remove non-serialisable args (tool_context, etc.)
            for key in list(call_args):
                if key == "tool_context" or not _is_json_serialisable(call_args[key]):
                    del call_args[key]

            try:
                result = _run_async(engine.simulate(proxy, call_args, None))
            except Exception as exc:
                logger.warning(
                    "Simulation engine error for %s: %s — falling through to real call",
                    tool_name,
                    exc,
                )
                return fn(*args, **kwargs)

            if result is not None:
                logger.info(
                    "Simulation intercepted %s: %s",
                    tool_name,
                    _truncate(str(result), 200),
                )

                # Patch dynamic fields in the static mock response
                if isinstance(result, dict):
                    patcher = _RESPONSE_PATCHERS.get(tool_name)
                    if patcher:
                        try:
                            patcher(call_args, result)
                        except Exception as patch_exc:
                            logger.warning(
                                "Response patcher failed for %s: %s",
                                tool_name, patch_exc,
                            )

                # Run post-interception hook to create placeholder files
                hook = _POST_INTERCEPT_HOOKS.get(tool_name)
                if hook:
                    try:
                        hook(call_args, result)
                    except Exception as hook_exc:
                        logger.warning(
                            "Post-intercept hook failed for %s: %s",
                            tool_name, hook_exc,
                        )

                # Tool functions return JSON strings — match that convention
                if isinstance(result, dict):
                    return json.dumps(result)
                return result

            # No injection matched — run the real function
            return fn(*args, **kwargs)

        # Mark the wrapper so we can identify simulated functions
        wrapper._simulated_tool_name = tool_name
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# ADK agent callback helper
# ---------------------------------------------------------------------------

def create_agent_callback(config=None):
    """Create an ADK ``before_tool_callback`` from the active or given config.

    For ADK-registered tools (FunctionTool), attach this to the agent::

        agent = LlmAgent(
            ...,
            before_tool_callback=create_agent_callback()
        )

    Args:
        config: Optional explicit config.  If None, uses the active registry.

    Returns:
        An async callback suitable for ``before_tool_callback``, or None if
        no simulation is active.
    """
    cfg = config or SimulationRegistry.get().config
    if cfg is None:
        return None

    from google.adk.tools.environment_simulation import (
        EnvironmentSimulationFactory,
    )

    return EnvironmentSimulationFactory.create_callback(cfg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_json_serialisable(value: Any) -> bool:
    """Check if a value can be JSON-serialised (for match_args)."""
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError, OverflowError):
        return False


def _truncate(s: str, max_len: int) -> str:
    return s[:max_len] + "…" if len(s) > max_len else s
