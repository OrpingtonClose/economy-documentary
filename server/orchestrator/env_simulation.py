"""
Environment simulation — mock GPU workers for testing without real Vast.ai costs.

Uses ADK's ``before_tool_callback`` pattern to intercept GPU calls and return
synthetic results.  The agent doesn't know it's being tested.

Configuration is loaded from ``eval_config/sampler_config.json``:
    {
        "environment_simulation": {
            "enabled": true,
            "mock_gpu_workers": true,
            "mock_responses": {
                "video_generation": {
                    "success_rate": 0.85,
                    "avg_gen_time_sec": 180,
                    "qa_pass_rate": 0.80
                }
            },
            "error_injection": {
                "probability": 0.10,
                "types": ["timeout", "cuda_oom", "black_frame"]
            }
        }
    }

Usage:
    # In a test or eval runner:
    from orchestrator.env_simulation import SimulationCallbacks

    sim = SimulationCallbacks.from_config()
    # Wire into ADK agent:
    agent.before_tool_callback = sim.before_tool_callback
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

EVAL_CONFIG_DIR = Path(__file__).parent / "eval_config"


class SimulationConfig:
    """Parsed simulation configuration."""

    def __init__(
        self,
        enabled: bool = False,
        mock_gpu_workers: bool = False,
        success_rate: float = 0.85,
        avg_gen_time_sec: float = 180.0,
        qa_pass_rate: float = 0.80,
        error_probability: float = 0.10,
        error_types: Optional[list[str]] = None,
        latency_multiplier: float = 0.01,  # simulate fast (1% of real time)
    ) -> None:
        self.enabled = enabled
        self.mock_gpu_workers = mock_gpu_workers
        self.success_rate = success_rate
        self.avg_gen_time_sec = avg_gen_time_sec
        self.qa_pass_rate = qa_pass_rate
        self.error_probability = error_probability
        self.error_types = error_types or ["timeout", "cuda_oom", "black_frame"]
        self.latency_multiplier = latency_multiplier

    @classmethod
    def from_file(cls, path: Optional[str] = None) -> SimulationConfig:
        """Load configuration from sampler_config.json."""
        config_path = Path(path) if path else EVAL_CONFIG_DIR / "sampler_config.json"
        if not config_path.exists():
            return cls()

        with open(config_path) as f:
            data = json.load(f)

        sim = data.get("environment_simulation", {})
        mock = sim.get("mock_responses", {}).get("video_generation", {})
        errors = sim.get("error_injection", {})

        return cls(
            enabled=sim.get("enabled", False),
            mock_gpu_workers=sim.get("mock_gpu_workers", False),
            success_rate=mock.get("success_rate", 0.85),
            avg_gen_time_sec=mock.get("avg_gen_time_sec", 180.0),
            qa_pass_rate=mock.get("qa_pass_rate", 0.80),
            error_probability=errors.get("probability", 0.10),
            error_types=errors.get("types", ["timeout", "cuda_oom", "black_frame"]),
        )


class SimulationCallbacks:
    """ADK before_tool_callback that intercepts GPU calls with mocks.

    Intercepts calls to video generation tools and returns synthetic
    results based on configured success rates and error injection.
    """

    # Tool names that should be intercepted
    GPU_TOOL_NAMES = {
        "generate_video_clip",
        "call_gpu_worker",
        "_call_gpu_worker",
    }

    def __init__(self, config: SimulationConfig) -> None:
        self._config = config
        self._call_count = 0
        self._success_count = 0
        self._failure_count = 0
        self._start_time = time.time()

    @classmethod
    def from_config(cls, path: Optional[str] = None) -> SimulationCallbacks:
        config = SimulationConfig.from_file(path)
        return cls(config)

    def before_tool_callback(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        **kwargs: Any,
    ) -> Optional[dict[str, Any]]:
        """Intercept GPU tool calls with mock responses.

        Returns None for non-GPU tools (let them execute normally).
        Returns a mock result dict for GPU tools.
        """
        if not self._config.enabled or not self._config.mock_gpu_workers:
            return None

        if tool_name not in self.GPU_TOOL_NAMES:
            return None

        self._call_count += 1

        # Simulate latency (compressed time)
        sim_delay = self._config.avg_gen_time_sec * self._config.latency_multiplier
        time.sleep(sim_delay)

        # Error injection
        if random.random() < self._config.error_probability:
            error_type = random.choice(self._config.error_types)
            self._failure_count += 1
            logger.info(
                "SimulationCallbacks: injecting %s error for call #%d",
                error_type, self._call_count,
            )
            return self._mock_error(error_type, tool_args)

        # Success / QA outcome
        if random.random() < self._config.success_rate:
            qa_passed = random.random() < self._config.qa_pass_rate
            self._success_count += 1
            return self._mock_success(tool_args, qa_passed)
        else:
            self._failure_count += 1
            return self._mock_error("generation_failed", tool_args)

    def _mock_success(
        self, tool_args: dict, qa_passed: bool
    ) -> dict[str, Any]:
        """Generate a mock successful video generation result."""
        gen_time = self._config.avg_gen_time_sec * random.uniform(0.7, 1.3)
        return {
            "success": True,
            "gen_time": round(gen_time, 2),
            "qa_quality": "good" if qa_passed else "rejected",
            "qa_reason": (
                "Mock QA: clip meets quality threshold"
                if qa_passed
                else "Mock QA: visual quality below threshold (simulated)"
            ),
            "qa_attempts": 1,
            "qa_seed": random.randint(1, 999999),
            "output_path": f"/tmp/sim_clip_{self._call_count}.mp4",
            "simulated": True,
        }

    def _mock_error(
        self, error_type: str, tool_args: dict
    ) -> dict[str, Any]:
        """Generate a mock error response."""
        error_messages = {
            "timeout": "CUDA operation timed out after 600s",
            "cuda_oom": "CUDA out of memory. Tried to allocate 4.00 GiB",
            "black_frame": "QA REJECTED: visual quality below threshold. QA_HINTS: black frames detected",
            "generation_failed": "Video generation failed: unexpected error",
        }
        return {
            "success": False,
            "error": error_messages.get(error_type, f"Simulated error: {error_type}"),
            "error_type": error_type,
            "simulated": True,
        }

    def get_summary(self) -> dict[str, Any]:
        """Return summary of simulation results."""
        elapsed = time.time() - self._start_time
        return {
            "total_calls": self._call_count,
            "successes": self._success_count,
            "failures": self._failure_count,
            "success_rate": (
                self._success_count / self._call_count
                if self._call_count > 0
                else 0
            ),
            "elapsed_sec": round(elapsed, 1),
            "config": {
                "target_success_rate": self._config.success_rate,
                "target_qa_pass_rate": self._config.qa_pass_rate,
                "error_probability": self._config.error_probability,
            },
        }


def is_simulation_enabled() -> bool:
    """Check if environment simulation is enabled (env var or config)."""
    if os.environ.get("SIMULATION_MODE", "").strip().lower() in ("1", "true"):
        return True
    config = SimulationConfig.from_file()
    return config.enabled


def get_simulation_callbacks() -> Optional[SimulationCallbacks]:
    """Get simulation callbacks if simulation is enabled, else None."""
    if not is_simulation_enabled():
        return None
    return SimulationCallbacks.from_config()
