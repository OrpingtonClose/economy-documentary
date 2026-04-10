"""
Master pipeline -- SequentialAgent assembly for the documentary pipeline.

Architecture::

    SequentialAgent("documentary_pipeline")
    ├── LoopAgent("scenario_director")        # script generation + ADHD eval
    ├── Agent("audio_agent")                  # TTS + WhisperX alignment
    ├── LoopAgent("visual_director")          # visual planning loop
    │   ├── Agent("content_analyst")
    │   ├── Agent("visual_concepter")
    │   └── Agent("coherence_evaluator")
    ├── Agent("production_supervisor")        # GPU video generation
    └── Agent("assembler_agent")              # final assembly

Data flows via session state (blackboard pattern):
  - scenario_director -> state["scenes"]
  - audio_agent -> state["whisperx_alignment"]
  - visual_director -> state["content_analysis"], state["visual_concepts"]
  - production_supervisor -> OTIO timeline clips
  - assembler_agent -> final documentary output
"""

from __future__ import annotations

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
from callbacks.state_manager import build_pipeline_state
from tools.otio_tools import _timeline_path

logger = logging.getLogger(__name__)


def _init_pipeline_state(
    callback_context: CallbackContext,
) -> Optional[genai_types.Content]:
    """Ensure session state has pipeline keys before the pipeline starts.

    AG-UI creates sessions without initial state, so the first time the
    pipeline runs we inject the keys that all agents read/write.
    """
    state = callback_context.state
    if "_pipeline_key" not in state:
        for k, v in build_pipeline_state().items():
            state[k] = v
        logger.info(
            "Pipeline state initialised: pipeline_key=%s",
            state["_pipeline_key"],
        )

    # Pre-compute and store timeline path so all sub-agents can find it.
    # The scenario director also sets this via create_timeline(), but that
    # write may not propagate out of the LoopAgent scope.
    topic = state.get("topic", "")
    if topic and not state.get("_timeline_path"):
        state["_timeline_path"] = _timeline_path(topic)
        logger.info("Pre-set _timeline_path=%s", state["_timeline_path"])

    return None


def _cleanup_pipeline_state(
    callback_context: CallbackContext,
) -> Optional[genai_types.Content]:
    """Cleanup after pipeline completes."""
    state = callback_context.state
    state["pipeline_phase"] = "completed"
    logger.info(
        "Pipeline completed: pipeline_key=%s",
        state.get("_pipeline_key", "unknown"),
    )
    return None


pipeline_agent = SequentialAgent(
    name="documentary_pipeline",
    description=(
        "ADHD-friendly documentary pipeline: scenario generation with "
        "evaluate-optimize loop, TTS narration with WhisperX alignment, "
        "iterative visual planning with LoRA selection, GPU video production, "
        "and final assembly. All phases validated by Timeline Guardian."
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
