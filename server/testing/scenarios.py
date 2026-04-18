"""
ADK Environment Simulation test scenarios.

Each scenario is a function returning an ``EnvironmentSimulationConfig`` that
defines targeted tool injections for a specific failure/success pattern.

Scenarios are grouped into categories:

    A — Tool-level failures that trigger escalation
    B — Gatekeeper quality-gate failures
    C — Timeline Guardian OTIO violations
    D — Systemic / fleet-level failures
    E — Happy paths (no failures)
    F — Edge cases

Usage::

    from testing.scenarios import get_scenario, list_scenarios

    config = get_scenario("A1")   # Audio duration drift
    # or
    config = get_scenario("E1")   # Full success (happy path)
"""

from __future__ import annotations

import json
import os
from typing import Dict, Optional

from google.adk.tools.environment_simulation.environment_simulation_config import (
    EnvironmentSimulationConfig,
    InjectedError,
    InjectionConfig,
    MockStrategy,
    ToolSimulationConfig,
)

# ---------------------------------------------------------------------------
# Constants for mock responses
# ---------------------------------------------------------------------------

_SAMPLE_RATE = 24000
_VIDEO_DIR = os.environ.get("VIDEO_OUTPUT_DIR", "/tmp/documentary-pipeline/video")
_AUDIO_DIR = os.environ.get("TTS_OUTPUT_DIR", "/tmp/documentary-pipeline/audio")


def _tts_success(
    scene_num: int = 1,
    voice_role: str = "V1",
    duration: float = 39.0,
    mode: str = "simulated",
) -> dict:
    """Mock successful TTS response."""
    return {
        "status": "generated",
        "mode": mode,
        "wav_path": f"{_AUDIO_DIR}/scene_{scene_num:03d}_{voice_role}.wav",
        "duration": round(duration, 2),
        "sample_rate": _SAMPLE_RATE,
        "text_length": 200,
        "word_count": 40,
    }


def _tts_short_duration(
    scene_num: int = 1,
    voice_role: str = "V1",
    target_duration: float = 39.0,
    drift_factor: float = 0.65,
) -> dict:
    """Mock TTS with duration significantly shorter than target (triggers gatekeeper)."""
    return _tts_success(
        scene_num=scene_num,
        voice_role=voice_role,
        duration=round(target_duration * drift_factor, 2),
        mode="simulated_short",
    )


def _video_success(
    output_path: str = "",
    duration_sec: float = 5.0,
    lora_id: str = "documentary-realism",
) -> dict:
    """Mock successful video generation response."""
    return {
        "status": "generated",
        "mode": "simulated",
        "output_path": output_path or f"{_VIDEO_DIR}/sim_clip.mp4",
        "target_duration": round(duration_sec, 2),
        # ARCH-F3 (#164): exact duration, no margin.
        "actual_duration": round(duration_sec, 2),
        "lora_id": lora_id,
        "lora_weight": 0.7,
        "resolution": "1280x720",
        "fps": 24,
    }


def _video_qa_rejected(reason: str = "black frames detected") -> dict:
    """Mock video generation where QA rejects the output."""
    return {
        "success": False,
        "error": f"QA REJECTED: visual quality below threshold. QA_HINTS: {reason}",
        "error_type": "qa_rejected",
        "qa_quality": "rejected",
        "qa_reason": reason,
    }


def _video_cuda_oom() -> dict:
    """Mock CUDA out-of-memory error from GPU worker."""
    return {
        "success": False,
        "error": "CUDA out of memory. Tried to allocate 4.00 GiB",
        "error_type": "cuda_oom",
    }


def _video_timeout() -> dict:
    """Mock timeout from GPU worker."""
    return {
        "success": False,
        "error": "GPU worker timed out after 3600s",
        "error_type": "timeout",
    }


def _provision_success(gpu_type: str = "A100_SXM4") -> dict:
    """Mock successful VM provisioning."""
    return {
        "status": "provisioned",
        "mode": "simulated",
        "vm_id": "sim-vm-001",
        "gpu_type": gpu_type,
        "min_vram_gb": 48,
        "max_price": 1.50,
        "ip": "192.168.1.100",
        "port": 8080,
    }


def _provision_no_offers() -> dict:
    """Mock empty GPU offers from Vast.ai."""
    return {
        "status": "no_offers",
        "error": "Vast.ai returned no offers matching criteria",
    }


def _provision_insufficient_credits() -> dict:
    """Mock insufficient Vast.ai credits."""
    return {
        "status": "error",
        "error": "Insufficient Vast.ai credits: $0.12 (reserve=$5.00)",
    }


