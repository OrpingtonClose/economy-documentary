"""Visual A2A Agent — conversational Strands agent for visual concepts.

Receives a "produce_visual_concepts" message, runs the LLM council
(propose -> evaluate -> refine) for each scene, and writes visual
concepts to otio-agent via A2A messages.
"""

from __future__ import annotations
from strands_agents.config import DEFAULT_FAST_MODEL, DEFAULT_MODEL

import json
import logging
from typing import Any

from strands_agents.shared_a2a.llm import completion
from strands import Agent
from ..shared_a2a.direct_dispatch_model import DirectDispatchModel
from strands import tool as strands_tool

from ..shared_a2a.sync_otio_client import SyncOtioClient
from tools.technique_tools import query_techniques, get_technique_details, count_words, estimate_speaking_duration

logger = logging.getLogger(__name__)

_MAX_CONCEPT_ITERATIONS = 5

SHOT_TYPES = [
    "establishing_wide", "wide_shot", "full_shot", "medium_shot",
    "medium_close_up", "close_up", "extreme_close_up", "over_the_shoulder",
    "two_shot", "aerial", "low_angle", "high_angle", "dutch_angle",
    "point_of_view", "tracking", "insert",
]

CAMERA_MOVEMENTS = [
    "static", "slow_pan_left", "slow_pan_right", "slow_tilt_up",
    "slow_tilt_down", "slow_zoom_in", "slow_zoom_out", "slow_dolly_in",
    "slow_dolly_out", "slow_tracking", "slow_crane_up", "slow_crane_down",
    "handheld",
]

_PASS_RATINGS = {"EXCELLENT", "GOOD"}


def build_visual_agent(
    otio_agent_url: str = "http://localhost:9001",
    model_id: str = DEFAULT_MODEL,
) -> Agent:
    """Build a conversational Strands agent for visual concept production.

    The agent has tools for producing visual concepts and evaluating
    visual coherence. It interprets incoming messages, calls the right
    tool, and returns the result.
    """
    otio = SyncOtioClient(otio_agent_url=otio_agent_url)
    _model_id = model_id

    @strands_tool
    def check_environment() -> str:
        """Check this agent's full environment and explain readiness."""
        from ..shared_a2a.env_check import build_env_check
        return build_env_check(
            "visual-agent",
            otio_url=otio_agent_url,
            needs_llm=True,
            needs_ffmpeg=False,
            needs_vast=False,
            needs_worker=False,
            upstream_deps={"scenario": "scenes", "timing": "timing"},
        )

    @strands_tool
    def produce_visual_concepts() -> str:
        """Generate visual concepts for all scenes via LLM council.

        Gets scene context from otio-agent, runs the LLM council
        (propose -> evaluate -> refine) per scene, and writes
        visual concepts to otio-agent.
        """
        ctx = otio.get_context("visual")
        scenes = ctx.get("state", ctx).get("scenes", [])
        style_lock = ctx.get("state", ctx).get("style_lock", {})
        visual_style = ctx.get("state", ctx).get("visual_style", {})

        if not scenes:
            return json.dumps({
                "status": "skipped",
                "reason": "no scenes available",
                "visual_concepts": [],
            })

        # LLM council: overall visual direction review
        direction = _llm_council_direction(
            scenes, style_lock, visual_style, _model_id,
        )

        visual_concepts = []
        for scene in scenes:
            scene_id = scene.get("scene_id") or f"scene_{scene.get('scene_num', 0)}"
            narration = scene.get("narration", scene.get("text", ""))

            concept = _concept_loop(
                scene_id, narration, style_lock, visual_style, direction, _model_id,
            )
            visual_concepts.append(concept)

        # Write to otio-agent
        otio.set_metadata(
            "visual_concepts", visual_concepts,
            "visual stage complete",
            stage="visual", agent_id="visual-agent",
        )

        return json.dumps({
            "status": "completed",
            "visual_concepts": visual_concepts,
            "scene_count": len(scenes),
            "direction": direction,
        })

    @strands_tool
    def evaluate_visual_coherence() -> str:
        """Evaluate visual coherence of proposed concepts."""
        ctx = otio.get_context("visual")
        visual_concepts = ctx.get("state", ctx).get("visual_concepts", [])

        if not visual_concepts:
            raise RuntimeError(
                "No visual concepts found in otio-agent context. "
                "The scenario stage must produce visual concepts before visual evaluation."
            )

        # Evaluate coherence across all concepts
        coherence = _evaluate_cross_scene_coherence(visual_concepts, _model_id)

        return json.dumps({
            "status": "completed",
            "coherence": coherence,
            "concept_count": len(visual_concepts),
        })

    @strands_tool
    def write_ladder_state(level: int, attempts: int, history_json: str = "[]") -> str:
        """Write visual ladder state to OTIO for tracking."""
        result = otio.call("set_metadata", {
            "key": "visual_ladder",
            "value": {
                "level": level,
                "attempts": attempts,
                "history": json.loads(history_json) if history_json else [],
            },
            "reason": "visual ladder state update",
            "stage": "visual",
            "agent_id": "visual-agent",
        })
        return json.dumps(result)

    @strands_tool
    def write_visual_style(style_json: str, reason: str = "") -> str:
        """Write visual style to OTIO metadata."""
        result = otio.call("set_metadata", {
            "key": "visual_style",
            "value": json.loads(style_json),
            "reason": reason or "visual style update",
            "stage": "visual",
            "agent_id": "visual-agent",
        })
        return json.dumps(result)

    @strands_tool
    def write_style_lock(lock_json: str, reason: str = "") -> str:
        """Write style lock to OTIO metadata."""
        result = otio.call("set_metadata", {
            "key": "style_lock",
            "value": json.loads(lock_json),
            "reason": reason or "style lock update",
            "stage": "visual",
            "agent_id": "visual-agent",
        })
        return json.dumps(result)

    # -- Build the agent --

    agent = Agent(
        model=DirectDispatchModel(),
        name="visual-agent",
        description=(
            "Visual concept proposal and coherence evaluation for the documentary pipeline. "
            "Uses LLM council to propose, evaluate, and refine visual concepts per scene."
        ),
        tools=[
            produce_visual_concepts, evaluate_visual_coherence, check_environment,
            write_ladder_state, write_visual_style, write_style_lock,
        ],
        system_prompt=(
            "You are the visual-agent for a documentary pipeline. "
            "You produce visual concepts for scenes and evaluate their coherence.\n\n"
            "When you receive a message:\n"
            "1. Determine what the caller wants (produce concepts or evaluate coherence)\n"
            "2. Call the appropriate tool\n"
            "3. Return the result to the caller\n\n"
            "Available operations: produce_visual_concepts, evaluate_visual_coherence, "
        "write_ladder_state, write_visual_style, write_style_lock\n\n"
            "The caller may send JSON with 'operation' and 'params' keys, "
            "or they may describe what they want in natural language. "
            "Handle both gracefully."
        ),
    )

    # Attach internals for testing
    agent._otio_url = otio_agent_url
    agent._model_id = _model_id

    return agent


