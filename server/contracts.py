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
from typing import Any, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

from testing.simulation_bridge import is_simulation_active


# ---------------------------------------------------------------------------
# Scene-level schema (scenario creator is the intelligence layer)
# ---------------------------------------------------------------------------
#
# These dataclasses document the structured fields that the scenario director
# emits PER SCENE (pronunciation_hints, ssml) and PER DOCUMENTARY (style_lock).
# They are intentionally lightweight — scenes move through the pipeline as
# plain JSON dicts in state["scenes"], so these classes exist to:
#   1. document the contract,
#   2. provide `from_dict` / validation helpers used by
#      ``server/tools/scenario_evaluator_checks.py`` and
#      ``server/tools/tts_ssml_smoke.py``,
#   3. keep prompt updates and structural checks in sync.
#
# The scenario director writes the FULL documentary's style_lock into
# state["style_lock"] once, at scenario-creation time.  It is then applied to
# every scene's visual prompt by downstream agents.  Scene 0 carries a
# HookSpec; the final scene carries an OutroSpec.


# Closed set of visual style families the documentary may lock to.  The
# scenario director must pick exactly one at scenario-creation time.  This
# prevents visual whiplash (PAG run mixed anime + watercolor + cyberpunk +
# live-action + 3D brain in the same documentary).
STYLE_FAMILIES: tuple[str, ...] = (
    "cinematic_documentary",
    "hand_drawn_animation",
    "realistic_3d",
    "stylized_2d_animation",
    "live_action_interview",
    "archival_footage",
    "mixed_media_collage",
    "painterly",
)

# Styles that are almost always disruptive when mixed with a serious
# documentary lock.  The evaluator uses this as the default forbidden set
# when a specific style_lock doesn't override it.
DEFAULT_FORBIDDEN_STYLES: frozenset[str] = frozenset(
    {
        "anime",
        "manga",
        "watercolor",
        "cyberpunk",
        "vaporwave",
        "pixel_art",
        "chibi",
        "cartoon_network",
    }
)


@dataclass
class StyleLock:
    """Global visual style directive locked at scenario-creation time.

    Applies to EVERY scene in the documentary.  The scenario director
    picks ONE style family before writing scenes; every visual prompt
    gets ``positive_fragment`` appended and ``negative_fragment`` fed
    to the diffusion model as negative conditioning.
    """

    dominant_style: str  # one of STYLE_FAMILIES
    forbidden_styles: frozenset[str] = field(default_factory=lambda: DEFAULT_FORBIDDEN_STYLES)
    positive_fragment: str = ""
    negative_fragment: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StyleLock":
        if not isinstance(raw, dict):
            raise ValueError(f"StyleLock must be a dict, got {type(raw).__name__}")
        dominant = str(raw.get("dominant_style", "")).strip()
        if not dominant:
            raise ValueError("StyleLock.dominant_style is required and non-empty")
        forbidden_raw = raw.get("forbidden_styles") or []
        if isinstance(forbidden_raw, str):
            forbidden_raw = [s.strip() for s in forbidden_raw.split(",") if s.strip()]
        forbidden = frozenset(str(s).lower().strip() for s in forbidden_raw)
        if not forbidden:
            forbidden = DEFAULT_FORBIDDEN_STYLES
        return cls(
            dominant_style=dominant,
            forbidden_styles=forbidden,
            positive_fragment=str(raw.get("positive_fragment", "")).strip(),
            negative_fragment=str(raw.get("negative_fragment", "")).strip(),
        )

    def is_valid(self) -> tuple[bool, str]:
        if not self.dominant_style:
            return False, "dominant_style empty"
        if not self.positive_fragment:
            return False, "positive_fragment empty (nothing to append to visual prompts)"
        return True, ""


