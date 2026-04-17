"""
Audio Agent -- TTS generation + WhisperX alignment + WhisperX oracle.

Generates TTS for all scenes (3 voices per scene) using Qwen3-TTS,
then runs WhisperX alignment on generated audio.
Writes alignment data to state["whisperx_alignment"].

NOTE: The actual work is done deterministically in the before_agent_callback
(deterministic_audio_callback) to avoid unreliable LLM tool-calling.
The LLM agent is skipped entirely — the callback returns Content directly.

After the deterministic work completes, two after_agent_callbacks run
in order:

1. ``whisperx_oracle_callback``  — processes per-clip WhisperX alignments
   into a :class:`tools.otio_moments.WhisperXOracle`, records ground-truth
   durations, and fires a reflection event (supervisor_escalate from W3)
   if the projected total falls below 80 % of the user-requested runtime
   (#86).
2. ``timeline_guardian_callback`` — the existing stage-boundary OTIO
   compliance check (#84).  Keeps audio stage in its original contract.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.genai import types as genai_types

from agents.model_config import build_model
from callbacks.deterministic_steps import deterministic_audio_callback
from callbacks.timeline_guardian import timeline_guardian_callback

logger = logging.getLogger(__name__)

_INSTRUCTION = """\
You are the Audio Agent for a documentary pipeline.
Audio generation is handled automatically. Report completion.
"""


def whisperx_oracle_callback(
    callback_context: CallbackContext,
) -> Optional[genai_types.Content]:
    """Reconcile per-clip WhisperX alignments into a running duration projection.

    Reads the alignment data written by ``deterministic_audio_callback``
    into ``state["whisperx_alignment"]`` and the scenes from
    ``state["scenes"]``.  Builds a :class:`WhisperXOracle`, records each
    measured clip, and fires a reflection event if the projected total
    falls below 80 % of ``target_total_sec`` (defaults to the sum of
    scene ``duration_sec`` values).

    The callback NEVER raises — it's an advisory check whose job is to
    escalate to the production supervisor (W3) so the supervisor's LLM
    can decide whether to add scenes, lengthen scenes, or accept the
    runtime.  The stage-boundary OTIO guardian runs separately and IS
    allowed to raise.
    """
    try:
        from tools.otio_moments import WhisperXOracle, fire_reflection_event
    except Exception as e:  # noqa: BLE001
        logger.warning("WhisperX oracle unavailable (import failed: %s)", e)
        return None

    state = callback_context.state

    # Only run this reconciliation on the audio stage; visual_direction
    # and later stages should leave the oracle alone.
    phase = state.get("pipeline_phase", "")
    if phase and phase != "audio":
        return None

    try:
        scenes_raw = state.get("scenes", "[]")
        scenes = scenes_raw if isinstance(scenes_raw, list) else json.loads(str(scenes_raw))
    except Exception as e:  # noqa: BLE001
        logger.warning("WhisperX oracle: can't parse scenes: %s", e)
        return None
    if not isinstance(scenes, list) or not scenes:
        logger.info("WhisperX oracle: no scenes in state, skipping projection")
        return None

    try:
        alignment_raw = state.get("whisperx_alignment", "{}")
        alignment = (
            alignment_raw
            if isinstance(alignment_raw, dict)
            else json.loads(str(alignment_raw))
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("WhisperX oracle: can't parse alignment: %s", e)
        return None

    target_total = sum(float(s.get("duration_sec", 0) or 0) for s in scenes)
    if target_total <= 0:
        logger.info("WhisperX oracle: target_total <= 0, skipping")
        return None

    oracle = WhisperXOracle(target_total_sec=target_total)
    oracle.register_scenes(scenes)

    # Walk alignment keys: each is "scene_NNN_VOICE" with total_duration.
    for key, data in (alignment or {}).items():
        if not isinstance(key, str) or not key.startswith("scene_"):
            continue
        if not isinstance(data, dict):
            continue
        try:
            _, scene_part, *voice_parts = key.split("_", 2)
            # scene_NNN_VOICE  -> scene_part=NNN, voice_parts=["VOICE"]
            scene_num = int(scene_part)
            voice = voice_parts[0] if voice_parts else "V1"
        except (ValueError, IndexError):
            continue

        measured = float(data.get("total_duration") or 0.0)
        if measured <= 0:
            logger.warning(
                "WhisperX oracle: clip %s has measured_duration=0; "
                "ignoring (fail-loud rule applies in whisperx_tools)",
                key,
            )
            continue

        scene = next(
            (s for s in scenes if int(s.get("scene_num", 0) or 0) == scene_num),
            None,
        )
        voices = (
            [v for v in (scene.get("voices") or []) if (v.get("text") or "").strip()]
            if scene
            else []
        )
        num_voices = max(1, len(voices))
        claimed = float(scene.get("duration_sec", 0) or 0) / num_voices if scene else 0.0

        oracle.record(
            scene_num=scene_num,
            voice=voice,
            claimed_sec=claimed,
            measured_sec=measured,
            wav_path=data.get("wav_path", ""),
        )

    # Persist a serialisable snapshot of the oracle's observations so
    # downstream stages (and the dashboard) can read it.
    state["_whisperx_oracle"] = {
        "target_total_sec": oracle.target_total_sec,
        "measured_total_sec": round(oracle.measured_total(), 3),
        "projected_total_sec": round(oracle.project_total(), 3),
        "completed_scenes": sorted(oracle.completed_scene_nums()),
        "clips": [
            {
                "scene_num": c.scene_num,
                "voice": c.voice,
                "claimed_sec": round(c.claimed_sec, 3),
                "measured_sec": round(c.measured_sec, 3),
                "ratio": round(c.ratio, 3),
            }
            for c in oracle.clips
        ],
    }

    alarm = oracle.check_projection()
    if alarm:
        # Context string matches the format the user requested:
        # 'projected total 194s vs target 420s = 46%; need more scenes or longer ones'
        fire_reflection_event(state=state, context=alarm)
    else:
        logger.info(
            "WhisperX oracle OK: projected=%.0fs target=%.0fs (%.0f%%)",
            oracle.project_total(),
            oracle.target_total_sec,
            100 * oracle.project_total() / oracle.target_total_sec,
        )

    return None


def _chained_after_agent_callback(
    callback_context: CallbackContext,
) -> Optional[genai_types.Content]:
    """Run the oracle THEN the timeline guardian.

    Keeps the original contract that ``timeline_guardian_callback`` is
    the last thing to run so its RuntimeError (on OTIO violation) is the
    authoritative stage verdict.  The oracle only fires escalations — it
    does not raise.

    The oracle call is wrapped in a best-effort try/except so that any
    unexpected error (e.g. malformed scenes list, alignment dict shape
    drift, supervisor import path changes) can NEVER short-circuit the
    guardian.  An OTIO violation must be caught even if the oracle is
    buggy — the guardian is the authoritative gate.
    """
    try:
        whisperx_oracle_callback(callback_context)
    except Exception as e:  # noqa: BLE001 — advisory check must not block guardian
        logger.error(
            "whisperx_oracle_callback raised (should never happen); "
            "continuing to timeline_guardian_callback: %r",
            e,
        )
    return timeline_guardian_callback(callback_context)


audio_agent = Agent(
    name="audio_agent",
    model=build_model(),
    instruction=_INSTRUCTION,
    tools=[],
    before_agent_callback=deterministic_audio_callback,
    after_agent_callback=_chained_after_agent_callback,
)
