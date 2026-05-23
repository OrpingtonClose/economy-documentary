"""Pydantic models for GPU requirements — extracted from web search text.

When the agent needs to provision a VM, it should not guess GPU specs.
It should RESEARCH what the model actually needs, then match against
available Vast.ai offers. These models define what the agent should
infer from search results.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class GPURequirements(BaseModel):
    """Hardware requirements for a specific ML model — extracted from web search."""

    model_name: str = Field(description="The model being researched")
    min_vram_gb: float = Field(description="Minimum VRAM required in GB")
    recommended_vram_gb: float = Field(description="Recommended VRAM for comfortable operation")
    gpu_architecture: str = Field(default="", description="e.g. 'Ampere', 'Hopper', 'Ada Lovelace'")
    min_cuda_version: str = Field(default="", description="Minimum CUDA version if specified")
    min_disk_gb: int = Field(default=50, description="Minimum disk space in GB")
    estimated_boot_time_min: int = Field(default=5, description="Estimated time to download model and warm up")
    docker_image: str = Field(default="", description="Recommended base Docker image if known")
    worker_ready_signal: str = Field(default="", description="What text the worker HTTP endpoint returns when ready")
    notes: str = Field(default="", description="Any special notes (e.g. 'needs 2x GPUs', 'CPU fallback available')")


class VastAIOfferMatch(BaseModel):
    """How well a Vast.ai offer matches GPU requirements — extracted from CLI text."""

    offer_id: str = Field(description="The Vast.ai offer ID")
    suitable: bool = Field(description="Does this offer meet minimum requirements?")
    vram_gb: float = Field(description="Offer VRAM in GB")
    price_per_hour: float = Field(description="Price in USD/hour")
    gpu_name: str = Field(description="GPU model name")
    match_score: float = Field(ge=0.0, le=1.0, description="0.0-1.0 score based on vram/price ratio")
    concerns: list[str] = Field(default_factory=list, description="Why this offer might be problematic")
    recommendation: str = Field(description="One of: 'ideal', 'acceptable', 'marginal', 'reject'")