def _alignment_success(word_count: int = 40) -> dict:
    """Mock successful WhisperX alignment."""
    words = []
    t = 0.0
    for i in range(word_count):
        words.append({"word": f"word_{i}", "start": round(t, 3), "end": round(t + 0.3, 3)})
        t += 0.35
    return {
        "status": "aligned",
        "mode": "simulated",
        "wav_path": f"{_AUDIO_DIR}/scene_001_V1.wav",
        "words": words,
        "total_duration": round(t, 3),
        "word_count": word_count,
    }


def _timeline_success(topic: str = "test_documentary") -> dict:
    """Mock successful OTIO timeline creation."""
    return {
        "status": "created",
        "timeline_path": f"/tmp/documentary-pipeline/timelines/{topic}.otio",
        "num_scenes": 5,
        "tracks": ["V1 Video", "A1 Narration", "A2 Music"],
    }


# ---------------------------------------------------------------------------
# Category A: Tool-Level Failures → Escalation
# ---------------------------------------------------------------------------

def A1_audio_duration_drift() -> EnvironmentSimulationConfig:
    """TTS generates audio with 35% duration drift → audio gatekeeper rejects
    → L0 agent rewrites narration text (up to 5 attempts)."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(
                        injected_response=_tts_short_duration(
                            target_duration=39.0, drift_factor=0.65,
                        ),
                    ),
                ],
            ),
            # Other tools succeed normally
            ToolSimulationConfig(
                tool_name="generate_video_clip",
                injection_configs=[
                    InjectionConfig(injected_response=_video_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(injected_response=_provision_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
        ],
    )


def A2_tts_worker_unreachable() -> EnvironmentSimulationConfig:
    """TTS worker returns 503 → escalation L0 retry → L1 different worker."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(
                        injected_error=InjectedError(
                            injected_http_error_code=503,
                            error_message="TTS worker unavailable: connection refused",
                        ),
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(injected_response=_provision_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
        ],
    )


def A3_tts_empty_wav() -> EnvironmentSimulationConfig:
    """TTS returns success but WAV has 0 bytes → audio clip OTIO violation."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(
                        injected_response={
                            "status": "generated",
                            "mode": "simulated_corrupt",
                            "wav_path": f"{_AUDIO_DIR}/scene_001_V1.wav",
                            "duration": 0.0,
                            "sample_rate": _SAMPLE_RATE,
                            "text_length": 200,
                            "word_count": 40,
                        },
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
        ],
    )


def A4_no_gpu_worker() -> EnvironmentSimulationConfig:
    """No GPU worker available → video_worker_missing escalation → full L0-L3 ladder."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_video_clip",
                injection_configs=[
                    InjectionConfig(
                        injected_error=InjectedError(
                            injected_http_error_code=503,
                            error_message="No video worker URL configured",
                        ),
                    ),
                ],
            ),
            # TTS and timeline succeed
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(injected_response=_provision_no_offers()),
                ],
            ),
        ],
    )


def A5_cuda_oom() -> EnvironmentSimulationConfig:
    """CUDA OOM during video generation → L0 reduce resolution → L1 retry."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_video_clip",
                injection_configs=[
                    InjectionConfig(injected_response=_video_cuda_oom()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(injected_response=_provision_success()),
                ],
            ),
        ],
    )


def A6_video_timeout() -> EnvironmentSimulationConfig:
    """GPU worker timeout → L1 retry with longer timeout."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_video_clip",
                injection_configs=[
                    InjectionConfig(injected_response=_video_timeout()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(injected_response=_provision_success()),
                ],
            ),
        ],
    )


def A7_video_qa_black_frames() -> EnvironmentSimulationConfig:
    """Video QA rejects for black frames → L0 re-seed → L1 adjust params."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_video_clip",
                injection_configs=[
                    InjectionConfig(
                        injected_response=_video_qa_rejected("black frames detected in 80% of clip"),
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(injected_response=_provision_success()),
                ],
            ),
        ],
    )


def A8_video_qa_style_mismatch() -> EnvironmentSimulationConfig:
    """Video QA rejects for style mismatch → L0 adjust prompt → L2 different LoRA."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_video_clip",
                injection_configs=[
                    InjectionConfig(
                        injected_response=_video_qa_rejected(
                            "visual style does not match documentary-realism: "
                            "detected cartoon/anime aesthetic"
                        ),
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(injected_response=_provision_success()),
                ],
            ),
        ],
    )


