"""Validation tools -- per-node output verification and OTIO compliance.

Every pipeline agent calls these tools to verify its own deliverables
before reporting completion. Failures are returned as structured JSON
so the agent can reason about what went wrong and attempt self-healing.

This is the innermost layer of the escalation system — the agent itself
is responsible for fixing its output before the graph-level recovery
ladder kicks in.
"""

from __future__ import annotations

import glob as globmod
import json
import logging
import os
from typing import Any

from strands import tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Node-to-contract mapping
# ---------------------------------------------------------------------------

_NODE_CONTRACT_MAP: dict[str, str] = {
    "scenario": "SCENARIO_CONTRACT",
    "audio": "AUDIO_CONTRACT",
    "visual_direction": "VISUAL_DIRECTION_CONTRACT",
    "production": "PRODUCTION_CONTRACT",
    "assembly": "ASSEMBLY_CONTRACT",
    # Graph node aliases
    "video": "PRODUCTION_CONTRACT",
    "refine": "SCENARIO_CONTRACT",
}

_PIPELINE_BASE = "/tmp/documentary-pipeline"

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


def _get_contract(stage_name: str) -> Any:
    """Look up the StageContract for a given stage name."""
    from contracts import (
        ASSEMBLY_CONTRACT,
        AUDIO_CONTRACT,
        PRODUCTION_CONTRACT,
        SCENARIO_CONTRACT,
        VISUAL_DIRECTION_CONTRACT,
    )

    contracts = {
        "SCENARIO_CONTRACT": SCENARIO_CONTRACT,
        "AUDIO_CONTRACT": AUDIO_CONTRACT,
        "VISUAL_DIRECTION_CONTRACT": VISUAL_DIRECTION_CONTRACT,
        "PRODUCTION_CONTRACT": PRODUCTION_CONTRACT,
        "ASSEMBLY_CONTRACT": ASSEMBLY_CONTRACT,
    }
    contract_name = _NODE_CONTRACT_MAP.get(stage_name.lower(), "")
    return contracts.get(contract_name)


def _check_state_keys(keys: list[str], state: dict) -> list[dict[str, str]]:
    """Check that state keys contain real data (not placeholder values)."""
    failures = []
    for key in keys:
        val = state.get(key, "")
        val_str = str(val).strip() if val is not None else ""
        if val_str in _PLACEHOLDER_VALUES:
            failures.append({
                "key": key,
                "issue": "empty or placeholder",
                "current_value": val_str[:100] if val_str else "(empty)",
            })
    return failures


def _check_artifacts(patterns: list[str]) -> list[dict[str, str]]:
    """Check that expected file artifacts exist and are non-empty."""
    failures = []
    for pattern in patterns:
        full_pattern = os.path.join(_PIPELINE_BASE, pattern)
        matches = globmod.glob(full_pattern)
        if not matches:
            failures.append({
                "pattern": pattern,
                "issue": "no files found",
                "expected_path": full_pattern,
            })
        else:
            empty = [m for m in matches if os.path.getsize(m) == 0]
            if empty:
                failures.append({
                    "pattern": pattern,
                    "issue": f"{len(empty)} empty file(s)",
                    "empty_files": [os.path.basename(f) for f in empty[:5]],
                })
    return failures