# ---------------------------------------------------------------------------
# LLM council helpers (module-level, not tools)
# ---------------------------------------------------------------------------

def _concept_loop(
    scene_id: str,
    narration: str,
    style_lock: dict[str, Any],
    visual_style: dict[str, Any],
    direction: dict[str, Any],
    model_id: str,
) -> dict[str, Any]:
    """Run propose -> evaluate -> refine loop for one scene."""
    concept = None
    rating = "POOR"

    for iteration in range(_MAX_CONCEPT_ITERATIONS):
        # 1. Propose
        if concept is None:
            concept = _llm_propose(
                scene_id, narration, style_lock, visual_style, direction, model_id,
            )
        else:
            # Refine based on previous evaluation
            concept = _llm_refine(
                scene_id, narration, concept, rating, style_lock, model_id,
            )

        # 2. Evaluate
        rating = _llm_evaluate(
            scene_id, narration, concept, style_lock, model_id,
        )

        if rating in _PASS_RATINGS:
            break

        logger.info(
            "Scene %s concept iteration %d: rating=%s",
            scene_id, iteration + 1, rating,
        )

    concept["rating"] = rating
    concept["scene_id"] = scene_id
    return concept


def _llm_council_direction(
    scenes: list[dict[str, Any]],
    style_lock: dict[str, Any],
    visual_style: dict[str, Any],
    model_id: str,
) -> dict[str, Any]:
    """LLM council: establish overall visual direction."""
    prompt = f"""You are a documentary visual director. Review the scenes and visual style, then establish the overall visual direction.

Scenes: {json.dumps(scenes, indent=2)}
Style lock: {json.dumps(style_lock, indent=2)}
Visual style: {json.dumps(visual_style, indent=2)}

Return JSON with:
- "overall_mood": string describing the dominant mood
- "palette": list of 3-5 color names
- "shot_rhythm": "slow"|"medium"|"varied"
- "consistency_rules": list of rules to maintain visual consistency
- "priority_shots": list of {{"scene_id": str, "shot_type": str}}"""

    try:
        response = completion(
            model=model_id,
            messages=[{"role": "user", "content": prompt}],
            
        )
        return json.loads(response.choices[0].message.content)
    except Exception as exc:
        raise RuntimeError(f"LLM council direction failed: {exc}")