def A9_no_gpu_offers() -> EnvironmentSimulationConfig:
    """Vast.ai returns no GPU offers → L0 try different tier → L3 wait."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(injected_response=_provision_no_offers()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
        ],
    )


def A10_insufficient_credits() -> EnvironmentSimulationConfig:
    """Vast.ai insufficient credits → L4 human escalation (tops up balance)."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(
                        injected_response=_provision_insufficient_credits(),
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
        ],
    )


def A11_whisperx_failure() -> EnvironmentSimulationConfig:
    """WhisperX alignment crashes → synthetic fallback."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(
                        injected_error=InjectedError(
                            injected_http_error_code=500,
                            error_message="WhisperX CUDA error: no GPU available for alignment",
                        ),
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
        ],
    )


def A12_corrupt_timeline() -> EnvironmentSimulationConfig:
    """create_timeline returns invalid OTIO → Timeline Guardian escalation."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(
                        injected_response={
                            "status": "error",
                            "error": "OTIO serialization failed: invalid track structure",
                        },
                    ),
                ],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Category B: Gatekeeper Failures
# ---------------------------------------------------------------------------

def B1_audio_gatekeeper_moderate_drift() -> EnvironmentSimulationConfig:
    """Audio gatekeeper sees 15-30% drift — L0 agent rewrites narration ×5."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(
                        injected_response=_tts_short_duration(
                            target_duration=39.0, drift_factor=0.78,
                        ),
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success(word_count=30)),
                ],
            ),
        ],
    )


def B2_audio_gatekeeper_missing_voice() -> EnvironmentSimulationConfig:
    """Audio gatekeeper detects missing voice track — L0 regenerates."""
    # Simulate V1 succeeding but V2 failing (via match_args)
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    # V1 succeeds
                    InjectionConfig(
                        match_args={"voice_role": "V1"},
                        injected_response=_tts_success(voice_role="V1"),
                    ),
                    # V2 fails
                    InjectionConfig(
                        match_args={"voice_role": "V2"},
                        injected_error=InjectedError(
                            injected_http_error_code=500,
                            error_message="TTS voice V2 model not loaded",
                        ),
                    ),
                    # V3 succeeds
                    InjectionConfig(
                        match_args={"voice_role": "V3"},
                        injected_response=_tts_success(voice_role="V3"),
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
        ],
    )


def B3_production_handoff_incomplete_concepts() -> EnvironmentSimulationConfig:
    """Production handoff gatekeeper blocks — visual concepts incomplete."""
    # Visual concepter returns partial data via LLM mock strategy
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
            # LoRA tools use LLM-generated mocks
            ToolSimulationConfig(
                tool_name="query_lora_catalog",
                mock_strategy_type=MockStrategy.MOCK_STRATEGY_TOOL_SPEC,
            ),
            ToolSimulationConfig(
                tool_name="get_lora_details",
                mock_strategy_type=MockStrategy.MOCK_STRATEGY_TOOL_SPEC,
            ),
        ],
    )


def B4_production_gatekeeper_low_qa() -> EnvironmentSimulationConfig:
    """Production gatekeeper rejects — <50% clip QA pass rate."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_video_clip",
                injection_configs=[
                    # 50% of clips get QA rejected
                    InjectionConfig(
                        injection_probability=0.5,
                        random_seed=42,
                        injected_response=_video_qa_rejected("visual quality below threshold"),
                    ),
                    # The rest succeed
                    InjectionConfig(
                        injected_response=_video_success(),
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(injected_response=_provision_success()),
                ],
            ),
        ],
    )


def B5_assembly_missing_clips() -> EnvironmentSimulationConfig:
    """Assembly handoff gatekeeper blocks — video clips missing for some scenes."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_video_clip",
                injection_configs=[
                    # 30% of clips fail outright
                    InjectionConfig(
                        injection_probability=0.3,
                        random_seed=99,
                        injected_error=InjectedError(
                            injected_http_error_code=500,
                            error_message="GPU worker crashed during generation",
                        ),
                    ),
                    # Rest succeed
                    InjectionConfig(
                        injected_response=_video_success(),
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(injected_response=_provision_success()),
                ],
            ),
        ],
    )


def B6_assembly_otio_violation() -> EnvironmentSimulationConfig:
    """Assembly handoff gatekeeper detects OTIO structural violations."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(
                        injected_response={
                            "status": "created",
                            "timeline_path": "/tmp/documentary-pipeline/timelines/test.otio",
                            "num_scenes": 5,
                            "tracks": ["V1 Video"],  # Missing A1/A2 tracks
                            "warning": "Incomplete track structure",
                        },
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Category C: Timeline Guardian Failures
# ---------------------------------------------------------------------------

