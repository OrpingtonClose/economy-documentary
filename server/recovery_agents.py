"""
LLM-powered recovery agents — cross-cutting concern for intelligent error handling.

Every level of the escalation ladder has an agent that can diagnose and fix
problems using LLMs (via API) and tools (MCPs, pipeline functions).  This is
NOT retry-with-backoff — each agent understands its domain and can ALTER the
inputs to make the operation succeed.

Architecture:
    Level 0  DOMAIN FIX     Specialist agent for the specific operation
    Level 1  RETRY          Intelligent retry — adjusts params based on error analysis
    Level 2  CREATIVE       Brainstorms alternative strategies (different model, approach)
    Level 3  COLLABORATIVE  Talks to OTHER pipeline agents to coordinate a fix
    Level 4  HUMAN          Last resort — presents full diagnostic chain

Every agent gets:
    - LLM access (via litellm)
    - Tool access (callable functions registered per agent)
    - Full diagnostic context (error, previous attempts, pipeline state)

Usage::

    policy = RecoveryPolicy(
        agents={
            RecoveryLevel.FIX: AudioTimingAgent(),
            RecoveryLevel.RETRY: RetryAgent(),
            RecoveryLevel.CREATIVE: CreativeAgent(),
            RecoveryLevel.COLLABORATIVE: CollaborativeAgent(),
        },
        level_budgets={0: 5, 1: 3, 2: 2, 3: 1},
    )
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# LLM model for recovery agents (fast + cheap for quick decisions)
# Honour RECOVERY_MODEL env var if set, otherwise use the same model
# as the main pipeline (ADK_MODEL) or fall back to a fast default.
import os as _os
_RECOVERY_MODEL = (
    _os.environ.get("RECOVERY_MODEL", "")
    or _os.environ.get("ADK_MODEL", "").removeprefix("litellm/")
    or "openrouter/google/gemini-2.5-flash"
)


# ---------------------------------------------------------------------------
# Recovery decision — what the agent decided to do
# ---------------------------------------------------------------------------

@dataclass
class RecoveryDecision:
    """What a recovery agent decided after analysing the failure."""

    action: str
    """One of: "fix", "retry", "skip", "escalate", "abort".

    - fix:      Apply state_patches and re-run the operation.
    - retry:    Re-run without changes (agent believes transient).
    - skip:     Accept the failure and continue the pipeline.
    - escalate: Pass to the next level (agent can't fix it).
    - abort:    Stop the pipeline.
    """

    state_patches: dict = field(default_factory=dict)
    """Modifications to apply to pipeline state or operation kwargs
    before re-running.  Only meaningful when action="fix"."""

    explanation: str = ""
    """Human-readable explanation of why this decision was made."""

    tool_results: list[dict] = field(default_factory=list)
    """Record of tool calls made during the decision."""

    confidence: float = 0.0
    """Agent's confidence in the fix (0.0–1.0)."""


# ---------------------------------------------------------------------------
# Tool registration — tools that agents can call
# ---------------------------------------------------------------------------

@dataclass
class AgentTool:
    """A tool available to a recovery agent."""

    name: str
    description: str
    parameters: dict  # JSON Schema for parameters
    fn: Callable[..., Any]  # The actual callable

    def to_schema(self) -> dict:
        """Convert to OpenAI function-calling schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ---------------------------------------------------------------------------
# RecoveryAgent base class
# ---------------------------------------------------------------------------

class RecoveryAgent:
    """Base class for LLM-powered recovery agents.

    Each agent has a system instruction, a set of tools, and access to
    an LLM via litellm.  The ``decide()`` method is the main entry point:
    it receives diagnostic context and returns a ``RecoveryDecision``.
    """

    def __init__(
        self,
        name: str,
        instruction: str,
        tools: Optional[list[AgentTool]] = None,
        model: str = _RECOVERY_MODEL,
        max_tool_rounds: int = 3,
    ):
        self.name = name
        self.instruction = instruction
        self.tools = tools or []
        self.model = model
        self.max_tool_rounds = max_tool_rounds

    def decide(self, context: RecoveryContext) -> RecoveryDecision:
        """Analyse the failure and decide what to do.

        Calls the LLM with the diagnostic context and available tools.
        The LLM can make tool calls (executed locally), then returns a
        final JSON decision.
        """
        import litellm

        messages = self._build_messages(context)
        tool_schemas = [t.to_schema() for t in self.tools] or None
        tool_results: list[dict] = []

        for _round in range(self.max_tool_rounds + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.3,
                    "max_tokens": 4096,
                }
                if tool_schemas:
                    kwargs["tools"] = tool_schemas

                response = litellm.completion(**kwargs)
                choice = response.choices[0]
                msg = choice.message

                # If the LLM made tool calls, execute them and loop
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    messages.append(msg.model_dump())
                    for tc in msg.tool_calls:
                        fn_name = tc.function.name
                        fn_args = json.loads(tc.function.arguments)
                        result = self._execute_tool(fn_name, fn_args)
                        tool_results.append({
                            "tool": fn_name,
                            "args": fn_args,
                            "result": str(result)[:2000],
                        })
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result, default=str)[:4000],
                        })
                    continue  # Loop back for more reasoning

                # No tool calls — parse the final response as a decision
                content = msg.content or ""
                return self._parse_decision(content, tool_results, context=context)

            except Exception as e:
                logger.error(
                    "RecoveryAgent '%s' LLM call failed (round %d): %s",
                    self.name, _round, str(e)[:300],
                )
                # Previously this silently returned "escalate", which was the
                # L1 fall-through that caused the PAG round-robin regression
                # (#76, #77).  Consult the production supervisor instead so
                # at least one LLM-backed canonical decision is made.
                return _supervisor_fallback_decision(
                    context=context,
                    reason=f"Recovery agent LLM call failed: {e}",
                    tool_results=tool_results,
                )

        # Ran out of tool rounds — same story: don't pass the buck, ask
        # the supervisor for a canonical action (#61 closes this gap).
        return _supervisor_fallback_decision(
            context=context,
            reason=(
                "Recovery agent exhausted tool call rounds without reaching "
                "a decision"
            ),
            tool_results=tool_results,
        )

    def _build_messages(self, context: RecoveryContext) -> list[dict]:
        """Build the LLM message chain with diagnostic context."""
        system_msg = (
            f"{self.instruction}\n\n"
            f"You are a recovery agent at level {context.current_level} "
            f"({context.level_name}) in the escalation ladder.\n\n"
            "RESPONSE FORMAT: After using any tools, respond with a JSON object:\n"
            "{\n"
            '  "action": "fix" | "retry" | "skip" | "escalate" | "abort",\n'
            '  "state_patches": { ... },  // only for action="fix"\n'
            '  "explanation": "why this decision",\n'
            '  "confidence": 0.0-1.0\n'
            "}\n\n"
            "IMPORTANT:\n"
            '- "fix" means you have a concrete fix (in state_patches) and the operation should retry with it\n'
            '- "escalate" means you cannot fix it and the next level should try\n'
            '- "skip" means accept the degraded output and continue the pipeline\n'
            '- "abort" means the pipeline should stop\n'
            '- "retry" means retry the same operation without changes (transient error)\n'
        )

        # Build diagnostic context
        diagnostic = {
            "operation_name": context.operation_name,
            "error_message": context.error_msg[:2000],
            "attempt_number": context.attempt_num,
            "max_attempts": context.max_attempts,
            "previous_attempts": [
                {
                    "level": a.get("level", ""),
                    "strategy": a.get("strategy", ""),
                    "error": a.get("error", "")[:500],
                }
                for a in context.previous_attempts[-5:]  # last 5 attempts
            ],
        }

        # Add operation-specific context
        if context.operation_kwargs:
            # Filter to keys that are safe to show (no binary data)
            safe_kwargs = {}
            for k, v in context.operation_kwargs.items():
                if isinstance(v, (str, int, float, bool, list, dict)):
                    safe_kwargs[k] = str(v)[:1000] if isinstance(v, str) else v
            diagnostic["operation_kwargs"] = safe_kwargs

        if context.pipeline_state:
            diagnostic["pipeline_state_keys"] = list(context.pipeline_state.keys())

        if context.diagnostic_data:
            diagnostic["diagnostic_data"] = context.diagnostic_data

        user_msg = (
            f"A pipeline operation failed and needs recovery.\n\n"
            f"DIAGNOSTIC CONTEXT:\n```json\n{json.dumps(diagnostic, indent=2, default=str)[:6000]}\n```\n\n"
            f"Analyse the problem and decide what to do. Use your tools if needed."
        )

        return [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]

    def _execute_tool(self, name: str, args: dict) -> Any:
        """Execute a registered tool by name."""
        for tool in self.tools:
            if tool.name == name:
                try:
                    result = tool.fn(**args)
                    logger.info(
                        "RecoveryAgent '%s' tool '%s' returned: %s",
                        self.name, name, str(result)[:200],
                    )
                    return result
                except Exception as e:
                    logger.error(
                        "RecoveryAgent '%s' tool '%s' failed: %s",
                        self.name, name, str(e)[:200],
                    )
                    return {"error": str(e)[:500]}
        return {"error": f"Unknown tool: {name}"}

    def _parse_decision(
        self,
        content: str,
        tool_results: list[dict],
        context: Optional["RecoveryContext"] = None,
    ) -> RecoveryDecision:
        """Parse the LLM response into a RecoveryDecision."""
        # Try to extract JSON from the response
        try:
            # Look for JSON block in markdown
            if "```json" in content:
                json_str = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                json_str = content.split("```")[1].split("```")[0].strip()
            elif content.strip().startswith("{"):
                json_str = content.strip()
            else:
                # No JSON found — route to the supervisor instead of
                # passing the buck up the ladder (#61, #73 round-robin fix).
                return _supervisor_fallback_decision(
                    context=context,
                    reason=f"Agent response (no JSON): {content[:500]}",
                    tool_results=tool_results,
                )

            data = json.loads(json_str)
            return RecoveryDecision(
                action=data.get("action", "escalate"),
                state_patches=data.get("state_patches", {}),
                explanation=data.get("explanation", ""),
                tool_results=tool_results,
                confidence=float(data.get("confidence", 0.5)),
            )
        except (json.JSONDecodeError, IndexError, ValueError) as e:
            logger.warning(
                "RecoveryAgent '%s' response parse failed: %s. Raw: %s",
                self.name, e, content[:300],
            )
            # Previously hardcoded "escalate" which was the round-robin
            # fall-through (#61, #73).  Ask the supervisor instead.
            return _supervisor_fallback_decision(
                context=context,
                reason=f"Failed to parse agent response: {content[:500]}",
                tool_results=tool_results,
            )


# ---------------------------------------------------------------------------
# Recovery context — passed to agents at each level
# ---------------------------------------------------------------------------

@dataclass
class RecoveryContext:
    """Full diagnostic context passed to recovery agents."""

    operation_name: str
    error_msg: str
    current_level: int
    level_name: str
    attempt_num: int
    max_attempts: int
    previous_attempts: list[dict] = field(default_factory=list)
    operation_kwargs: dict = field(default_factory=dict)
    pipeline_state: dict = field(default_factory=dict)
    diagnostic_data: dict = field(default_factory=dict)
    """Domain-specific diagnostic data (e.g. timing analysis, QA results)."""


# ═══════════════════════════════════════════════════════════════════════════
# LEVEL 0: Domain-specific fix agents
# ═══════════════════════════════════════════════════════════════════════════

# -- Tools for AudioTimingAgent ------------------------------------------------

def _tool_analyse_timing(scenes_json: str) -> dict:
    """Analyse narration timing per scene.  Returns which scenes are
    over/under budget and by how much.

    Args:
        scenes_json: JSON array of scenes with duration_sec and voices.
    """
    import json as _json
    try:
        scenes = _json.loads(scenes_json)
    except _json.JSONDecodeError:
        return {"error": "Invalid JSON"}

    analysis = {"scenes": [], "total_target": 0, "total_actual": 0}
    for s in scenes:
        sn = s.get("scene_num", 0)
        budget = s.get("duration_sec", 0)
        voices = s.get("voices", [])
        # Estimate actual duration from text length (rough: 150 words/min)
        total_words = sum(len(v.get("text", "").split()) for v in voices if v.get("text", "").strip())
        est_duration = (total_words / 150) * 60  # seconds
        drift_pct = ((est_duration - budget) / budget * 100) if budget else 0
        analysis["scenes"].append({
            "scene_num": sn,
            "budget_sec": budget,
            "estimated_sec": round(est_duration, 1),
            "drift_pct": round(drift_pct, 1),
            "word_count": total_words,
            "status": "short" if drift_pct < -10 else ("long" if drift_pct > 10 else "ok"),
        })
        analysis["total_target"] += budget
        analysis["total_actual"] += est_duration

    analysis["total_target"] = round(analysis["total_target"], 1)
    analysis["total_actual"] = round(analysis["total_actual"], 1)
    return analysis


def _tool_rewrite_narration(
    scene_num: int,
    voice: str,
    current_text: str,
    target_duration_sec: float,
    direction: str,
) -> dict:
    """Call LLM to rewrite narration text to better fit the target duration.

    Args:
        scene_num: Scene number.
        voice: Voice role (V1, V2, V3).
        current_text: The current narration text.
        target_duration_sec: Target duration in seconds.
        direction: "expand" to make longer, "trim" to make shorter.
    """
    import litellm

    target_words = int((target_duration_sec / 60) * 150)  # ~150 wpm
    current_words = len(current_text.split())

    prompt = (
        f"You are rewriting narration text for a documentary.\n\n"
        f"CURRENT TEXT ({current_words} words, voice {voice}):\n{current_text}\n\n"
        f"TARGET: {target_words} words ({target_duration_sec:.0f} seconds at ~150 wpm)\n"
        f"DIRECTION: {direction}\n\n"
    )
    if direction == "expand":
        prompt += (
            f"The narration is too SHORT by ~{target_words - current_words} words. "
            f"EXPAND it by adding more detail, examples, or elaboration. "
            f"Maintain the same tone, style, and factual content. "
            f"Do NOT add filler — add substantive content that enriches the explanation."
        )
    else:
        prompt += (
            f"The narration is too LONG by ~{current_words - target_words} words. "
            f"TRIM it by removing redundant phrases, tightening sentences, "
            f"or cutting less essential details. Maintain the key points."
        )

    prompt += "\n\nOutput ONLY the rewritten text, nothing else."

    try:
        response = litellm.completion(
            model=_RECOVERY_MODEL,
            messages=[
                {"role": "system", "content": "You are a documentary narration writer. Output only the rewritten narration text."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=2000,
        )
        new_text = response.choices[0].message.content.strip()
        new_words = len(new_text.split())
        return {
            "scene_num": scene_num,
            "voice": voice,
            "original_words": current_words,
            "new_words": new_words,
            "new_text": new_text,
            "target_words": target_words,
        }
    except Exception as e:
        return {"error": f"LLM rewrite failed: {e}"}


def _tool_get_actual_durations(diagnostic_data: str) -> dict:
    """Extract actual TTS durations from the gatekeeper diagnostic data.

    Args:
        diagnostic_data: JSON string of diagnostic data from the gatekeeper.
    """
    import json as _json
    try:
        data = _json.loads(diagnostic_data)
        return data.get("actual_durations", data)
    except (TypeError, _json.JSONDecodeError, AttributeError):
        return {"error": "Could not parse diagnostic data"}


_AUDIO_TIMING_TOOLS = [
    AgentTool(
        name="analyse_timing",
        description="Analyse narration timing per scene — returns which scenes are over/under budget and by how much.",
        parameters={
            "type": "object",
            "properties": {
                "scenes_json": {
                    "type": "string",
                    "description": "JSON array of scenes with duration_sec and voices",
                },
            },
            "required": ["scenes_json"],
        },
        fn=_tool_analyse_timing,
    ),
    AgentTool(
        name="rewrite_narration",
        description="Rewrite narration text to fit a target duration. Calls LLM to expand or trim the text.",
        parameters={
            "type": "object",
            "properties": {
                "scene_num": {"type": "integer", "description": "Scene number"},
                "voice": {"type": "string", "description": "Voice role (V1, V2, V3)"},
                "current_text": {"type": "string", "description": "Current narration text"},
                "target_duration_sec": {"type": "number", "description": "Target duration in seconds"},
                "direction": {"type": "string", "enum": ["expand", "trim"], "description": "Expand to make longer, trim to make shorter"},
            },
            "required": ["scene_num", "voice", "current_text", "target_duration_sec", "direction"],
        },
        fn=_tool_rewrite_narration,
    ),
    AgentTool(
        name="get_actual_durations",
        description="Extract actual TTS durations from the gatekeeper diagnostic data.",
        parameters={
            "type": "object",
            "properties": {
                "diagnostic_data": {
                    "type": "string",
                    "description": "JSON string of diagnostic data",
                },
            },
            "required": ["diagnostic_data"],
        },
        fn=_tool_get_actual_durations,
    ),
]


class AudioTimingAgent(RecoveryAgent):
    """Level 0 domain fix agent for audio timing mismatches.

    When the gatekeeper rejects narration for duration drift, this agent:
    1. Analyses which scenes are short/long
    2. Rewrites the narration text for the problematic scenes
    3. Returns state patches so the audio stage can regenerate
    """

    def __init__(self) -> None:
        super().__init__(
            name="audio_timing_fix",
            instruction=(
                "You are the Audio Timing Fix Agent for a documentary pipeline.\n\n"
                "The audio gatekeeper has rejected the narration because the total "
                "duration drifts too far from the target. Your job is to FIX the "
                "narration text so that when TTS regenerates, the timing will be closer "
                "to the target.\n\n"
                "STRATEGY:\n"
                "1. Use analyse_timing to identify which scenes are short/long\n"
                "2. For each problematic scene, use rewrite_narration to expand or trim the text\n"
                "3. Return a 'fix' decision with state_patches containing the new scene texts\n\n"
                "state_patches format:\n"
                '{"scenes_amendments": [{"scene_num": N, "voice": "V1", "new_text": "..."}]}\n\n'
                "RULES:\n"
                "- Focus on scenes with >10% drift first\n"
                "- For expansion: add substantive detail, not filler\n"
                "- For trimming: cut redundancy, keep key facts\n"
                "- Narration should still flow naturally\n"
                "- If drift is <5% overall, you can action='skip' (close enough)\n"
                "- If you cannot fix it after rewriting, action='escalate'"
            ),
            tools=_AUDIO_TIMING_TOOLS,
            max_tool_rounds=8,  # May need multiple rewrites
        )


# -- Tools for VisualPromptAgent -----------------------------------------------

def _tool_rewrite_visual_prompt(
    current_prompt: str,
    rejection_reason: str,
    lora_id: str,
) -> dict:
    """Rewrite a visual generation prompt based on QA rejection feedback.

    Args:
        current_prompt: The current visual prompt that was rejected.
        rejection_reason: Why the QA rejected this clip.
        lora_id: The LoRA style being used.
    """
    import litellm

    try:
        response = litellm.completion(
            model=_RECOVERY_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a cinematography prompt specialist for LTX-2.3 video model. "
                        "Rewrite the rejected prompt to address the QA feedback. "
                        "Follow the cinematography shot description format: "
                        "shot size + subject + action, environment + atmosphere, "
                        "camera movement, lighting + style, temporal change. "
                        "Output ONLY the rewritten prompt."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"REJECTED PROMPT:\n{current_prompt}\n\n"
                        f"REJECTION REASON:\n{rejection_reason}\n\n"
                        f"LoRA STYLE: {lora_id}\n\n"
                        f"Rewrite to address the rejection. Keep the same subject matter "
                        f"but fix the issues identified in the rejection."
                    ),
                },
            ],
            temperature=0.5,
            max_tokens=1000,
        )
        return {"new_prompt": response.choices[0].message.content.strip()}
    except Exception as e:
        return {"error": f"LLM rewrite failed: {e}"}


def _tool_check_lora_capabilities(lora_id: str) -> dict:
    """Check what a LoRA style is good/bad at generating.

    Args:
        lora_id: The LoRA identifier to look up.
    """
    try:
        from tools.lora_tools import get_lora_details
        return json.loads(get_lora_details(lora_id=lora_id))
    except Exception as e:
        return {"error": f"LoRA lookup failed: {e}"}


_VISUAL_PROMPT_TOOLS = [
    AgentTool(
        name="rewrite_visual_prompt",
        description="Rewrite a rejected visual prompt based on QA feedback. Calls LLM to fix the prompt.",
        parameters={
            "type": "object",
            "properties": {
                "current_prompt": {"type": "string", "description": "The rejected prompt"},
                "rejection_reason": {"type": "string", "description": "QA rejection reason"},
                "lora_id": {"type": "string", "description": "LoRA style ID"},
            },
            "required": ["current_prompt", "rejection_reason", "lora_id"],
        },
        fn=_tool_rewrite_visual_prompt,
    ),
    AgentTool(
        name="check_lora_capabilities",
        description="Look up what a LoRA style is good/bad at generating.",
        parameters={
            "type": "object",
            "properties": {
                "lora_id": {"type": "string", "description": "LoRA identifier"},
            },
            "required": ["lora_id"],
        },
        fn=_tool_check_lora_capabilities,
    ),
]


class VisualPromptAgent(RecoveryAgent):
    """Level 0 domain fix agent for visual generation QA failures.

    When video QA rejects a clip, this agent rewrites the prompt based
    on the rejection reason and the LoRA's capabilities.
    """

    def __init__(self) -> None:
        super().__init__(
            name="visual_prompt_fix",
            instruction=(
                "You are the Visual Prompt Fix Agent for a documentary pipeline.\n\n"
                "A video clip was rejected by QA. Your job is to rewrite the visual "
                "prompt so the regenerated clip passes QA.\n\n"
                "STRATEGY:\n"
                "1. Check the LoRA capabilities to understand what it can/can't do\n"
                "2. Rewrite the prompt to address the QA rejection\n"
                "3. Return state_patches with the new prompt\n\n"
                "state_patches format:\n"
                '{"prompt": "new cinematography prompt...", "seed": null}\n\n'
                "RULES:\n"
                "- Follow LTX-2.3 cinematography prompt format\n"
                "- Avoid things the model is bad at (text, complex humans, chaotic motion)\n"
                "- Use close-ups, objects, atmospherics instead of human figures\n"
                "- ONE subject, ONE action, ONE setting per prompt\n"
                "- If the LoRA can't handle the subject, suggest switching LoRA via escalate"
            ),
            tools=_VISUAL_PROMPT_TOOLS,
            max_tool_rounds=4,
        )


# -- Tools for ProductionBatchAgent -------------------------------------------

def _tool_get_worker_status() -> dict:
    """Get current fleet worker health and availability."""
    try:
        from fleet.coordinator import get_fleet_coordinator
        coordinator = get_fleet_coordinator()
        if coordinator is None:
            return {"error": "Fleet coordinator not running"}
        return coordinator.get_summary()
    except Exception as e:
        return {"error": f"Worker status check failed: {e}"}


def _tool_get_queue_status() -> dict:
    """Get work queue status — pending, active, completed, failed items."""
    try:
        from fleet.coordinator import get_fleet_coordinator
        coordinator = get_fleet_coordinator()
        if coordinator is None:
            return {"error": "Fleet coordinator not running"}
        return coordinator.work_queue.get_stats()
    except Exception as e:
        return {"error": f"Queue status check failed: {e}"}


_PRODUCTION_BATCH_TOOLS = [
    AgentTool(
        name="get_worker_status",
        description="Get current fleet worker health, availability, and resource usage.",
        parameters={"type": "object", "properties": {}},
        fn=_tool_get_worker_status,
    ),
    AgentTool(
        name="get_queue_status",
        description="Get work queue status: pending, active, completed, failed items.",
        parameters={"type": "object", "properties": {}},
        fn=_tool_get_queue_status,
    ),
]


class ProductionBatchAgent(RecoveryAgent):
    """Level 0 domain fix agent for production batch failures.

    When a production batch fails (worker crash, QA rejection, timeout),
    this agent analyses the failure and decides how to restructure.
    """

    def __init__(self) -> None:
        super().__init__(
            name="production_batch_fix",
            instruction=(
                "You are the Production Batch Fix Agent for a documentary pipeline.\n\n"
                "A production batch failed during video generation. Your job is to "
                "analyse the failure and decide how to recover.\n\n"
                "STRATEGY:\n"
                "1. Check worker health to see if the worker crashed\n"
                "2. Check queue status to see what's pending/failed\n"
                "3. Decide: reassign to different worker, split batch, or retry\n\n"
                "state_patches format:\n"
                '{"reassign_worker": true, "split_batch": false, "skip_failed_clips": [...]}\n\n'
                "RULES:\n"
                "- If worker is unhealthy, reassign to a different worker\n"
                "- If clip keeps failing on all workers, it may be a bad prompt (escalate)\n"
                "- If budget is running low, skip non-essential clips\n"
                "- Never skip more than 20% of clips in a batch"
            ),
            tools=_PRODUCTION_BATCH_TOOLS,
            max_tool_rounds=4,
        )


class OTIOValidationAgent(RecoveryAgent):
    """Level 0 domain fix agent for OTIO timeline violations."""

    def __init__(self) -> None:
        super().__init__(
            name="otio_validation_fix",
            instruction=(
                "You are the OTIO Validation Fix Agent for a documentary pipeline.\n\n"
                "The timeline guardian detected an OTIO violation (missing clips, "
                "gaps, duration mismatches). Your job is to diagnose and fix it.\n\n"
                "STRATEGY:\n"
                "1. Analyse the violation details from the error message\n"
                "2. If it's a missing clip: mark it for regeneration\n"
                "3. If it's a gap: determine what should fill it\n"
                "4. If it's a duration mismatch: identify the source\n\n"
                "state_patches format:\n"
                '{"regenerate_clips": [...], "fill_gaps": [...], "adjust_durations": [...]}\n\n'
                "RULES:\n"
                "- Timeline violations are serious — they cause assembly failures\n"
                "- Missing metadata (prompt, lora_id) usually means visual direction failed\n"
                "- If the root cause is upstream (scenario or visual direction), escalate"
            ),
            tools=[],  # OTIO tools added at wire-up time
            max_tool_rounds=3,
        )


# ═══════════════════════════════════════════════════════════════════════════
# LEVEL 1: Retry Agent
# ═══════════════════════════════════════════════════════════════════════════

def _tool_check_service_health(service: str) -> dict:
    """Check if a service/API is healthy and responsive.

    Args:
        service: Service name — "tts", "video_worker", "llm", "b2", "network".
    """
    import subprocess
    from urllib.request import Request, urlopen
    from urllib.error import URLError

    checks = {
        "tts": ("TTS service", "http://localhost:5050/health"),
        "llm": ("LLM API", "https://openrouter.ai/api/v1/models"),
        "b2": ("Backblaze B2", "https://api.backblazeb2.com"),
        "network": ("General network", "https://google.com"),
    }

    if service == "video_worker":
        try:
            from infra_agent import get_infra_agent
            agent = get_infra_agent()
            if agent is None:
                return {"service": service, "healthy": False, "reason": "InfraAgent not running"}
            status = agent.get_status()
            workers = status.get("workers", [])
            healthy = [w for w in workers if w.get("status") == "healthy"]
            return {
                "service": service,
                "healthy": len(healthy) > 0,
                "total_workers": len(workers),
                "healthy_workers": len(healthy),
            }
        except Exception as e:
            return {"service": service, "healthy": False, "reason": str(e)[:200]}

    if service == "gpu":
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split("\n")
                gpus = []
                for line in lines:
                    parts = line.split(",")
                    if len(parts) == 2:
                        used, total = int(parts[0].strip()), int(parts[1].strip())
                        gpus.append({"used_mb": used, "total_mb": total, "pct": round(used / total * 100, 1)})
                return {"service": "gpu", "healthy": True, "gpus": gpus}
            return {"service": "gpu", "healthy": False, "reason": "nvidia-smi failed"}
        except Exception as e:
            return {"service": "gpu", "healthy": False, "reason": str(e)[:200]}

    name, url = checks.get(service, ("Unknown", ""))
    if not url:
        return {"service": service, "healthy": False, "reason": f"Unknown service: {service}"}

    try:
        req = Request(url, method="HEAD")
        with urlopen(req, timeout=5) as resp:
            return {"service": service, "healthy": True, "status_code": resp.status}
    except (URLError, Exception) as e:
        return {"service": service, "healthy": False, "reason": str(e)[:200]}


def _tool_analyse_error_pattern(error_msg: str, previous_errors: str) -> dict:
    """Analyse if an error is transient or persistent based on the pattern.

    Args:
        error_msg: The current error message.
        previous_errors: JSON array of previous error messages from retry attempts.
    """
    import litellm

    try:
        prev = json.loads(previous_errors) if previous_errors else []
    except json.JSONDecodeError:
        prev = []

    try:
        response = litellm.completion(
            model=_RECOVERY_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an error pattern analyst. Classify the error as:\n"
                        "- transient: Likely to succeed on retry (network hiccup, rate limit, timeout)\n"
                        "- persistent: Same error will recur (bad input, logic error, missing resource)\n"
                        "- degrading: Getting worse with each attempt (resource exhaustion, cascading failure)\n"
                        "Respond with JSON: {\"pattern\": \"...\", \"reason\": \"...\", \"retry_recommended\": true/false}"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Current error:\n{error_msg[:500]}\n\n"
                        f"Previous errors ({len(prev)}):\n"
                        + "\n".join(f"- {e[:200]}" for e in prev[-3:])
                    ),
                },
            ],
            temperature=0.1,
            max_tokens=500,
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("{"):
            return json.loads(content)
        return {"pattern": "unknown", "reason": content[:200], "retry_recommended": False}
    except Exception as e:
        return {"error": f"Analysis failed: {e}"}


_RETRY_TOOLS = [
    AgentTool(
        name="check_service_health",
        description="Check if a service is healthy: tts, video_worker, llm, b2, network, gpu.",
        parameters={
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "enum": ["tts", "video_worker", "llm", "b2", "network", "gpu"],
                    "description": "Service to check",
                },
            },
            "required": ["service"],
        },
        fn=_tool_check_service_health,
    ),
    AgentTool(
        name="analyse_error_pattern",
        description="Analyse if an error is transient, persistent, or degrading. Uses LLM to classify.",
        parameters={
            "type": "object",
            "properties": {
                "error_msg": {"type": "string", "description": "Current error message"},
                "previous_errors": {"type": "string", "description": "JSON array of previous error messages"},
            },
            "required": ["error_msg"],
        },
        fn=_tool_analyse_error_pattern,
    ),
]


class RetryAgent(RecoveryAgent):
    """Level 1 — intelligent retry agent.

    Doesn't just retry blindly.  Checks service health, analyses error
    patterns, and decides whether retrying makes sense and with what
    adjusted parameters.
    """

    def __init__(self) -> None:
        super().__init__(
            name="retry_agent",
            instruction=(
                "You are the Retry Agent (Level 1) in the recovery ladder.\n\n"
                "The Level 0 domain fix agent could not resolve the problem. "
                "Your job is to decide if RETRYING the operation makes sense.\n\n"
                "STRATEGY:\n"
                "1. Check the health of relevant services (tts, video_worker, llm, etc.)\n"
                "2. Analyse the error pattern (transient vs persistent vs degrading)\n"
                "3. If transient: action='retry' (the operation will be retried as-is)\n"
                "4. If persistent/degrading: action='escalate' to Level 2\n\n"
                "RULES:\n"
                "- Only recommend retry if the error looks transient\n"
                "- If a service is down, escalate (retry won't help)\n"
                "- If the same error repeated 3+ times, it's persistent — escalate\n"
                "- You can suggest parameter adjustments in state_patches (e.g. timeout increase)"
            ),
            tools=_RETRY_TOOLS,
            max_tool_rounds=4,
        )


# ═══════════════════════════════════════════════════════════════════════════
# LEVEL 2: Creative Agent
# ═══════════════════════════════════════════════════════════════════════════

def _tool_suggest_alternative(
    operation_name: str,
    current_approach: str,
    failure_history: str,
) -> dict:
    """Use LLM to brainstorm an alternative approach to the failing operation.

    Args:
        operation_name: What operation is failing.
        current_approach: Description of the current approach.
        failure_history: Summary of what's been tried and failed.
    """
    import litellm

    try:
        response = litellm.completion(
            model=_RECOVERY_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a creative problem solver for a documentary production pipeline. "
                        "Suggest ONE alternative approach to achieve the same goal. "
                        "Be specific and actionable. Respond with JSON:\n"
                        '{"alternative": "description", "state_patches": {...}, "risk": "low/medium/high"}'
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Operation: {operation_name}\n"
                        f"Current approach: {current_approach}\n"
                        f"Failure history:\n{failure_history}\n\n"
                        f"What alternative approach could work?"
                    ),
                },
            ],
            temperature=0.7,
            max_tokens=1000,
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("{"):
            return json.loads(content)
        return {"alternative": content[:500], "state_patches": {}, "risk": "medium"}
    except Exception as e:
        return {"error": f"Alternative suggestion failed: {e}"}


def _tool_list_available_models(model_type: str) -> dict:
    """List available models of a given type that could be used as alternatives.

    Args:
        model_type: "tts", "video", "llm".
    """
    models = {
        "tts": [
            {"id": "qwen3-tts", "name": "Qwen3 TTS", "status": "primary"},
            {"id": "edge-tts", "name": "Edge TTS (Microsoft)", "status": "fallback"},
        ],
        "video": [
            {"id": "ltx-2.3", "name": "LTX Video 2.3", "status": "primary"},
        ],
        "llm": [
            {"id": "openrouter/google/gemini-2.5-flash", "name": "Gemini 2.5 Flash", "status": "primary"},
            {"id": "openrouter/google/gemini-2.5-pro", "name": "Gemini 2.5 Pro", "status": "fallback"},
            {"id": "openrouter/anthropic/claude-sonnet-4", "name": "Claude Sonnet 4", "status": "fallback"},
        ],
    }
    return models.get(model_type, [])


_CREATIVE_TOOLS = [
    AgentTool(
        name="suggest_alternative",
        description="Use LLM to brainstorm an alternative approach to the failing operation.",
        parameters={
            "type": "object",
            "properties": {
                "operation_name": {"type": "string", "description": "What operation is failing"},
                "current_approach": {"type": "string", "description": "Current approach description"},
                "failure_history": {"type": "string", "description": "Summary of failures"},
            },
            "required": ["operation_name", "current_approach", "failure_history"],
        },
        fn=_tool_suggest_alternative,
    ),
    AgentTool(
        name="list_available_models",
        description="List available models of a type (tts, video, llm) that could be used as alternatives.",
        parameters={
            "type": "object",
            "properties": {
                "model_type": {"type": "string", "enum": ["tts", "video", "llm"]},
            },
            "required": ["model_type"],
        },
        fn=_tool_list_available_models,
    ),
]


class CreativeAgent(RecoveryAgent):
    """Level 2 — creative strategy agent.

    When retrying didn't work, this agent brainstorms alternative
    approaches: different model, different parameters, restructured input.
    """

    def __init__(self) -> None:
        super().__init__(
            name="creative_agent",
            instruction=(
                "You are the Creative Agent (Level 2) in the recovery ladder.\n\n"
                "Levels 0 (domain fix) and 1 (retry) failed. Your job is to find "
                "a CREATIVE ALTERNATIVE approach to achieve the same goal.\n\n"
                "STRATEGY:\n"
                "1. Review what's been tried and why it failed\n"
                "2. Brainstorm alternatives: different model, different params, restructured input\n"
                "3. Check what models/resources are available\n"
                "4. Return a fix with the alternative approach in state_patches\n\n"
                "EXAMPLES:\n"
                "- TTS keeps timing out → switch to edge-tts as fallback\n"
                "- Video prompt keeps producing bad output → simplify the prompt radically\n"
                "- LLM max_tokens → reduce input size or switch to model with larger context\n\n"
                "RULES:\n"
                "- Be bold — you're the creative option, not the safe one\n"
                "- The alternative should be DIFFERENT, not just retry with small changes\n"
                "- If you truly can't think of an alternative, escalate to Level 3"
            ),
            tools=_CREATIVE_TOOLS,
            max_tool_rounds=4,
        )


# ═══════════════════════════════════════════════════════════════════════════
# LEVEL 3: Collaborative Agent (inter-agent communication)
# ═══════════════════════════════════════════════════════════════════════════

def _tool_request_from_agent(agent_name: str, request: str) -> dict:
    """Send a request to another pipeline agent and get its response.

    This enables inter-agent collaboration: the recovery agent can ask
    the Scenario Director to regenerate scenes, the Production Supervisor
    to restructure batches, the Audio Agent to try different TTS params, etc.

    Args:
        agent_name: Name of the agent to consult ("scenario_director",
            "production_supervisor", "audio_agent", "visual_director",
            "assembly_director").
        request: Natural language description of what you need from the agent.
    """
    import litellm

    # Map agent names to their system instructions (simplified for recovery context)
    agent_instructions = {
        "scenario_director": (
            "You are the Scenario Director for a documentary pipeline. "
            "Another agent is asking for your help to fix a pipeline problem. "
            "You have authority over scene structure, narration text, duration targets, "
            "and narrative flow. Respond with specific, actionable changes."
        ),
        "production_supervisor": (
            "You are the Production Supervisor for a documentary pipeline. "
            "Another agent is asking for your help to fix a pipeline problem. "
            "You have authority over batch planning, worker assignment, GPU scheduling, "
            "and production strategy. Respond with specific, actionable changes."
        ),
        "audio_agent": (
            "You are the Audio Agent for a documentary pipeline. "
            "Another agent is asking for your help to fix a pipeline problem. "
            "You have authority over TTS generation, voice selection, narration timing, "
            "and audio quality. Respond with specific, actionable changes."
        ),
        "visual_director": (
            "You are the Visual Director for a documentary pipeline. "
            "Another agent is asking for your help to fix a pipeline problem. "
            "You have authority over visual concepts, LoRA selection, camera movements, "
            "and prompt writing. Respond with specific, actionable changes."
        ),
        "assembly_director": (
            "You are the Assembly Director for a documentary pipeline. "
            "Another agent is asking for your help to fix a pipeline problem. "
            "You have authority over final assembly, clip ordering, transitions, "
            "and output format. Respond with specific, actionable changes."
        ),
    }

    instruction = agent_instructions.get(agent_name)
    if instruction is None:
        return {
            "error": f"Unknown agent: {agent_name}. "
            f"Available: {list(agent_instructions.keys())}",
        }

    try:
        response = litellm.completion(
            model=_RECOVERY_MODEL,
            messages=[
                {"role": "system", "content": instruction},
                {
                    "role": "user",
                    "content": (
                        f"RECOVERY REQUEST from another agent:\n\n{request}\n\n"
                        f"Respond with JSON:\n"
                        '{"can_help": true/false, "changes": {...}, "explanation": "..."}'
                    ),
                },
            ],
            temperature=0.4,
            max_tokens=2000,
        )
        content = response.choices[0].message.content.strip()
        # Try to parse as JSON
        if "```json" in content:
            json_str = content.split("```json")[1].split("```")[0].strip()
            return json.loads(json_str)
        if content.startswith("{"):
            return json.loads(content)
        return {"can_help": True, "changes": {}, "explanation": content[:500]}
    except Exception as e:
        return {"error": f"Agent consultation failed: {e}"}


def _tool_get_pipeline_state_summary(state_keys: str) -> dict:
    """Get a summary of specific pipeline state keys for context sharing.

    Args:
        state_keys: Comma-separated list of state keys to retrieve.
    """
    # This will be populated at wire-up time with actual state access
    return {"note": "Pipeline state access — populated at runtime", "keys_requested": state_keys}


_COLLABORATIVE_TOOLS = [
    AgentTool(
        name="request_from_agent",
        description=(
            "Send a request to another pipeline agent for help. Available agents: "
            "scenario_director, production_supervisor, audio_agent, visual_director, assembly_director."
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent_name": {
                    "type": "string",
                    "enum": ["scenario_director", "production_supervisor", "audio_agent", "visual_director", "assembly_director"],
                    "description": "Which agent to consult",
                },
                "request": {
                    "type": "string",
                    "description": "What you need from the agent (natural language)",
                },
            },
            "required": ["agent_name", "request"],
        },
        fn=_tool_request_from_agent,
    ),
    AgentTool(
        name="get_pipeline_state_summary",
        description="Get a summary of specific pipeline state keys for context sharing between agents.",
        parameters={
            "type": "object",
            "properties": {
                "state_keys": {
                    "type": "string",
                    "description": "Comma-separated state keys to retrieve",
                },
            },
            "required": ["state_keys"],
        },
        fn=_tool_get_pipeline_state_summary,
    ),
]


class CollaborativeAgent(RecoveryAgent):
    """Level 3 — collaborative multi-agent recovery.

    When domain fix, retry, and creative approaches all failed, this
    agent reaches out to OTHER pipeline agents for help.  It can ask
    the Scenario Director to regenerate scenes, the Production Supervisor
    to restructure, etc.
    """

    def __init__(self) -> None:
        super().__init__(
            name="collaborative_agent",
            instruction=(
                "You are the Collaborative Agent (Level 3) in the recovery ladder.\n\n"
                "Levels 0-2 all failed. Your job is to coordinate with OTHER agents "
                "in the pipeline to fix the problem.\n\n"
                "AVAILABLE AGENTS:\n"
                "- scenario_director: Controls scene structure, narration text, durations\n"
                "- production_supervisor: Controls batch planning, worker assignment\n"
                "- audio_agent: Controls TTS params, voice selection, narration timing\n"
                "- visual_director: Controls visual concepts, LoRA selection, prompts\n"
                "- assembly_director: Controls final assembly, transitions\n\n"
                "STRATEGY:\n"
                "1. Identify which agent has authority over the root cause\n"
                "2. Send a detailed request explaining the problem and what you need\n"
                "3. Interpret the agent's response and apply their suggested changes\n\n"
                "EXAMPLES:\n"
                "- Audio timing keeps failing → ask scenario_director to regenerate "
                "  scenes with stricter duration constraints\n"
                "- Video clips keep getting rejected → ask visual_director to use "
                "  simpler prompts or different LoRA styles\n"
                "- Production batches keep failing → ask production_supervisor to "
                "  restructure the batch plan with fewer clips per batch\n\n"
                "RULES:\n"
                "- Explain the FULL failure history to the consulted agent\n"
                "- The other agent's response goes into state_patches\n"
                "- If no agent can help, escalate to Level 4 (human)"
            ),
            tools=_COLLABORATIVE_TOOLS,
            max_tool_rounds=6,  # May need to consult multiple agents
        )


# ═══════════════════════════════════════════════════════════════════════════
# Pre-built agent configurations (policies with agents)
# ═══════════════════════════════════════════════════════════════════════════

# These are ready-to-use agent sets for common operation types.
# Wire them into RecoveryPolicy.agents when creating policies.

AUDIO_AGENTS = {
    0: AudioTimingAgent(),
    1: RetryAgent(),
    2: CreativeAgent(),
    3: CollaborativeAgent(),
}

VIDEO_AGENTS = {
    0: VisualPromptAgent(),
    1: RetryAgent(),
    2: CreativeAgent(),
    3: CollaborativeAgent(),
}

PRODUCTION_AGENTS = {
    0: ProductionBatchAgent(),
    1: RetryAgent(),
    2: CreativeAgent(),
    3: CollaborativeAgent(),
}

OTIO_AGENTS = {
    0: OTIOValidationAgent(),
    1: RetryAgent(),
    2: CreativeAgent(),
    3: CollaborativeAgent(),
}

# Generic agents (no domain-specific L0) — for operations that don't have
# a specialised fix agent.  Starts at L1.
GENERIC_AGENTS = {
    1: RetryAgent(),
    2: CreativeAgent(),
    3: CollaborativeAgent(),
}


# ---------------------------------------------------------------------------
# Supervisor consultation for recovery agents
# ---------------------------------------------------------------------------
#
# Closes #61/#73/#76/#77/#103: when a recovery agent can't reach a decision
# (LLM failure, tool-round exhaustion, parse failure) it used to silently
# return ``action="escalate"`` — the round-robin fall-through that led to
# five extension-clip decisions and nine regenerations being handed to the
# human during the PAG run.  We now consult the production supervisor so
# the canonical ``EscalationAction`` menu is applied instead.

def _supervisor_fallback_decision(
    context: "RecoveryContext | None",
    reason: str,
    tool_results: list[dict],
) -> RecoveryDecision:
    """Consult supervisor_escalate when a recovery agent cannot decide.

    Returns a ``RecoveryDecision`` with the canonical action, mapped to
    the recovery agent vocabulary (``fix``/``retry``/``skip``/``abort``).
    ``escalate`` is intentionally NEVER returned here — the supervisor
    IS the escalation target; passing the buck would reintroduce #61.
    """
    try:
        from agents.production_supervisor import supervisor_escalate
        from orchestrator.escalation_menu import EscalationContext
    except Exception as exc:  # pragma: no cover — import safety net
        logger.warning(
            "Supervisor unavailable in recovery agent fallback: %s — "
            "passing to next recovery level",
            exc,
        )
        return RecoveryDecision(
            action="escalate",
            explanation=(
                f"{reason} (supervisor module unavailable: {exc})"
            ),
            tool_results=tool_results,
        )

    # Build descriptor/history from context if present.
    descriptor: dict = {"reason": reason}
    history: list[dict] = []
    op_name = "recovery_agent_fallback"
    pipeline_state: dict = {}
    if context is not None:
        op_name = context.operation_name
        pipeline_state = context.pipeline_state or {}
        descriptor.update({
            "operation_name": context.operation_name,
            "error_msg": context.error_msg[:500],
            "current_level": context.current_level,
            "level_name": context.level_name,
        })
        for prev in (context.previous_attempts or [])[-10:]:
            history.append({
                "action": f"L{prev.get('level', '?')}:{prev.get('agent', '?')}",
                "outcome": prev.get("explanation", "")[:200],
                "timestamp": time.time(),
            })

    esc_context = EscalationContext(
        failing_artifact=op_name,
        artifact_descriptor=descriptor,
        timeline_state_snapshot=pipeline_state,
        escalation_history=history,
    )

    try:
        action = supervisor_escalate(esc_context)
    except Exception as exc:  # pragma: no cover — defensive
        logger.error(
            "supervisor_escalate raised in recovery fallback: %s", exc,
        )
        return RecoveryDecision(
            action="escalate",
            explanation=f"{reason} (supervisor raised: {exc})",
            tool_results=tool_results,
        )

    # Map canonical action → recovery vocabulary.
    _CANONICAL_TO_RECOVERY = {
        "regenerate_clip": "fix",
        "generate_extension_clip": "fix",
        "replace_with_brand_card": "skip",
        "rewrite_scene": "fix",
        "abort_run": "abort",
    }
    mapped = _CANONICAL_TO_RECOVERY.get(action.action, "abort")
    return RecoveryDecision(
        action=mapped,
        state_patches=action.to_dict(),
        explanation=(
            f"supervisor_escalate → {action.action} (L{action.level}): "
            f"{action.llm_reasoning or 'no rationale'}"
        ),
        tool_results=tool_results,
        confidence=0.6,
    )
