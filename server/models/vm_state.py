"""Pydantic models for VM/worker state — extracted from raw CLI text via LLM.

These are NOT dataclasses populated by code parsing. They are targets for
instructor + DeepSeek v4-flash extraction from the raw text the agent sees.

The agent receives SSH output, Vast.ai CLI output, worker HTTP responses —
all raw text. These models define what the agent SHOULD infer from that text.
The extraction layer (structured_extract.py) turns text → typed objects.

This is the "types implied in text" architecture:
    Raw text (SSH/CLI/HTTP) → LLM extraction → VMState (typed)
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class WorkerStatus(BaseModel):
    """What a GPU worker reports about itself (extracted from HTTP GET / text)."""

    ready: bool = Field(description="Worker has loaded models and accepts jobs")
    worker_type: str = Field(description="'tts' or 'ltx' or 'unknown'")
    gpu_name: str = Field(default="", description="GPU model name if reported")
    vram_used_gb: float = Field(default=0.0, description="VRAM in use")
    vram_total_gb: float = Field(default=0.0, description="Total VRAM")
    model_loaded: str = Field(default="", description="Which model is loaded")
    jobs_in_queue: int = Field(default=0, description="Pending jobs")
    uptime_seconds: float = Field(default=0.0)


class VMState(BaseModel):
    """Complete state of a Vast.ai VM — extracted from `vastai show instance` text."""

    instance_id: str = Field(description="Vast.ai instance ID")
    status: str = Field(description="'running', 'offline', 'loading', 'unknown'")
    ssh_host: str = Field(default="")
    ssh_port: int = Field(default=0)
    gpu_name: str = Field(default="")
    vram_gb: float = Field(default=0.0)
    price_per_hour: float = Field(default=0.0)
    disk_gb: float = Field(default=0.0)
    worker_url: str = Field(default="", description="HTTP endpoint if known")
    worker_status: WorkerStatus | None = Field(default=None)
    provisioning_age_minutes: float = Field(default=0.0)
    labeled_for_run: str = Field(default="", description="Run ID this VM was labeled for")
    labeled_for_stage: str = Field(default="", description="'audio' or 'video'")


class VMRegistryDecision(BaseModel):
    """What the agent should do about VM provisioning — extracted from reasoning text."""

    action: str = Field(
        description="One of: 'use_existing', 'provision_new', 'destroy_and_reprovision', 'wait', 'abort'"
    )
    target_instance_id: str = Field(default="", description="Which VM to use/destroy")
    reason: str = Field(description="Why this action was chosen")
    confidence: float = Field(ge=0.0, le=1.0)