def C1_timeline_not_found() -> EnvironmentSimulationConfig:
    """Timeline file missing after audio phase → Guardian escalation."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(
                        injected_response={
                            "status": "created",
                            "timeline_path": "/tmp/nonexistent/timeline.otio",
                            "num_scenes": 5,
                            "tracks": ["V1 Video", "A1 Narration", "A2 Music"],
                        },
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
        ],
    )


def C2_overlapping_audio_clips() -> EnvironmentSimulationConfig:
    """Overlapping audio clips in OTIO after audio phase."""
    # This is triggered by alignment data that produces overlapping regions
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(
                        injected_response={
                            "status": "aligned",
                            "mode": "simulated_overlap",
                            "wav_path": f"{_AUDIO_DIR}/scene_001_V1.wav",
                            "words": [
                                {"word": "test", "start": 0.0, "end": 5.0},
                                {"word": "overlap", "start": 3.0, "end": 8.0},
                            ],
                            "total_duration": 8.0,
                            "word_count": 2,
                        },
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
        ],
    )


def C3_video_gaps() -> EnvironmentSimulationConfig:
    """Gaps between video clips post-production → Guardian detects."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_video_clip",
                injection_configs=[
                    # Return shorter duration than expected, creating gaps
                    InjectionConfig(
                        injected_response={
                            **_video_success(),
                            "actual_duration": 2.0,  # Much shorter than needed
                        },
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(injected_response=_provision_success()),
                ],
            ),
        ],
    )


def C4_invalid_track_structure() -> EnvironmentSimulationConfig:
    """Invalid track structure post-assembly → Guardian escalation."""
    # Same as C1 but targets assembly phase
    return C1_timeline_not_found()


# ---------------------------------------------------------------------------
# Category D: Systemic / Fleet Failures
# ---------------------------------------------------------------------------

def D1_cascade_failure() -> EnvironmentSimulationConfig:
    """3+ sequential video generation failures → fleet coordinator cascade detection."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_video_clip",
                injection_configs=[
                    # First 3 clips all fail (cascade)
                    InjectionConfig(
                        injection_probability=1.0,
                        injected_error=InjectedError(
                            injected_http_error_code=500,
                            error_message="GPU worker segfault: SIGSEGV in libcuda.so",
                        ),
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(injected_response=_provision_success()),
                ],
            ),
        ],
    )


def D2_common_error() -> EnvironmentSimulationConfig:
    """Same CUDA OOM error on 60%+ of clips → pattern detection."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_video_clip",
                injection_configs=[
                    InjectionConfig(
                        injection_probability=0.65,
                        random_seed=123,
                        injected_response=_video_cuda_oom(),
                    ),
                    InjectionConfig(
                        injected_response=_video_success(),
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(injected_response=_provision_success()),
                ],
            ),
        ],
    )


def D3_performance_degradation() -> EnvironmentSimulationConfig:
    """GPU generation time 3× baseline → throttle detection."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_video_clip",
                injection_configs=[
                    InjectionConfig(
                        injected_latency_seconds=10.0,  # Simulate slow generation
                        injected_response=_video_success(),
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(injected_response=_provision_success()),
                ],
            ),
        ],
    )


def D4_poison_clip() -> EnvironmentSimulationConfig:
    """One specific clip always fails → dead-letter queue.

    Uses match_args to target scene 3 phrase 2 specifically.
    """
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_video_clip",
                injection_configs=[
                    # Scene 3 always fails
                    InjectionConfig(
                        match_args={"lora_id": "documentary-realism"},
                        injection_probability=0.2,
                        random_seed=42,
                        injected_error=InjectedError(
                            injected_http_error_code=500,
                            error_message="Poison clip: generation diverges to NaN at step 15",
                        ),
                    ),
                    # Rest succeed
                    InjectionConfig(
                        injected_response=_video_success(),
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(injected_response=_provision_success()),
                ],
            ),
        ],
    )


def D5_budget_burn() -> EnvironmentSimulationConfig:
    """Cost exceeds 80% of budget ceiling → fleet coordinator pause."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(
                        injected_response={
                            **_provision_success(),
                            "cost_per_hour": 5.00,
                            "warning": "High cost instance — budget burn risk",
                        },
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_video_clip",
                injection_configs=[
                    InjectionConfig(injected_response=_video_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Category E: Happy Paths
# ---------------------------------------------------------------------------

def E1_full_success() -> EnvironmentSimulationConfig:
    """Baseline — all tools return valid simulated data.  No failures."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_video_clip",
                injection_configs=[
                    InjectionConfig(injected_response=_video_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(injected_response=_provision_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
        ],
    )


def E2_resume_from_checkpoint() -> EnvironmentSimulationConfig:
    """Resume — stage markers exist for scenario+audio, only production onwards runs."""
    # Same as E1 but the runner should set up B2 stage markers before starting
    return E1_full_success()


# ---------------------------------------------------------------------------
# Category F: Edge Cases
# ---------------------------------------------------------------------------

def F1_malformed_json_scene_num() -> EnvironmentSimulationConfig:
    """LLM returns string scene_num instead of int — tests _safe_int()."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(
                        injected_response={
                            "status": "generated",
                            "mode": "simulated",
                            "wav_path": f"{_AUDIO_DIR}/scene_001_V1.wav",
                            "duration": 39.0,
                            "sample_rate": _SAMPLE_RATE,
                            "text_length": 200,
                            "word_count": 40,
                            "scene_num": "1",  # String instead of int
                        },
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
        ],
    )