def _llm_propose(
    scene_id: str,
    narration: str,
    style_lock: dict[str, Any],
    visual_style: dict[str, Any],
    direction: dict[str, Any],
    model_id: str,
) -> dict[str, Any]:
    """LLM council: propose a visual concept for a scene."""
    prompt = f"""You are a documentary visual director. Propose a visual concept for this scene.

Scene ID: {scene_id}
Narration: {narration[:500]}
Style lock: {json.dumps(style_lock, indent=2)}
Visual direction: {json.dumps(direction, indent=2)}

Available shot types: {SHOT_TYPES}
Available camera movements: {CAMERA_MOVEMENTS}

Return JSON with:
- "shot_type": one of {SHOT_TYPES}
- "camera_movement": one of {CAMERA_MOVEMENTS}
- "prompt": detailed LTX-2.3 video generation prompt (2-3 sentences)
- "negative_prompt": what to avoid
- "mood": mood description
- "palette": list of 2-3 colors
- "phrases": list of 2-3 key visual phrases from the narration"""

    try:
        response = completion(
            model=model_id,
            messages=[{"role": "user", "content": prompt}],
            
        )
        return json.loads(response.choices[0].message.content)
    except Exception as exc:
        raise RuntimeError(f"LLM propose failed for {scene_id}: {exc}")


def _llm_evaluate(
    scene_id: str,
    narration: str,
    concept: dict[str, Any],
    style_lock: dict[str, Any],
    model_id: str,
) -> str:
    """LLM council: evaluate a visual concept. Returns rating string."""
    prompt = f"""You are a visual coherence evaluator. Score this concept.

Scene: {scene_id}
Narration: {narration[:300]}
Concept: {json.dumps(concept, indent=2)}
Style lock: {json.dumps(style_lock, indent=2)}

Rate on: style consistency, shot variety, phrase coverage, mood alignment.
Return JSON: {{"rating": "EXCELLENT"|"GOOD"|"FAIR"|"POOR", "issues": [...]}}"""

    try:
        response = completion(
            model=model_id,
            messages=[{"role": "user", "content": prompt}],
            
        )
        result = json.loads(response.choices[0].message.content)
        return result.get("rating", "FAIR")
    except Exception as exc:
        raise RuntimeError(f"LLM evaluate failed for {scene_id}: {exc}")


def _llm_refine(
    scene_id: str,
    narration: str,
    concept: dict[str, Any],
    rating: str,
    style_lock: dict[str, Any],
    model_id: str,
) -> dict[str, Any]:
    """LLM council: refine a visual concept based on evaluation feedback."""
    prompt = f"""You are a visual concept refiner. The concept received a {rating} rating.

Scene: {scene_id}
Narration: {narration[:300]}
Current concept: {json.dumps(concept, indent=2)}
Style lock: {json.dumps(style_lock, indent=2)}

Improve the concept to achieve GOOD or EXCELLENT rating.
Return JSON with the same structure: shot_type, camera_movement, prompt, negative_prompt, mood, palette, phrases"""

    try:
        response = completion(
            model=model_id,
            messages=[{"role": "user", "content": prompt}],
            
        )
        return json.loads(response.choices[0].message.content)
    except Exception as exc:
        raise RuntimeError(f"LLM refine failed for {scene_id}: {exc}")


def _evaluate_cross_scene_coherence(
    visual_concepts: list[dict[str, Any]],
    model_id: str,
) -> dict[str, Any]:
    """Evaluate visual coherence across all scenes."""
    prompt = f"""You are a visual coherence evaluator. Review all visual concepts for cross-scene consistency.

Visual concepts: {json.dumps(visual_concepts, indent=2)}

Evaluate: palette consistency, shot variety, mood continuity, style lock adherence.
Return JSON: {{"coherence_rating": "EXCELLENT"|"GOOD"|"FAIR"|"POOR", "issues": [...], "suggestions": [...]}}"""

    try:
        response = completion(
            model=model_id,
            messages=[{"role": "user", "content": prompt}],
            
        )
        return json.loads(response.choices[0].message.content)
    except Exception as exc:
        raise RuntimeError(f"Cross-scene coherence evaluation failed: {exc}")