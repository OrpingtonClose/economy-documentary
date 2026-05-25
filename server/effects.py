"""Algebraic effect types — the only things that can mutate pipeline state.

Every state change is an effect. Only validated effects are appended to the event store.
Only the projection handler applies effects.

Effects fall into three categories:
1. AGENT effects — proposed by agents (UpdateScript, GenerateNarrationAudio, RenderVideoSegment)
2. WORKER effects — emitted by workers and provisioner (JobQueued, JobStarted, JobCompleted, JobFailed, VMAllocated)
3. QA effects — emitted by QA jury (QAPassed, QAFailed, JobRequeued)
4. SYSTEM effects — internal (NoOp, ExecuteRawBash)
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Effect(BaseModel):
    """Base class for all algebraic effects."""

    effect_type: str
    agent_id: str = ""
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    justification: str = Field(default="", description="The raw text that caused this effect")
    scene_num: int = Field(default=0, description="Scene number this effect applies to")


# ---------------------------------------------------------------------------
# AGENT EFFECTS — proposed by agents, parsed by instructor
# ---------------------------------------------------------------------------

class UpdateScript(Effect):
    """Scenario agent proposes script/narration/visual changes."""

    effect_type: Literal["UpdateScript"] = "UpdateScript"
    narration_v1: str = Field(default="", description="V1 Hook narration text")
    narration_v2: str = Field(default="", description="V2 Expert narration text")
    narration_v3: str = Field(default="", description="V3 Storyteller narration text")
    visual_notes: str = Field(default="", description="Visual description and shot notes")
    dopamine_hook: str = Field(default="", description="Dopamine hook phrase")
    pronunciation_hints: str = Field(default="", description="Pronunciation guidance")
    duration_sec: int = Field(default=30, description="Target scene duration in seconds")


class GenerateNarrationAudio(Effect):
    """Audio agent requests TTS generation for a voice line."""

    effect_type: Literal["GenerateNarrationAudio"] = "GenerateNarrationAudio"
    voice: str = Field(default="V1", description="Voice identifier: V1, V2, or V3")
    text: str = Field(default="", description="Narration text to synthesize")


class RenderVideoSegment(Effect):
    """Video agent requests LTX video generation for a scene."""

    effect_type: Literal["RenderVideoSegment"] = "RenderVideoSegment"
    prompt: str = Field(default="", description="LTX-2.3 video generation prompt")
    lora_id: str = Field(default="", description="LoRA identifier for style consistency")
    duration_sec: int = Field(default=5, description="Target clip duration in seconds")


class MergeIntoOTIO(Effect):
    """Assembly agent requests clips be merged into the OTIO timeline."""

    effect_type: Literal["MergeIntoOTIO"] = "MergeIntoOTIO"
    audio_clips: list[dict] = Field(default_factory=list, description="Audio clips to add to A1_Narration")
    video_clips: list[dict] = Field(default_factory=list, description="Video clips to add to V1_Video")


# ---------------------------------------------------------------------------
# WORKER EFFECTS — emitted by workers and provisioner
# ---------------------------------------------------------------------------

class JobQueued(Effect):
    """A job was created in the queue. Emitted by projection handler."""

    effect_type: Literal["JobQueued"] = "JobQueued"
    job_type: str = Field(default="NARRATION", description="NARRATION or VIDEO_RENDER")
    stage: str = Field(default="audio", description="audio or video")
    job_id: str = Field(default="", description="Unique job ID")
    payload: dict = Field(default_factory=dict, description="Job payload")


class JobStarted(Effect):
    """A worker claimed a job and started processing."""

    effect_type: Literal["JobStarted"] = "JobStarted"
    job_id: str = Field(default="", description="Job ID")
    worker_id: str = Field(default="", description="Worker that claimed the job")
    instance_id: str = Field(default="", description="Vast.ai instance ID of the VM")
    stage: str = Field(default="", description="audio or video")


class JobCompleted(Effect):
    """A worker finished processing a job. Artifact is ready."""

    effect_type: Literal["JobCompleted"] = "JobCompleted"
    job_id: str = Field(default="", description="Job ID")
    artifact_path: str = Field(default="", description="Path to generated file on the VM")
    local_artifact_path: str = Field(default="", description="Path to downloaded file on the pipeline host")
    stage: str = Field(default="", description="audio or video")


class JobFailed(Effect):
    """A worker failed to process a job."""

    effect_type: Literal["JobFailed"] = "JobFailed"
    job_id: str = Field(default="", description="Job ID")
    error_message: str = Field(default="", description="Why the job failed")
    stage: str = Field(default="", description="audio or video")


class VMAllocated(Effect):
    """A VM was provisioned for worker jobs."""

    effect_type: Literal["VMAllocated"] = "VMAllocated"
    instance_id: str = Field(default="", description="VM instance ID")
    offer_id: str = Field(default="", description="Vast.ai offer ID")
    gpu_type: str = Field(default="", description="GPU type")
    worker_url: str = Field(default="", description="Worker HTTP URL if available")


class VMDeallocated(Effect):
    """A VM was destroyed."""

    effect_type: Literal["VMDeallocated"] = "VMDeallocated"
    instance_id: str = Field(default="", description="VM instance ID")
    reason: str = Field(default="", description="Why the VM was destroyed")


class VMProvisionFailed(Effect):
    """A VM provisioning attempt failed."""

    effect_type: Literal["VMProvisionFailed"] = "VMProvisionFailed"
    offer_id: str = Field(default="", description="Vast.ai offer ID that was attempted")
    error_message: str = Field(default="", description="Why provisioning failed")


# ---------------------------------------------------------------------------
# QA EFFECTS — emitted by QA jury
# ---------------------------------------------------------------------------

class QAPassed(Effect):
    """QA jury approved an artifact."""

    effect_type: Literal["QAPassed"] = "QAPassed"
    job_id: str = Field(default="", description="Job ID")
    artifact_path: str = Field(default="", description="Path to artifact")
    verdict: str = Field(default="", description="QA verdict text")


class QAFailed(Effect):
    """QA jury rejected an artifact."""

    effect_type: Literal["QAFailed"] = "QAFailed"
    job_id: str = Field(default="", description="Job ID")
    artifact_path: str = Field(default="", description="Path to artifact")
    verdict: str = Field(default="", description="QA verdict text")
    comments: list[str] = Field(default_factory=list, description="Specific issues found")
    suggested_fix: str = Field(default="", description="How to fix the issues")


class JobRequeued(Effect):
    """A failed job was requeued with QA comments for retry."""

    effect_type: Literal["JobRequeued"] = "JobRequeued"
    job_id: str = Field(default="", description="Job ID")
    comments: list[str] = Field(default_factory=list, description="QA comments")
    suggested_fix: str = Field(default="", description="Suggested fix")


class JobQuestionReceived(Effect):
    """A worker asked a clarifying question about a job."""

    effect_type: Literal["JobQuestionReceived"] = "JobQuestionReceived"
    job_id: str = Field(default="", description="Job ID")
    question: str = Field(default="", description="Question from the worker")
    worker_url: str = Field(default="", description="Worker that asked the question")


class JobQuestionAnswered(Effect):
    """The pipeline sent an answer to a worker's clarifying question."""

    effect_type: Literal["JobQuestionAnswered"] = "JobQuestionAnswered"
    job_id: str = Field(default="", description="Job ID")
    answer: str = Field(default="", description="Answer sent to the worker")


# ---------------------------------------------------------------------------
# SYSTEM EFFECTS
# ---------------------------------------------------------------------------

class NoOp(Effect):
    """No actionable effect detected."""

    effect_type: Literal["NoOp"] = "NoOp"
    reason: str = Field(default="", description="Why no effect was extracted")