def F2_partial_success() -> EnvironmentSimulationConfig:
    """3/5 video clips succeed, 2/5 fail → mixed recovery."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_video_clip",
                injection_configs=[
                    # 40% fail
                    InjectionConfig(
                        injection_probability=0.4,
                        random_seed=77,
                        injected_error=InjectedError(
                            injected_http_error_code=500,
                            error_message="GPU worker error: mixed failure scenario",
                        ),
                    ),
                    # Rest succeed
                    InjectionConfig(
                        injected_response=_video_success(),
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(injected_response=_provision_success()),
                ],
            ),
        ],
    )


def F3_agent_abort() -> EnvironmentSimulationConfig:
    """All tools return errors → every escalation agent aborts → clean pipeline stop."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(
                        injected_error=InjectedError(
                            injected_http_error_code=500,
                            error_message="FATAL: TTS model corrupted, cannot recover",
                        ),
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
        ],
    )


def F4_agents_exhausted_human_escalation() -> EnvironmentSimulationConfig:
    """All L0-L3 agents fail to resolve → escalation reaches L4 (human).

    This is the same as D1 (cascade) but with DOCUMENTARY_AUTO_APPROVE=false
    so the pipeline actually pauses for human input.
    """
    return D1_cascade_failure()


def F5_flaky_failures() -> EnvironmentSimulationConfig:
    """50% injection probability on video generation — tests retry resilience."""
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="generate_video_clip",
                injection_configs=[
                    InjectionConfig(
                        injection_probability=0.5,
                        random_seed=55,
                        injected_error=InjectedError(
                            injected_http_error_code=500,
                            error_message="Flaky GPU worker: intermittent failure",
                        ),
                    ),
                    InjectionConfig(
                        injected_response=_video_success(),
                    ),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(injected_response=_provision_success()),
                ],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Category G: GPU Provisioning Failures
# ---------------------------------------------------------------------------
# These scenarios exercise the WorkerProvisioner's retry logic and the
# escalation ladder for infrastructure-level failures.  They inject responses
# into ``vast_provision_lifecycle`` — the simulatable wrapper around the full
# VM provisioning lifecycle (offer search → create → boot → SSH → health).
#
# Each scenario specifies different failure reasons via ``error_category``
# (matching the categories in WorkerSpec.bootstrap_error_category) and
# ``stage`` (which provisioning step failed).
#
# The provisioner retries up to 2 times (3 attempts total) with excluded
# offers.  Scenarios G1-G4 fail on attempt 0 and succeed on attempt 1,
# testing the retry path.  G5 fails on all attempts, testing the full
# escalation ladder.

def _provision_lifecycle_success(
    role: str = "video",
    gpu_type: str = "A100_SXM4",
    vm_id: str = "sim-vm-001",
) -> dict:
    """Mock successful provisioning lifecycle."""
    return {
        "status": "healthy",
        "vm_id": vm_id,
        "gpu_type": gpu_type,
        "role": role,
        "message": f"{role} worker healthy after provisioning",
    }


def _provision_lifecycle_failure(
    error: str,
    error_category: str,
    stage: str,
) -> dict:
    """Mock failed provisioning lifecycle at a specific stage."""
    return {
        "status": "error",
        "error": error,
        "error_category": error_category,
        "stage": stage,
    }