@tool
def validate_deliverables(stage_name: str, tool_context: Any = None) -> str:
    """Validate that this agent produced all required outputs for the given stage.

    Checks StageContract postconditions (produced_state keys must contain real
    data, produced_artifacts must exist on disk and be non-empty) and OTIO
    compliance (timeline structure) for the current stage.

    The agent MUST call this as its final action. If failures are found,
    the agent should analyze the failure details, fix the issues, and
    re-validate. Up to 3 self-healing attempts are expected before giving up.

    Args:
        stage_name: Pipeline stage name (scenario, audio, production, assembly).

    Returns:
        JSON string: {passed: bool, failures: [...], otio_errors: [...]}
    """
    state = tool_context.invocation_state if tool_context else {}
    result: dict[str, Any] = {"passed": True, "failures": [], "otio_errors": []}

    # Look up contract
    contract = _get_contract(stage_name)
    if contract is None:
        result["failures"].append({
            "issue": f"unknown stage '{stage_name}'",
            "hint": "valid stages: scenario, audio, visual_direction, production, assembly, video, refine",
        })
        result["passed"] = False
        return json.dumps(result, indent=2)

    # Check produced_state
    state_failures = _check_state_keys(contract.produced_state, state)
    if state_failures:
        result["failures"].extend(state_failures)
        result["passed"] = False

    # Check produced_artifacts
    artifact_failures = _check_artifacts(contract.produced_artifacts)
    if artifact_failures:
        result["failures"].extend(artifact_failures)
        result["passed"] = False

    # Check OTIO compliance if timeline exists
    timeline_path = state.get("_timeline_path", "")
    if timeline_path:
        otio_errors = _run_otio_validation(state)
        if otio_errors:
            result["otio_errors"] = otio_errors
            result["passed"] = False

    status = "PASSED" if result["passed"] else "FAILED"
    logger.info(
        "stage=<%s>, status=<%s>, failures=<%d>, otio_errors=<%d> | deliverable validation",
        stage_name,
        status,
        len(result["failures"]),
        len(result["otio_errors"]),
    )

    return json.dumps(result, indent=2)


@tool
def validate_preconditions_tool(stage_name: str, tool_context: Any = None) -> str:
    """Check that all upstream deliverables exist before starting work.

    Checks StageContract preconditions: required services must be healthy
    and required state keys must contain real data (not placeholder values).

    The agent should call this first to verify its inputs are ready.
    If preconditions fail, the agent should report the specific missing
    inputs — do NOT proceed with work on missing data.

    Args:
        stage_name: Pipeline stage name (scenario, audio, production, assembly).

    Returns:
        JSON string: {passed: bool, failures: [...], services: [...]}
    """
    state = tool_context.invocation_state if tool_context else {}
    result: dict[str, Any] = {"passed": True, "failures": [], "services": []}

    contract = _get_contract(stage_name)
    if contract is None:
        result["failures"].append({
            "issue": f"unknown stage '{stage_name}'",
        })
        result["passed"] = False
        return json.dumps(result, indent=2)

    # Check required state
    state_failures = _check_state_keys(contract.required_state, state)
    if state_failures:
        for f in state_failures:
            f["hint"] = (
                f"The upstream stage did not produce '{f['key']}'. "
                "This is a critical precondition failure — the pipeline "
                "cannot continue without this data."
            )
        result["failures"].extend(state_failures)
        result["passed"] = False

    # Check required services
    try:
        from contracts import _check_service_health

        for svc in contract.required_services:
            err = _check_service_health(svc)
            if err:
                result["services"].append({
                    "service": svc.name,
                    "issue": err,
                    "required": svc.required,
                })
                if svc.required:
                    result["passed"] = False
    except ImportError:
        logger.debug("contracts module not available for service health checks")

    status = "PASSED" if result["passed"] else "FAILED"
    logger.info(
        "stage=<%s>, status=<%s>, failures=<%d> | precondition validation",
        stage_name,
        status,
        len(result["failures"]) + len([s for s in result["services"] if s.get("required")]),
    )

    return json.dumps(result, indent=2)


