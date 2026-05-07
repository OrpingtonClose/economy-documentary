"""Artifact provenance schema — full causal record for every piece of media.

Every artifact produced by the pipeline carries a complete record of what
led to its creation. This is not optional metadata — it is the audit
trail. If two artifacts differ, the provenance must explain why.

Design principle: an artifact's provenance should be sufficient to
reproduce it (given the same model, same environment, same prompts).
If it cannot be reproduced, the provenance must explain what was
non-deterministic and what the alternatives were.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

@dataclass
class ArtifactProvenance:
    """Complete causal record for a single piece of media.

    Written once at creation time. Never mutated afterward — only
    appended to (QA results arrive after creation).
    """

    # -- Identity --
    artifact_id: str = ""          # UUID or deterministic hash
    artifact_type: str = ""       # "scene_plan", "narration_wav", "video_clip",
                                  # "visual_concept", "alignment", "assembly"

    # -- Provenance: who made it --
    creator_agent: str = ""       # "scenario", "audio", "visual", "production", "assembly"
    creator_tool: str = ""        # "generate_scenario", "generate_narration", "submit_gpu_production_job"
    created_at: float = 0.0       # Unix timestamp

    # -- Provenance: what was asked --
    prompt: str = ""              # The exact prompt that led to this artifact.
                                  # For LLM calls: the user message + system prompt excerpt.
                                  # For tool calls: the tool arguments.
    prompt_hash: str = ""         # SHA-256 of the prompt for dedup/caching

    # -- Provenance: what model produced it --
    model_id: str = ""            # "claude-sonnet-4-20250514"
    model_version: str = ""       # Full model version string if available
    model_parameters: dict = field(default_factory=dict)  # temperature, max_tokens, top_p, etc.
    token_usage: dict = field(default_factory=dict)       # input_tokens, output_tokens, cost_usd
    model_region: str = ""        # "us-east-1", "eu-west-1"

    # -- Provenance: production environment --
    environment: dict = field(default_factory=dict)
    # Examples:
    #   python_version: "3.11.14"
    #   os: "darwin-arm64"
    #   strands_version: "0.9.2"
    #   otio_version: "0.16.0"
    #   gpu_worker_id: "vast-48291"
    #   gpu_worker_url: "http://..."
    #   gpu_model: "H200"
    #   gpu_vram_gb: "80"
    #   tts_worker_url: "http://..."
    #   tts_model: "Qwen3-TTS"

    # -- Provenance: lineage --
    parent_artifacts: list[str] = field(default_factory=list)
    # B2 keys of upstream artifacts this depends on.
    # A narration_wav depends on: ["state/scenes.json", "state/style_lock.json"]
    # A video_clip depends on: ["state/visual_concepts.json", "audio/s1p0.wav"]

    upstream_stage: str = ""      # "scenario" for artifacts consumed by audio stage

    # -- Provenance: tool call chain --
    tool_calls: list[dict] = field(default_factory=list)
    # Ordered list of tool calls that led to this artifact.
    # Each: {"tool": str, "args": dict, "result_summary": str, "duration_ms": int}
    # This captures the full reasoning chain, not just the final call.

    # -- State --
    status: str = "valid"        # "valid", "partial", "error", "rejected"
    error: str = ""              # If status is error/partial/rejected, what happened
    is_deterministic: bool = False  # Can this be reproduced exactly?

    # -- Location --
    disk_path: str = ""          # Local filesystem path
    b2_key: str = ""             # B2 object key
    otio_track: str = ""         # "V1_Video", "A1_Narration", "A2_Music", or "" for metadata
    otio_clip_name: str = ""     # Clip name in OTIO timeline

    # -- Content metadata --
    content_meta: dict = field(default_factory=dict)
    # Type-specific:
    #   narration_wav: {duration, sample_rate, channels, word_count, voice_role, text}
    #   video_clip: {duration, resolution, fps, codec, scene_num, phrase_idx}
    #   scene_plan: {scene_count, total_duration_sec, language}
    #   visual_concept: {scene_num, phrase_idx, prompt, mood}
    #   alignment: {method, alignment_count, avg_confidence}

    # -- QA results (appended after creation) --
    qa_checks: list[dict] = field(default_factory=list)
    # Every QA check performed on this artifact, including FAILED ones.
    # Each: {
    #   "check": str,        # "duration_matches", "voice_count", "has_audio"
    #   "result": str,       # "PASS", "FAIL", "SKIP", "ERROR", "TIMEOUT"
    #   "message": str,      # "Duration 48s exceeds 45s limit"
    #   "check_at": float,   # Unix timestamp
    #   "checker": str,      # "evaluate_scenario", "contract_enforcer", "timeline_guardian"
    # }
    # FAILED checks are NOT removed. They stay in the record.
    # A later PASS on the same check is appended, not overwritten.

    # -- Escalation history --
    escalations: list[dict] = field(default_factory=list)
    # If this artifact triggered an escalation:
    # Each: {
    #   "escalation_type": str,  # "contract_violation", "qa_failure", "timing_overrun"
    #   "stage": str,
    #   "reason": str,
    #   "resolution": str,       # "retried", "revised", "accepted_with_caveat", "abandoned"
    #   "resolved_at": float,
    # }

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "creator_agent": self.creator_agent,
            "creator_tool": self.creator_tool,
            "created_at": self.created_at,
            "prompt": self.prompt[:500],  # Truncate for storage, full version on disk
            "prompt_hash": self.prompt_hash,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "model_parameters": self.model_parameters,
            "token_usage": self.token_usage,
            "model_region": self.model_region,
            "environment": self.environment,
            "parent_artifacts": self.parent_artifacts,
            "upstream_stage": self.upstream_stage,
            "tool_calls": self.tool_calls,
            "status": self.status,
            "error": self.error,
            "is_deterministic": self.is_deterministic,
            "disk_path": self.disk_path,
            "b2_key": self.b2_key,
            "otio_track": self.otio_track,
            "otio_clip_name": self.otio_clip_name,
            "content_meta": self.content_meta,
            "qa_checks": self.qa_checks,
            "escalations": self.escalations,
        }

    def append_qa(self, check: str, result: str, message: str, checker: str) -> None:
        """Append a QA check result. Never removes previous results."""
        import time
        self.qa_checks.append({
            "check": check,
            "result": result,
            "message": message,
            "checker": checker,
            "check_at": time.time(),
        })

    def append_escalation(self, escalation_type: str, stage: str, reason: str) -> None:
        """Record that this artifact triggered an escalation."""
        self.escalations.append({
            "escalation_type": escalation_type,
            "stage": stage,
            "reason": reason,
            "opened_at": time.time(),
        })