def _g_common_tools() -> list:
    """Common tool configs for G scenarios — all non-provisioning tools succeed."""
    return [
        ToolSimulationConfig(
            tool_name="generate_narration",
            injection_configs=[
                InjectionConfig(injected_response=_tts_success()),
            ],
        ),
        ToolSimulationConfig(
            tool_name="generate_video_clip",
            injection_configs=[
                InjectionConfig(injected_response=_video_success()),
            ],
        ),
        ToolSimulationConfig(
            tool_name="align_narration",
            injection_configs=[
                InjectionConfig(injected_response=_alignment_success()),
            ],
        ),
        ToolSimulationConfig(
            tool_name="create_timeline",
            injection_configs=[
                InjectionConfig(injected_response=_timeline_success()),
            ],
        ),
        ToolSimulationConfig(
            tool_name="provision_gpu_vm",
            injection_configs=[
                InjectionConfig(injected_response=_provision_success()),
            ],
        ),
    ]


def G1_bootstrap_network_failure() -> EnvironmentSimulationConfig:
    """Host bootstrap fails (apt-get can't reach repos) → retry on different host → succeeds.

    Simulates the exact failure we hit in production: VM boots, SSH connects,
    but ``apt-get update`` fails because the host can't resolve
    ``archive.ubuntu.com``.  The provisioner destroys the VM and retries on
    a different host, which succeeds.
    """
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="vast_provision_lifecycle",
                injection_configs=[
                    # Attempt 0 (video): bootstrap network failure
                    InjectionConfig(
                        match_args={"role": "video", "attempt": 0},
                        injected_response=_provision_lifecycle_failure(
                            error=(
                                "apt-get update failed: "
                                "Err:1 http://archive.ubuntu.com/ubuntu jammy InRelease "
                                "Could not resolve 'archive.ubuntu.com'"
                            ),
                            error_category="network",
                            stage="bootstrap",
                        ),
                    ),
                    # Attempt 1 (video): success on different host
                    InjectionConfig(
                        match_args={"role": "video", "attempt": 1},
                        injected_response=_provision_lifecycle_success(
                            role="video", gpu_type="A100_SXM4", vm_id="sim-vm-002",
                        ),
                    ),
                    # TTS: always succeeds
                    InjectionConfig(
                        match_args={"role": "tts"},
                        injected_response=_provision_lifecycle_success(
                            role="tts", gpu_type="RTX_4000", vm_id="sim-tts-001",
                        ),
                    ),
                ],
            ),
            *_g_common_tools(),
        ],
    )


def G2_ssh_key_rejection() -> EnvironmentSimulationConfig:
    """SSH proxy rejects all identity files → retry on different host → succeeds.

    Vast.ai proxy hosts sometimes reject SSH keys that work on other hosts.
    The provisioner should destroy the VM and try a different offer.
    """
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="vast_provision_lifecycle",
                injection_configs=[
                    # Attempt 0 (video): SSH rejection
                    InjectionConfig(
                        match_args={"role": "video", "attempt": 0},
                        injected_response=_provision_lifecycle_failure(
                            error=(
                                "SSH proxy rejected all identity files after 5 "
                                "attempts (tried: id_ed25519, id_rsa, id_ecdsa). "
                                "Host: ssh5.vast.ai:23894"
                            ),
                            error_category="auth",
                            stage="ssh_tunnel",
                        ),
                    ),
                    # Attempt 1 (video): success on different host
                    InjectionConfig(
                        match_args={"role": "video", "attempt": 1},
                        injected_response=_provision_lifecycle_success(
                            role="video", gpu_type="A100_SXM4", vm_id="sim-vm-003",
                        ),
                    ),
                    # TTS: always succeeds
                    InjectionConfig(
                        match_args={"role": "tts"},
                        injected_response=_provision_lifecycle_success(
                            role="tts", gpu_type="RTX_4000", vm_id="sim-tts-001",
                        ),
                    ),
                ],
            ),
            *_g_common_tools(),
        ],
    )