def _run_otio_validation(state: dict) -> list[dict[str, str]]:
    """Run OTIO timeline validators and return structured errors."""
    errors: list[dict[str, str]] = []
    try:
        from callbacks.timeline_guardian import _VALIDATORS, _load_timeline

        pipeline_phase = state.get("pipeline_phase", "")
        timeline = _load_timeline(state)
        if not timeline:
            errors.append({
                "issue": "timeline not found",
                "path": state.get("_timeline_path", ""),
                "hint": "The OTIO timeline file does not exist or is empty",
            })
            return errors

        if pipeline_phase and pipeline_phase in _VALIDATORS:
            validator = _VALIDATORS[pipeline_phase]
            validation_errors = validator(timeline, state)
            if validation_errors:
                if isinstance(validation_errors, list):
                    for err in validation_errors:
                        errors.append({
                            "phase": pipeline_phase,
                            "issue": str(err),
                            "hint": _otio_remediation_hint(str(err)),
                        })
                else:
                    errors.append({
                        "phase": pipeline_phase,
                        "issue": str(validation_errors),
                        "hint": _otio_remediation_hint(str(validation_errors)),
                    })
        else:
            # Run all validators if no specific phase
            for phase_name, validator in _VALIDATORS.items():
                try:
                    validation_errors = validator(timeline, state)
                    if validation_errors:
                        err_list = validation_errors if isinstance(validation_errors, list) else [validation_errors]
                        for err in err_list:
                            errors.append({
                                "phase": phase_name,
                                "issue": str(err),
                                "hint": _otio_remediation_hint(str(err)),
                            })
                except Exception as exc:
                    errors.append({
                        "phase": phase_name,
                        "issue": f"validator error: {exc}",
                        "hint": "The validator itself crashed — this may indicate corrupted timeline data",
                    })

    except ImportError:
        logger.debug("timeline guardian not available for OTIO validation")
    except Exception as exc:
        errors.append({
            "issue": f"OTIO validation failed: {exc}",
            "hint": "Unexpected error during timeline validation",
        })

    return errors


def _otio_remediation_hint(error_str: str) -> str:
    """Generate actionable remediation hints for OTIO violations."""
    error_lower = error_str.lower()

    if "gap" in error_lower:
        return (
            "Timeline has a gap between clips. This usually means a video clip "
            "is shorter than its audio duration. Re-generate the video clip with "
            "a longer duration, or adjust the audio timing."
        )
    if "overlap" in error_lower:
        return (
            "Timeline has overlapping clips. This usually means a video clip "
            "extends beyond its allocated time slot. Trim the clip or adjust "
            "the timeline layout."
        )
    if "drift" in error_lower:
        return (
            "Audio/video sync drift detected. The cumulative timing error has "
            "exceeded the tolerance. Re-align the affected clips by regenerating "
            "with corrected durations from WhisperX alignment data."
        )
    if "missing" in error_lower or "empty" in error_lower:
        return (
            "Expected timeline track or clip is missing. Verify that the "
            "upstream generation step completed successfully and wrote its "
            "output to the correct timeline track."
        )
    if "duration" in error_lower:
        return (
            "A clip duration is outside the acceptable range. Check that the "
            "video generation parameters match the narration timing from "
            "WhisperX alignment."
        )
    return (
        "OTIO structural violation detected. Review the specific error and "
        "check the timeline file for structural integrity. May need to "
        "regenerate affected clips."
    )


@tool
def validate_otio_compliance(tool_context: Any = None) -> str:
    """Run OTIO timeline validation for the current pipeline phase.

    Checks for gaps, overlaps, drift, and structural violations in the
    OTIO timeline. Returns structured JSON with specific violations and
    actionable remediation hints.

    Critical for media generation nodes (audio, video, assembly).
    Should be called after any operation that modifies the OTIO timeline.

    Returns:
        JSON string: {passed: bool, errors: [...], timeline_path: str}
    """
    state = tool_context.invocation_state if tool_context else {}
    timeline_path = state.get("_timeline_path", "")

    result: dict[str, Any] = {
        "passed": True,
        "errors": [],
        "timeline_path": timeline_path,
        "pipeline_phase": state.get("pipeline_phase", ""),
    }

    if not timeline_path:
        result["errors"].append({
            "issue": "no timeline path in pipeline state",
            "hint": "The OTIO timeline has not been created yet. Call create_timeline first.",
        })
        result["passed"] = False
        return json.dumps(result, indent=2)

    otio_errors = _run_otio_validation(state)
    if otio_errors:
        result["errors"] = otio_errors
        result["passed"] = False

    status = "PASSED" if result["passed"] else "FAILED"
    logger.info(
        "phase=<%s>, status=<%s>, errors=<%d> | OTIO compliance check",
        result["pipeline_phase"],
        status,
        len(result["errors"]),
    )

    return json.dumps(result, indent=2)