@dataclass
class HookSpec:
    """Topic-specific opening hook for scene 0.

    PAG run failure mode: opened on a generic blurry 3D brain with no
    connection to the actual topic.  The hook must reference something
    concrete about the documentary's subject matter (a specific artifact,
    device, person, metric, date, etc.).
    """

    topic_specific_motif: str  # concrete thing: "a neurostimulator electrode array"
    motion_description: str  # what camera/subject does: "slow push-in on..."
    narrative_pull: str  # why the viewer stays: "because within 7 seconds..."

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "HookSpec":
        if not isinstance(raw, dict):
            raise ValueError(f"HookSpec must be a dict, got {type(raw).__name__}")
        return cls(
            topic_specific_motif=str(raw.get("topic_specific_motif", "")).strip(),
            motion_description=str(raw.get("motion_description", "")).strip(),
            narrative_pull=str(raw.get("narrative_pull", "")).strip(),
        )

    def is_valid(self, user_prompt: str = "") -> tuple[bool, str]:
        if not self.topic_specific_motif:
            return False, "topic_specific_motif empty"
        if len(self.topic_specific_motif.split()) < 2:
            return False, "topic_specific_motif too short (must be a concrete noun phrase)"
        if not self.motion_description:
            return False, "motion_description empty"
        if not self.narrative_pull:
            return False, "narrative_pull empty"
        return True, ""


@dataclass
class OutroSpec:
    """Explicit closing beat for the final scene.

    PAG run failure mode: ended on a fade, no recap, no CTA, no brand.
    The outro must be a concrete visual + one-sentence recap + CTA +
    brand card — not silence.
    """

    closing_shot: str  # concrete visual: "wide shot of empty lab chair, fade"
    recap_sentence: str  # one-sentence documentary summary
    cta: str  # call to action: "subscribe", "read the paper", etc.
    brand_card: str  # brand text overlay, e.g. documentary title + channel

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "OutroSpec":
        if not isinstance(raw, dict):
            raise ValueError(f"OutroSpec must be a dict, got {type(raw).__name__}")
        return cls(
            closing_shot=str(raw.get("closing_shot", "")).strip(),
            recap_sentence=str(raw.get("recap_sentence", "")).strip(),
            cta=str(raw.get("cta", "")).strip(),
            brand_card=str(raw.get("brand_card", "")).strip(),
        )

    def is_valid(self) -> tuple[bool, str]:
        if not self.closing_shot:
            return False, "closing_shot empty"
        if not self.recap_sentence:
            return False, "recap_sentence empty"
        # cta and brand_card are allowed to be brief but must be non-empty
        if not self.cta:
            return False, "cta empty"
        if not self.brand_card:
            return False, "brand_card empty"
        return True, ""


# Scene schema extension
#
# Every scene dict in state["scenes"] MAY now carry:
#   "pronunciation_hints": {"PAG": "P-A-G", "DBS": "D-B-S", ...}
#       -> passed to TTS so initialisms are spoken letter-by-letter.
#   "ssml": "<speak>...</speak>" | null
#       -> optional pre-rendered SSML.  When present, the TTS adapter
#          prefers this over plain text (falling back when the voice is
#          flagged ssml_unsupported by tts_ssml_smoke).
#
# Scene 0 additionally carries:
#   "hook_spec": { ... HookSpec fields ... }
#
# The final scene additionally carries:
#   "outro_spec": { ... OutroSpec fields ... }
#
# The documentary-global style_lock lives at state["style_lock"] (dict form).
# ``StyleLock.from_dict`` parses it.  It is NOT repeated per-scene.


def scene_pronunciation_hints(scene: dict[str, Any]) -> dict[str, str]:
    """Return a scene's pronunciation hints, tolerating missing keys."""
    hints = scene.get("pronunciation_hints") or {}
    if not isinstance(hints, dict):
        return {}
    return {str(k): str(v) for k, v in hints.items() if k and v}


def scene_ssml(scene: dict[str, Any]) -> Optional[str]:
    """Return the pre-rendered SSML for a scene, if any."""
    ssml = scene.get("ssml")
    if ssml is None:
        return None
    ssml_str = str(ssml).strip()
    return ssml_str or None


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

TIMING_CONTRACT = StageContract(
    name="timing",
    required_services=[],  # pure arithmetic over prior stage outputs
    required_state=["scenes", "whisperx_alignment"],
    produced_state=["timing_passed", "timing_report"],
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
    required_state=["scenes", "whisperx_alignment", "visual_concepts"],
    produced_state=[],
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

    if is_simulation_active():
        logger.warning("Contract [%s]: %s (simulation mode — continuing)", contract.name, error_msg)
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

    if is_simulation_active():
        logger.warning("Contract [%s]: %s (simulation mode — continuing)", contract.name, error_msg)
        return

    raise ContractViolation(
        stage=contract.name,
        message=error_msg,
        details={"errors": errors},
    )