def G3_docker_image_pull_timeout() -> EnvironmentSimulationConfig:
    """Docker image pull stalls (slow host) → retry on faster host → succeeds.

    Large Docker images (pytorch 12GB+) can stall on hosts with slow
    internet.  The provisioner detects the loading timeout and tries a
    host with better download speed.
    """
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="vast_provision_lifecycle",
                injection_configs=[
                    # Attempt 0 (video): image pull timeout
                    InjectionConfig(
                        match_args={"role": "video", "attempt": 0},
                        injected_response=_provision_lifecycle_failure(
                            error=(
                                "VM stuck in 'loading' state after 300s "
                                "(Docker image pull: pytorch/pytorch:2.10.0-cuda12.6-cudnn9-devel). "
                                "Host inet_down=45 Mbps — too slow for 12GB image."
                            ),
                            error_category="timeout",
                            stage="vm_boot",
                        ),
                    ),
                    # Attempt 1 (video): success on faster host
                    InjectionConfig(
                        match_args={"role": "video", "attempt": 1},
                        injected_response=_provision_lifecycle_success(
                            role="video", gpu_type="A100_SXM4", vm_id="sim-vm-004",
                        ),
                    ),
                    # TTS: always succeeds
                    InjectionConfig(
                        match_args={"role": "tts"},
                        injected_response=_provision_lifecycle_success(
                            role="tts", gpu_type="RTX_4000", vm_id="sim-tts-001",
                        ),
                    ),
                ],
            ),
            *_g_common_tools(),
        ],
    )


def G4_cuda_oom_model_load() -> EnvironmentSimulationConfig:
    """CUDA OOM during model load (GPU VRAM too small) → retry with larger GPU → succeeds.

    The offer search filter passed but the actual GPU has slightly less
    usable VRAM than advertised (driver overhead, ECC).  The LTX-2.3
    transformer (46GB bf16) doesn't fit.  The provisioner retries and
    gets a host with more headroom.
    """
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="vast_provision_lifecycle",
                injection_configs=[
                    # Attempt 0 (video): CUDA OOM during model load
                    InjectionConfig(
                        match_args={"role": "video", "attempt": 0},
                        injected_response=_provision_lifecycle_failure(
                            error=(
                                "RuntimeError: CUDA out of memory. "
                                "Tried to allocate 46.60 GiB. "
                                "GPU 0 has a total capacity of 47.54 GiB of which "
                                "44.22 GiB is free. "
                                "Process peak memory: 3.32 GiB."
                            ),
                            error_category="runtime",
                            stage="model_load",
                        ),
                    ),
                    # Attempt 1 (video): success on host with more VRAM
                    InjectionConfig(
                        match_args={"role": "video", "attempt": 1},
                        injected_response=_provision_lifecycle_success(
                            role="video", gpu_type="H100_SXM5", vm_id="sim-vm-005",
                        ),
                    ),
                    # TTS: always succeeds
                    InjectionConfig(
                        match_args={"role": "tts"},
                        injected_response=_provision_lifecycle_success(
                            role="tts", gpu_type="RTX_4000", vm_id="sim-tts-001",
                        ),
                    ),
                ],
            ),
            *_g_common_tools(),
        ],
    )


