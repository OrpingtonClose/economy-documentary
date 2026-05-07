"""Preflight stage — verify all pipeline resources before any work begins.

Runs as stage 0, before scenario. Checks every external dependency the
pipeline needs: API keys, worker URLs, model access, disk space, write
permissions. If anything is missing, the pipeline reports exactly what
and stops. No side-channel bash commands. No guessing.

This is a deterministic tool, not an LLM agent. It does not need a
model. It runs checks and returns a structured report.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Check definitions
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str = ""


@dataclass
class PreflightReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [
                {"name": c.name, "passed": c.passed, "message": c.message}
                for c in self.checks
            ],
            "failures": [
                {"name": c.name, "message": c.message}
                for c in self.failures
            ],
        }


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_env_var(name: str, description: str) -> CheckResult:
    """Check that an environment variable is set and non-empty."""
    value = os.environ.get(name, "")
    if value:
        # Mask secrets: show first 4 chars + ...
        masked = value[:4] + "..." if len(value) > 8 else "***"
        return CheckResult(name=name, passed=True, message=f"{description}: {masked}")
    return CheckResult(name=name, passed=False, message=f"Missing: {description}")


def _check_writable_dir(path: str, description: str) -> CheckResult:
    """Check that a directory exists and is writable."""
    name = f"dir:{path}"
    try:
        os.makedirs(path, exist_ok=True)
        test_file = os.path.join(path, ".write_test")
        with open(test_file, "w") as f:
            f.write("ok")
        os.remove(test_file)
        return CheckResult(name=name, passed=True, message=f"{description}: writable")
    except OSError as exc:
        return CheckResult(name=name, passed=False, message=f"{description}: not writable ({exc})")


def _check_disk_space(path: str, min_gb: float, description: str) -> CheckResult:
    """Check available disk space."""
    name = f"disk:{path}"
    try:
        usage = shutil.disk_usage(path if os.path.exists(path) else "/")
        free_gb = usage.free / (1024 ** 3)
        if free_gb >= min_gb:
            return CheckResult(name=name, passed=True, message=f"{description}: {free_gb:.1f} GB free")
        return CheckResult(name=name, passed=False, message=f"{description}: {free_gb:.1f} GB free (need {min_gb})")
    except Exception as exc:
        return CheckResult(name=name, passed=False, message=f"{description}: check failed ({exc})")


def _check_http_reachable(url: str, description: str, timeout: float = 5.0) -> CheckResult:
    """Check that an HTTP endpoint is reachable."""
    import urllib.request
    import urllib.error
    name = f"http:{description}"
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return CheckResult(name=name, passed=True, message=f"{description}: status {resp.status}")
    except Exception as exc:
        return CheckResult(name=name, passed=False, message=f"{description}: unreachable ({exc})")


def _check_python_module(module_name: str, description: str) -> CheckResult:
    """Check that a Python module is importable."""
    name = f"module:{module_name}"
    try:
        __import__(module_name)
        return CheckResult(name=name, passed=True, message=f"{description}: installed")
    except ImportError:
        return CheckResult(name=name, passed=False, message=f"{description}: not installed")


# ---------------------------------------------------------------------------
# Pipeline-specific checks
# ---------------------------------------------------------------------------

def _check_llm_access() -> CheckResult:
    """Check that the LLM API key is set."""
    return _check_env_var("ANTHROPIC_API_KEY", "Anthropic API key (for Claude)")


def _check_video_worker() -> CheckResult:
    """Check that a GPU video worker is available."""
    url = os.environ.get("VIDEO_WORKER_URLS", "")
    if not url:
        return CheckResult(
            name="video_worker",
            passed=False,
            message="No GPU video worker. Set VIDEO_WORKER_URLS or run the provisioner first.",
        )
    return _check_http_reachable(f"{url.rstrip('/')}/health", "GPU video worker")


def _check_tts_engine() -> CheckResult:
    """Check that edge-tts is available."""
    return _check_python_module("edge_tts", "edge-tts (narration TTS)")


def _check_otio() -> CheckResult:
    """Check that OpenTimelineIO is available."""
    return _check_python_module("opentimelineio", "OpenTimelineIO")


def _check_output_dir(output_dir: str) -> CheckResult:
    """Check that the output directory is writable."""
    return _check_writable_dir(output_dir, "Pipeline output directory")


def _check_disk() -> CheckResult:
    """Check that there's enough disk space for video output."""
    return _check_disk_space("/tmp", 5.0, "Disk space for video output")


# ---------------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------------

def run_preflight(output_dir: str = "/tmp/documentary-pipeline") -> PreflightReport:
    """Run all preflight checks and return a structured report.

    This is the single entry point. Call it before the pipeline starts.
    If report.passed is False, the pipeline must not start.
    """
    report = PreflightReport()

    checks = [
        _check_llm_access(),
        _check_video_worker(),
        _check_tts_engine(),
        _check_otio(),
        _check_output_dir(output_dir),
        _check_disk(),
    ]

    for check in checks:
        report.checks.append(check)
        status = "OK" if check.passed else "FAIL"
        logger.info("preflight %s: %s — %s", status, check.name, check.message)

    if report.passed:
        logger.info("preflight: all checks passed")
    else:
        logger.error("preflight: %d check(s) failed:", len(report.failures))
        for f in report.failures:
            logger.error("  %s: %s", f.name, f.message)

    return report


class PreflightError(Exception):
    """Raised when preflight checks fail."""
    def __init__(self, report: PreflightReport):
        self.report = report
        failures = "\n".join(f"  {f.name}: {f.message}" for f in report.failures)
        super().__init__(f"Preflight checks failed:\n{failures}")


# ---------------------------------------------------------------------------
# Agent builder — deterministic, no LLM needed
# ---------------------------------------------------------------------------

def build_preflight_agent(output_dir: str = "/tmp/documentary-pipeline") -> Any:
    """Build the preflight stage agent.

    This is a deterministic agent: it runs checks, returns a report,
    and does not need an LLM. It uses a single tool that runs all
    preflight checks and returns the result.
    """
    from strands import Agent, tool

    @tool
    def verify_pipeline_readiness() -> dict[str, Any]:
        """Run all preflight checks and return a structured report.

        Checks: API keys, GPU worker, TTS engine, OTIO, output dir,
        disk space. If any check fails, the pipeline must not start.
        """
        report = run_preflight(output_dir=output_dir)
        return report.as_dict()

    return Agent(
        name="preflight",
        system_prompt=(
            "You are the preflight stage. Call verify_pipeline_readiness "
            "exactly once. If the report shows any failures, state them "
            "clearly. If all checks pass, confirm readiness."
        ),
        tools=[verify_pipeline_readiness],
    )
