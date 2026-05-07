"""Preflight — verify all pipeline resources before any work begins.

Runs before the graph is built. Checks every external dependency the
pipeline needs: API keys, worker URLs, model access, disk space, write
permissions. If anything is missing, the pipeline reports exactly what
and stops. No side-channel bash commands. No guessing.

Output is structured for projection: JSON, SSE events, and a plain-text
dashboard layout. Any consumer (CLI, web page, dashboard) can render it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
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
    soft: bool = False       # Soft failure: warn but don't block pipeline
    category: str = ""       # Group: "credentials", "workers", "dependencies", "filesystem"
    remedy: str = ""         # What to do to fix it

    @property
    def status(self) -> str:
        if self.passed:
            return "PASS"
        return "WARN" if self.soft else "FAIL"


@dataclass
class PreflightReport:
    checks: list[CheckResult] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    pipeline_id: str = ""

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks if not c.soft)

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    @property
    def hard_failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed and not c.soft]

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed and c.soft]

    def as_dict(self) -> dict[str, Any]:
        return {
            "pipeline_id": self.pipeline_id,
            "passed": self.passed,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round((self.finished_at - self.started_at) * 1000),
            "summary": {
                "total": len(self.checks),
                "passed": sum(1 for c in self.checks if c.passed),
                "failed": len(self.hard_failures),
                "warned": len(self.warnings),
            },
            "checks": [
                {
                    "name": c.name,
                    "category": c.category,
                    "status": c.status,
                    "message": c.message,
                    "remedy": c.remedy,
                }
                for c in self.checks
            ],
            "hard_failures": [
                {"name": c.name, "message": c.message, "remedy": c.remedy}
                for c in self.hard_failures
            ],
            "warnings": [
                {"name": c.name, "message": c.message, "remedy": c.remedy}
                for c in self.warnings
            ],
        }

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2)

    def as_dashboard(self) -> str:
        """Render as a plain-text dashboard suitable for terminal or web."""
        lines = []
        lines.append("╔══════════════════════════════════════════════════════════╗")
        lines.append("║              DOCUMENTARY PIPELINE — PREFLIGHT           ║")
        lines.append("╚══════════════════════════════════════════════════════════╝")
        lines.append("")

        # Group by category
        categories: dict[str, list[CheckResult]] = {}
        for c in self.checks:
            cat = c.category or "other"
            categories.setdefault(cat, []).append(c)

        for cat, checks in categories.items():
            lines.append(f"  {cat.upper()}")
            lines.append(f"  {'─' * 56}")
            for c in checks:
                icon = "✓" if c.passed else ("⚠" if c.soft else "✗")
                lines.append(f"  {icon}  {c.name:<24} {c.message}")
                if not c.passed and c.remedy:
                    lines.append(f"     {'':24} → {c.remedy}")
            lines.append("")

        # Verdict
        if self.passed:
            lines.append("  ┌──────────────────────────────────────────────────────┐")
            lines.append("  │  PREFLIGHT PASSED — pipeline is ready to start        │")
            if self.warnings:
                lines.append(f"  │  ({len(self.warnings)} warning(s) — stages may fail later)    │")
            lines.append("  └──────────────────────────────────────────────────────┘")
        else:
            lines.append("  ┌──────────────────────────────────────────────────────┐")
            lines.append("  │  PREFLIGHT FAILED — pipeline cannot start            │")
            lines.append(f"  │  {len(self.hard_failures)} hard failure(s) must be resolved         │")
            lines.append("  └──────────────────────────────────────────────────────┘")

        lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_env_var(name: str, description: str) -> CheckResult:
    value = os.environ.get(name, "")
    if value:
        masked = value[:4] + "..." if len(value) > 8 else "***"
        return CheckResult(name=name, passed=True, message=f"{description}: {masked}")
    return CheckResult(name=name, passed=False, message=f"Missing: {description}")


def _check_writable_dir(path: str, description: str) -> CheckResult:
    name = f"dir:{path}"
    try:
        os.makedirs(path, exist_ok=True)
        test_file = os.path.join(path, ".write_test")
        with open(test_file, "w") as f:
            f.write("ok")
        os.remove(test_file)
        return CheckResult(name=name, passed=True, message=f"{description}: writable")
    except OSError as exc:
        return CheckResult(
            name=name, passed=False, message=f"{description}: not writable ({exc})",
            remedy=f"Create directory {path} and ensure write permissions",
        )


def _check_disk_space(path: str, min_gb: float, description: str) -> CheckResult:
    name = f"disk:{path}"
    try:
        usage = shutil.disk_usage(path if os.path.exists(path) else "/")
        free_gb = usage.free / (1024 ** 3)
        if free_gb >= min_gb:
            return CheckResult(name=name, passed=True, message=f"{description}: {free_gb:.1f} GB free")
        return CheckResult(
            name=name, passed=False,
            message=f"{description}: {free_gb:.1f} GB free (need {min_gb})",
            remedy=f"Free at least {min_gb:.0f} GB on {path}",
        )
    except Exception as exc:
        return CheckResult(name=name, passed=False, message=f"{description}: check failed ({exc})")


def _check_http_reachable(url: str, description: str, timeout: float = 5.0) -> CheckResult:
    import urllib.request
    import urllib.error
    name = f"http:{description}"
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return CheckResult(name=name, passed=True, message=f"{description}: status {resp.status}")
    except Exception as exc:
        return CheckResult(
            name=name, passed=False, message=f"{description}: unreachable ({exc})",
            remedy=f"Ensure the service at {url} is running and reachable",
        )


def _check_python_module(module_name: str, description: str) -> CheckResult:
    name = f"module:{module_name}"
    try:
        __import__(module_name)
        return CheckResult(name=name, passed=True, message=f"{description}: installed")
    except ImportError:
        return CheckResult(
            name=name, passed=False, message=f"{description}: not installed",
            remedy=f"pip install {module_name}",
        )


# ---------------------------------------------------------------------------
# Pipeline-specific checks
# ---------------------------------------------------------------------------

def _check_llm_access() -> CheckResult:
    """Check that LLM API credentials are available."""
    # AWS Bedrock
    aws_access = os.environ.get("AWS_ACCESS_KEY_ID", "")
    aws_secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    aws_profile = os.environ.get("AWS_PROFILE", "")
    if aws_access and aws_secret:
        return CheckResult(
            name="llm_access", passed=True, category="credentials",
            message="AWS credentials: configured",
        )
    if aws_profile:
        return CheckResult(
            name="llm_access", passed=True, category="credentials",
            message=f"AWS profile: {aws_profile}",
        )
    aws_config = os.path.expanduser("~/.aws/credentials")
    if os.path.exists(aws_config):
        return CheckResult(
            name="llm_access", passed=True, category="credentials",
            message="AWS credentials file: found",
        )

    # Direct Anthropic API
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        return CheckResult(
            name="llm_access", passed=True, category="credentials",
            message="Anthropic API key: configured",
        )

    return CheckResult(
        name="llm_access", passed=False, category="credentials",
        message="No LLM credentials found",
        remedy="Set AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (for Bedrock) or ANTHROPIC_API_KEY",
    )


def _check_video_worker() -> CheckResult:
    """Soft check — GPU worker optional for first 4 stages."""
    url = os.environ.get("VIDEO_WORKER_URLS", "")
    if not url:
        return CheckResult(
            name="video_worker", passed=False, soft=True, category="workers",
            message="No GPU video worker configured",
            remedy="Set VIDEO_WORKER_URLS or let the provisioner allocate one (needs VAST_API_KEY)",
        )
    result = _check_http_reachable(f"{url.rstrip('/')}/health", "GPU video worker")
    result.category = "workers"
    return result


def _check_tts_engine() -> CheckResult:
    result = _check_python_module("edge_tts", "edge-tts (narration TTS)")
    result.category = "dependencies"
    if not result.passed:
        result.remedy = "pip install edge-tts"
    return result


def _check_otio() -> CheckResult:
    result = _check_python_module("opentimelineio", "OpenTimelineIO")
    result.category = "dependencies"
    if not result.passed:
        result.remedy = "pip install opentimelineio"
    return result


def _check_output_dir(output_dir: str) -> CheckResult:
    result = _check_writable_dir(output_dir, "Pipeline output directory")
    result.category = "filesystem"
    return result


def _check_disk() -> CheckResult:
    result = _check_disk_space("/tmp", 5.0, "Disk space for video output")
    result.category = "filesystem"
    return result


# ---------------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------------

def run_preflight(
    output_dir: str = "/tmp/documentary-pipeline",
    pipeline_id: str = "",
) -> PreflightReport:
    """Run all preflight checks and return a structured report.

    This is the single entry point. Call it before the pipeline starts.
    If report.passed is False, the pipeline must not start.
    """
    report = PreflightReport(pipeline_id=pipeline_id)
    report.started_at = time.time()

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
        status = check.status
        logger.info("preflight [%s] %s: %s", status, check.name, check.message)

    report.finished_at = time.time()

    # Always print the dashboard — this is what gets projected
    print(report.as_dashboard())

    # Also write JSON to the output directory for web consumers
    try:
        os.makedirs(output_dir, exist_ok=True)
        json_path = os.path.join(output_dir, "preflight.json")
        with open(json_path, "w") as f:
            f.write(report.as_json())
        logger.info("Preflight JSON written to %s", json_path)
    except Exception:
        pass

    if not report.passed:
        logger.error("Preflight FAILED — %d hard failure(s):", len(report.hard_failures))
        for f in report.hard_failures:
            logger.error("  %s: %s → %s", f.name, f.message, f.remedy)
    elif report.warnings:
        logger.warning("Preflight PASSED with %d warning(s)", len(report.warnings))
    else:
        logger.info("Preflight PASSED — all checks green")

    return report


class PreflightError(Exception):
    """Raised when preflight checks fail."""
    def __init__(self, report: PreflightReport):
        self.report = report
        failures = "\n".join(f"  {f.name}: {f.message} → {f.remedy}" for f in report.hard_failures)
        super().__init__(f"Preflight checks failed:\n{failures}")