def G5_all_hosts_fail_escalation() -> EnvironmentSimulationConfig:
    """All provisioning attempts fail → full escalation ladder activates.

    Three different hosts fail for three different reasons.  After all
    retries are exhausted, the provisioner raises RuntimeError, which
    propagates through the pipeline and triggers the full agent-powered
    escalation ladder (L0 → L1 → L2 → L3 → L4 human).

    The escalation agents also have access to ``provision_gpu_vm`` (ADK tool)
    but it returns no_offers — the market is depleted.
    """
    return EnvironmentSimulationConfig(
        tool_simulation_configs=[
            ToolSimulationConfig(
                tool_name="vast_provision_lifecycle",
                injection_configs=[
                    # Attempt 0 (video): network failure
                    InjectionConfig(
                        match_args={"role": "video", "attempt": 0},
                        injected_response=_provision_lifecycle_failure(
                            error=(
                                "apt-get update failed: "
                                "Temporary failure resolving 'archive.ubuntu.com'"
                            ),
                            error_category="network",
                            stage="bootstrap",
                        ),
                    ),
                    # Attempt 1 (video): SSH failure
                    InjectionConfig(
                        match_args={"role": "video", "attempt": 1},
                        injected_response=_provision_lifecycle_failure(
                            error="SSH connection refused by proxy host ssh8.vast.ai:29104",
                            error_category="auth",
                            stage="ssh_tunnel",
                        ),
                    ),
                    # Attempt 2 (video): CUDA OOM
                    InjectionConfig(
                        match_args={"role": "video", "attempt": 2},
                        injected_response=_provision_lifecycle_failure(
                            error=(
                                "RuntimeError: CUDA out of memory. "
                                "Tried to allocate 46.60 GiB."
                            ),
                            error_category="runtime",
                            stage="model_load",
                        ),
                    ),
                    # TTS: always succeeds (only video provisioning fails)
                    InjectionConfig(
                        match_args={"role": "tts"},
                        injected_response=_provision_lifecycle_success(
                            role="tts", gpu_type="RTX_4000", vm_id="sim-tts-001",
                        ),
                    ),
                ],
            ),
            # Escalation agents try provision_gpu_vm (ADK tool) — also fails
            ToolSimulationConfig(
                tool_name="provision_gpu_vm",
                injection_configs=[
                    InjectionConfig(injected_response=_provision_no_offers()),
                ],
            ),
            # All other tools succeed (pipeline gets past audio/visual to production)
            ToolSimulationConfig(
                tool_name="generate_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_tts_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="generate_video_clip",
                injection_configs=[
                    InjectionConfig(injected_response=_video_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="align_narration",
                injection_configs=[
                    InjectionConfig(injected_response=_alignment_success()),
                ],
            ),
            ToolSimulationConfig(
                tool_name="create_timeline",
                injection_configs=[
                    InjectionConfig(injected_response=_timeline_success()),
                ],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Scenario Registry
# ---------------------------------------------------------------------------

_SCENARIOS: Dict[str, Callable] = {
    # Category A: Tool-level failures → escalation
    "A1": A1_audio_duration_drift,
    "A2": A2_tts_worker_unreachable,
    "A3": A3_tts_empty_wav,
    "A4": A4_no_gpu_worker,
    "A5": A5_cuda_oom,
    "A6": A6_video_timeout,
    "A7": A7_video_qa_black_frames,
    "A8": A8_video_qa_style_mismatch,
    "A9": A9_no_gpu_offers,
    "A10": A10_insufficient_credits,
    "A11": A11_whisperx_failure,
    "A12": A12_corrupt_timeline,
    # Category B: Gatekeeper failures
    "B1": B1_audio_gatekeeper_moderate_drift,
    "B2": B2_audio_gatekeeper_missing_voice,
    "B3": B3_production_handoff_incomplete_concepts,
    "B4": B4_production_gatekeeper_low_qa,
    "B5": B5_assembly_missing_clips,
    "B6": B6_assembly_otio_violation,
    # Category C: Timeline Guardian failures
    "C1": C1_timeline_not_found,
    "C2": C2_overlapping_audio_clips,
    "C3": C3_video_gaps,
    "C4": C4_invalid_track_structure,
    # Category D: Systemic / fleet failures
    "D1": D1_cascade_failure,
    "D2": D2_common_error,
    "D3": D3_performance_degradation,
    "D4": D4_poison_clip,
    "D5": D5_budget_burn,
    # Category E: Happy paths
    "E1": E1_full_success,
    "E2": E2_resume_from_checkpoint,
    # Category F: Edge cases
    "F1": F1_malformed_json_scene_num,
    "F2": F2_partial_success,
    "F3": F3_agent_abort,
    "F4": F4_agents_exhausted_human_escalation,
    "F5": F5_flaky_failures,
    # Category G: GPU provisioning failures
    "G1": G1_bootstrap_network_failure,
    "G2": G2_ssh_key_rejection,
    "G3": G3_docker_image_pull_timeout,
    "G4": G4_cuda_oom_model_load,
    "G5": G5_all_hosts_fail_escalation,
}

# Also register by full function name for convenience
for _key, _fn in list(_SCENARIOS.items()):
    _SCENARIOS[_fn.__name__] = _fn


# Type alias for callable returning config
Callable = type(A1_audio_duration_drift)


def get_scenario(name: str) -> EnvironmentSimulationConfig:
    """Get a simulation config by scenario ID or function name.

    Args:
        name: Scenario ID (e.g. "A1") or function name (e.g. "A1_audio_duration_drift").

    Returns:
        EnvironmentSimulationConfig for the scenario.

    Raises:
        KeyError: If the scenario name is not found.
    """
    if name not in _SCENARIOS:
        available = ", ".join(sorted(k for k in _SCENARIOS if len(k) <= 3))
        raise KeyError(f"Unknown scenario '{name}'. Available: {available}")
    return _SCENARIOS[name]()


def list_scenarios() -> list[dict]:
    """List all available scenarios with ID, name, and description."""
    result = []
    seen = set()
    for key, fn in sorted(_SCENARIOS.items()):
        if fn in seen or len(key) > 3:
            continue
        seen.add(fn)
        result.append({
            "id": key,
            "name": fn.__name__,
            "description": (fn.__doc__ or "").strip().split("\n")[0],
        })
    return result
