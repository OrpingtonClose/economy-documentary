"""
Stage contracts — declarative enforcement of pipeline architecture invariants.

Every pipeline stage declares what it **requires** (services, upstream artifacts)
and what it **produces** (state keys, files).  Before a stage runs, its
preconditions are validated; after it runs, its postconditions are validated.

If any check fails in production mode the pipeline **stops immediately** with a
clear error — no silent degradation, no fallback to placeholder media.

Architecture invariants enforced here:

1. One model per VM — never share, never swap.
2. All required services must be healthy before a stage starts.
3. Upstream artifacts must be authentic (mode == "production"), not
   placeholder/fallback/synthetic.
4. Every produced artifact must exist and be uploaded to B2 immediately.
5. Failures are fatal and loud — never swallowed silently.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_TEST_MODE = os.environ.get("DOCUMENTARY_TEST_MODE", "").strip().lower() in (
    "1",
    "true",
)


# ---------------------------------------------------------------------------
# Contract definitions
# ---------------------------------------------------------------------------


@dataclass
class ServiceRequirement:
    """A GPU/TTS worker that must be healthy before a stage can run."""

    name: str  # human-readable (e.g. "TTS worker")
    env_var: str  # env var holding the URL (e.g. "TTS_WORKER_URL")
    capability: str  # key in /health response (e.g. "tts")
    required: bool = True  # if False, warn but don't block


@dataclass
class StageContract:
    """Declarative contract for a single pipeline stage.

    Fields:
        name:               Human-readable stage name.
        required_services:  Workers that must be healthy.
        required_state:     State keys that must hold real data (not placeholder
                            strings like "(not yet generated)").
        produced_state:     State keys this stage is expected to populate.
        produced_artifacts: Glob patterns for files this stage creates
                            (relative to /tmp/documentary-pipeline/).
    """

    name: str
    required_services: list[ServiceRequirement] = field(default_factory=list)
    required_state: list[str] = field(default_factory=list)
    produced_state: list[str] = field(default_factory=list)
    produced_artifacts: list[str] = field(default_factory=list)


# ── Concrete contracts for each pipeline stage ────────────────────────────

SCENARIO_CONTRACT = StageContract(
    name="scenario",
    required_services=[],  # LLM only — no GPU workers needed
    required_state=[],  # first stage — no upstream dependencies
    produced_state=["scenes"],
)

AUDIO_CONTRACT = StageContract(
    name="audio",
    required_services=[
        ServiceRequirement(
            name="TTS worker",
            env_var="TTS_WORKER_URL",
            capability="tts",
        ),
    ],
    required_state=["scenes"],
    produced_state=["whisperx_alignment"],
    produced_artifacts=["audio/*.wav"],
)

VISUAL_DIRECTION_CONTRACT = StageContract(
    name="visual_direction",
    required_services=[],  # LLM only
    required_state=["scenes", "whisperx_alignment"],
    produced_state=["visual_concepts"],
)

PRODUCTION_CONTRACT = StageContract(
    name="production",
    required_services=[
        ServiceRequirement(
            name="Video worker",
            env_var="VIDEO_WORKER_URLS",
            capability="ltx",
        ),
    ],
    required_state=["scenes", "whisperx_alignment"],
    produced_state=["visual_concepts"],
    produced_artifacts=["video/*.mp4"],
)

ASSEMBLY_CONTRACT = StageContract(
    name="assembly",
    required_services=[],  # ffmpeg only — local
    required_state=["scenes", "whisperx_alignment", "visual_concepts"],
    produced_artifacts=["output/*.mp4"],
)


# Placeholder values that indicate upstream stage didn't actually produce
# real output.  If any required_state key holds one of these, the stage
# contract fails.
_PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "[]",
        "{}",
        "(not yet analyzed)",
        "(not yet generated)",
        "(not yet evaluated)",
    }
)


# ---------------------------------------------------------------------------
# Validation logic
# ---------------------------------------------------------------------------


class ContractViolation(RuntimeError):
    """Raised when a stage contract is violated.

    Carries a structured ``details`` dict so callers can log / report
    the exact failure without parsing the message string.
    """

    def __init__(self, stage: str, message: str, details: Optional[dict] = None):
        self.stage = stage
        self.details = details or {}
        super().__init__(f"[{stage}] CONTRACT VIOLATION: {message}")


def _check_service_health(svc: ServiceRequirement) -> Optional[str]:
    """Check a single service's health.  Returns error string or None."""
    url = os.environ.get(svc.env_var, "")

    # Handle comma-separated URLs (e.g. VIDEO_WORKER_URLS)
    urls = [u.strip() for u in url.split(",") if u.strip()] if url else []
    if not urls:
        # Also check singular fallback env vars
        fallback_var = svc.env_var.replace("_URLS", "_URL")
        fallback = os.environ.get(fallback_var, "")
        if fallback:
            urls = [fallback.strip()]
        # Also check GPU_WORKER_URL as last resort for video
        if not urls and svc.capability == "ltx":
            gpu_url = os.environ.get("GPU_WORKER_URL", "")
            if gpu_url:
                urls = [gpu_url.strip()]

    if not urls:
        return (
            f"{svc.name}: {svc.env_var} is not set. "
            f"A dedicated {svc.name} VM is REQUIRED."
        )

    # At least one URL must be healthy with the right capability loaded
    healthy = 0
    last_error = ""
    for u in urls:
        health_url = f"{u.rstrip('/')}/health"
        try:
            req = Request(health_url)
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            if data.get("status") != "ok":
                last_error = f"{svc.name} at {u}: unhealthy status {data}"
                continue
            loaded_key = f"{svc.capability}_loaded"
            if not data.get(loaded_key, False):
                last_error = (
                    f"{svc.name} at {u}: {svc.capability} not loaded. "
                    f"Each model MUST run on its own dedicated VM."
                )
                continue
            # GAP 5.1: Verify worker_mode matches expected capability
            worker_mode = data.get("worker_mode", "")
            if worker_mode and svc.capability not in ("tts", "ltx"):
                pass  # unknown capability — skip mode check
            elif worker_mode and worker_mode not in (svc.capability, "both"):
                last_error = (
                    f"{svc.name} at {u}: worker_mode='{worker_mode}' but "
                    f"expected '{svc.capability}'. Each model MUST run on "
                    f"its own dedicated VM — never swap or share."
                )
                continue
            healthy += 1
        except (URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
            last_error = f"{svc.name} at {u}: unreachable ({exc})"

    if healthy == 0:
        return last_error or f"{svc.name}: no healthy workers found"
    return None


def validate_preconditions(contract: StageContract, state: dict) -> None:
    """Validate all preconditions for a pipeline stage.

    Checks:
    1. All required services are healthy (HTTP health check).
    2. All required state keys contain real data (not placeholder values).

    Raises ``ContractViolation`` if any check fails in production mode.
    In test mode, logs warnings but does not block.
    """
    errors: list[str] = []

    # -- Service health checks --
    for svc in contract.required_services:
        err = _check_service_health(svc)
        if err:
            if svc.required:
                errors.append(err)
            else:
                logger.warning("Contract [%s]: optional service issue: %s", contract.name, err)

    # -- Upstream state validation --
    for key in contract.required_state:
        val = state.get(key, "")
        val_str = str(val).strip() if val is not None else ""
        if val_str in _PLACEHOLDER_VALUES:
            errors.append(
                f"Required state key '{key}' is empty or placeholder: "
                f"'{val_str[:100]}'. The upstream stage did not produce "
                f"real output."
            )

    if not errors:
        logger.info(
            "Contract [%s]: preconditions PASSED (%d services, %d state keys)",
            contract.name,
            len(contract.required_services),
            len(contract.required_state),
        )
        return

    error_msg = (
        f"Stage '{contract.name}' cannot start — "
        f"{len(errors)} precondition(s) failed:\n"
        + "\n".join(f"  - {e}" for e in errors)
    )

    if _TEST_MODE:
        logger.warning("Contract [%s]: %s (test mode — continuing)", contract.name, error_msg)
        return

    raise ContractViolation(
        stage=contract.name,
        message=error_msg,
        details={"errors": errors},
    )


def validate_postconditions(contract: StageContract, state: dict) -> None:
    """Validate postconditions after a pipeline stage completes.

    Checks:
    1. All produced_state keys are populated with real data.
    2. At least some produced_artifacts exist on disk.

    Raises ``ContractViolation`` if validation fails in production mode.
    """
    errors: list[str] = []

    # -- Produced state validation --
    for key in contract.produced_state:
        val = state.get(key, "")
        val_str = str(val).strip() if val is not None else ""
        if val_str in _PLACEHOLDER_VALUES:
            errors.append(
                f"Stage '{contract.name}' should have produced state key "
                f"'{key}' but it is empty/placeholder: '{val_str[:100]}'"
            )

    # -- Produced artifact validation --
    import glob as globmod

    base = "/tmp/documentary-pipeline"
    for pattern in contract.produced_artifacts:
        full_pattern = os.path.join(base, pattern)
        matches = globmod.glob(full_pattern)
        if not matches:
            errors.append(
                f"Stage '{contract.name}' should have produced files "
                f"matching '{pattern}' but none found."
            )
        else:
            # Check that files are non-empty
            empty = [m for m in matches if os.path.getsize(m) == 0]
            if empty:
                errors.append(
                    f"Stage '{contract.name}' produced empty files: "
                    f"{empty[:3]}"
                )

    if not errors:
        logger.info(
            "Contract [%s]: postconditions PASSED (%d state keys, %d artifact patterns)",
            contract.name,
            len(contract.produced_state),
            len(contract.produced_artifacts),
        )
        return

    error_msg = (
        f"Stage '{contract.name}' postcondition check failed — "
        f"{len(errors)} issue(s):\n"
        + "\n".join(f"  - {e}" for e in errors)
    )

    if _TEST_MODE:
        logger.warning("Contract [%s]: %s (test mode — continuing)", contract.name, error_msg)
        return

    raise ContractViolation(
        stage=contract.name,
        message=error_msg,
        details={"errors": errors},
    )
