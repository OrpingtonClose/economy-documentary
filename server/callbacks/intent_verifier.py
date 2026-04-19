"""Per-stage R0 constraint re-verification — INTENT-04 (#268).

Every stage boundary re-checks the subset of R0 constraints it owns
against the artefact it just produced.  This catches drift introduced
downstream (timing-loop re-writes, visual director sub-plots, assembler
re-cuts) that would otherwise reach the final film despite passing the
pre-flight gate at the scenario boundary.

Stage → constraint ownership map (matches issue #268 verbatim):

* ``scenario``  — total scene duration, required_topics coverage,
                  forbidden_topics absence.
* ``audio``     — measured narration duration (TTS + WhisperX) vs the
                  scenario's declared target.  When WhisperX hasn't
                  run yet we fall back to the scene-declared sum.
* ``visual``    — aspect-ratio match + visual-topic coverage for
                  required_topics.
* ``production``— per-clip duration sane + aspect ratio.
* ``assembly``  — final-film duration within R0 tolerance.

The module exposes :func:`verify_stage_constraints` (pure) and
:func:`log_verification` (writes a structured record to both the
logger and, when available, the reasoning-digest sink).  Drift beyond
tolerance is fail-closed: the caller raises / emits a halt rather than
continuing.  The narrator is poked with a plain-English ``halt_fired``
event on drift so the user sees a human-readable reason.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, MutableMapping, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

STAGE_SCENARIO = "scenario"
STAGE_AUDIO = "audio"
STAGE_VISUAL = "visual"
STAGE_PRODUCTION = "production"
STAGE_ASSEMBLY = "assembly"

_KNOWN_STAGES: tuple[str, ...] = (
    STAGE_SCENARIO,
    STAGE_AUDIO,
    STAGE_VISUAL,
    STAGE_PRODUCTION,
    STAGE_ASSEMBLY,
)

#: Blackboard key where a rolling list of verification records lives.
#: Each entry is a dict produced by :meth:`VerificationRecord.to_dict`.
#: Kept on the blackboard so the ``/agui/restated_brief`` endpoint can
#: surface the full verification history alongside R0 itself.
VERIFICATION_LOG_KEY: str = "_intent_verification_log"


# ---------------------------------------------------------------------------
# Verification record
# ---------------------------------------------------------------------------


@dataclass
class VerificationRecord:
    """Structured outcome of one stage-boundary verification."""

    stage: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers shared across stages
# ---------------------------------------------------------------------------


def _load_scenes(state: Mapping[str, Any]) -> list[dict]:
    raw = state.get("scenes")
    if raw is None:
        return []
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, Mapping)]
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(parsed, list):
        return [s for s in parsed if isinstance(s, Mapping)]
    return []


def _sum_scene_duration_sec(scenes: list[dict]) -> float:
    total = 0.0
    for scene in scenes:
        try:
            total += float(scene.get("duration_sec") or 0.0)
        except (TypeError, ValueError):
            continue
    return total


# Silence-gap constants.  MUST stay in lockstep with the values in
# :mod:`callbacks.intent_gate` and :mod:`callbacks.deterministic_steps`
# so the pre-flight gate, the per-stage verifier, and the actual audio
# pipeline agree on what "movie duration" means.
_INTER_VOICE_PAUSE_SEC: float = 1.5
_INTER_SCENE_PAUSE_SEC: float = 2.5


def _compute_gap_overhead_sec(scenes: list[dict]) -> float:
    total_voice_gaps = 0.0
    for scene in scenes:
        voices = scene.get("voices") or []
        active = 0
        for voice in voices:
            if not isinstance(voice, Mapping):
                continue
            text = voice.get("text") or ""
            if isinstance(text, str) and text.strip():
                active += 1
        total_voice_gaps += max(0, active - 1) * _INTER_VOICE_PAUSE_SEC
    total_scene_gaps = max(0, len(scenes) - 1) * _INTER_SCENE_PAUSE_SEC
    return total_voice_gaps + total_scene_gaps


def _scene_text_blob(scenes: list[dict]) -> str:
    parts: list[str] = []
    for scene in scenes:
        for key in ("title", "narration", "visual_notes", "dopamine_hook"):
            v = scene.get(key)
            if isinstance(v, str):
                parts.append(v)
        voices = scene.get("voices")
        if isinstance(voices, list):
            for voice in voices:
                if isinstance(voice, Mapping):
                    text = voice.get("text") or voice.get("line") or ""
                    if isinstance(text, str):
                        parts.append(text)
    return "\n".join(parts).lower()


def _visual_text_blob(state: Mapping[str, Any]) -> str:
    parts: list[str] = []
    concepts = state.get("visual_concepts")
    if isinstance(concepts, str):
        parts.append(concepts)
    elif isinstance(concepts, Mapping):
        parts.append(json.dumps(dict(concepts)))
    elif isinstance(concepts, list):
        parts.append(json.dumps(concepts))
    analysis = state.get("content_analysis")
    if isinstance(analysis, str):
        parts.append(analysis)
    return "\n".join(parts).lower()


def _measured_narration_duration_sec(state: Mapping[str, Any]) -> Optional[float]:
    """Read WhisperX-aligned total narration duration, if present."""
    raw = state.get("whisperx_alignment")
    if not raw:
        return None
    try:
        data = json.loads(str(raw)) if not isinstance(raw, Mapping) else dict(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, Mapping):
        return None
    total = data.get("total_duration_sec") or data.get("duration_sec")
    if total is not None:
        try:
            return float(total)
        except (TypeError, ValueError):
            pass
    # Sum per-scene alignments if present.
    scenes = data.get("scenes")
    if isinstance(scenes, list):
        acc = 0.0
        any_hit = False
        for item in scenes:
            if not isinstance(item, Mapping):
                continue
            val = item.get("duration_sec") or item.get("measured_duration_sec")
            if val is None:
                continue
            try:
                acc += float(val)
                any_hit = True
            except (TypeError, ValueError):
                continue
        if any_hit:
            return acc
    return None


def _format_aspect_ratio(state: Mapping[str, Any]) -> Optional[str]:
    hints = state.get("format_hints")
    if isinstance(hints, Mapping):
        val = hints.get("aspect_ratio")
        if val:
            return str(val).strip()
    return None


# ---------------------------------------------------------------------------
# Per-stage verifiers
# ---------------------------------------------------------------------------


def _verify_scenario(intent, state: Mapping[str, Any]) -> VerificationRecord:
    scenes = _load_scenes(state)
    total = _sum_scene_duration_sec(scenes)
    gap_overhead = _compute_gap_overhead_sec(scenes)
    movie_duration = total + gap_overhead
    failures: list[str] = []

    # Compare MOVIE runtime (narration + silence gaps) against target —
    # same semantics as the pre-flight gate.  See #263 follow-up: the
    # verifier used to check the raw narration sum, which meant the
    # deterministic_steps gap-scaling pass silently shifted the metric
    # out of range (run #3: 386.5s narration vs. 420s target while
    # movie runtime was exactly 450s) and triggered false-negative
    # halts on reasonable scenarios.
    lower = intent.duration_sec - intent.tolerance_sec
    upper = intent.duration_sec + intent.tolerance_sec
    if not scenes:
        failures.append("scenario artefact has zero scenes")
    elif movie_duration < lower or movie_duration > upper:
        failures.append(
            f"scenario film runtime {movie_duration:.1f}s (narration "
            f"{total:.1f}s + gaps {gap_overhead:.1f}s) outside "
            f"{intent.duration_sec:.1f}s ± {intent.tolerance_sec:.1f}s"
        )

    blob = _scene_text_blob(scenes)
    missing = [t for t in intent.required_topics if t and t.lower() not in blob]
    if missing:
        failures.append(
            "scenario missing required_topic(s): "
            + ", ".join(repr(t) for t in missing)
        )
    present_forbidden = [
        t for t in intent.forbidden_topics if t and t.lower() in blob
    ]
    if present_forbidden:
        failures.append(
            "scenario contains forbidden_topic(s): "
            + ", ".join(repr(t) for t in present_forbidden)
        )

    return VerificationRecord(
        stage=STAGE_SCENARIO,
        passed=not failures,
        failures=failures,
        metrics={
            "total_scene_duration_sec": total,
            "movie_duration_sec": movie_duration,
            "gap_overhead_sec": gap_overhead,
            "target_duration_sec": intent.duration_sec,
            "tolerance_sec": intent.tolerance_sec,
            "scene_count": len(scenes),
            "missing_required_topics": missing,
            "present_forbidden_topics": present_forbidden,
        },
    )


def _verify_audio(intent, state: Mapping[str, Any]) -> VerificationRecord:
    scenes = _load_scenes(state)
    declared = _sum_scene_duration_sec(scenes)
    measured = _measured_narration_duration_sec(state)
    reference = measured if measured is not None else declared

    # After ``deterministic_steps`` scales scene durations so that
    # narration + silence gaps = user target, the narration-only
    # number is intentionally shorter than the target by the gap
    # overhead.  Compare MOVIE duration (narration + gaps) against
    # the user's stated target — that's what ffprobe sees on the
    # delivered mp4.  Without this, any documentary with non-trivial
    # gap overhead would HALT right after the timing loop passes.
    gap_overhead = _compute_gap_overhead_sec(scenes)
    movie_duration = reference + gap_overhead

    failures: list[str] = []
    lower = intent.duration_sec - intent.tolerance_sec
    upper = intent.duration_sec + intent.tolerance_sec
    if movie_duration < lower or movie_duration > upper:
        failures.append(
            f"movie runtime {movie_duration:.1f}s "
            f"(narration {reference:.1f}s + gaps {gap_overhead:.1f}s) "
            f"outside {intent.duration_sec:.1f}s ± {intent.tolerance_sec:.1f}s"
        )
    if measured is not None and declared:
        drift = abs(measured - declared)
        if drift > intent.tolerance_sec:
            failures.append(
                f"measured narration ({measured:.1f}s) drifts "
                f"{drift:.1f}s from declared scenario duration "
                f"({declared:.1f}s)"
            )
    return VerificationRecord(
        stage=STAGE_AUDIO,
        passed=not failures,
        failures=failures,
        metrics={
            "measured_duration_sec": measured,
            "declared_scene_duration_sec": declared,
            "gap_overhead_sec": gap_overhead,
            "movie_duration_sec": movie_duration,
            "target_duration_sec": intent.duration_sec,
            "tolerance_sec": intent.tolerance_sec,
        },
    )


def _verify_visual(intent, state: Mapping[str, Any]) -> VerificationRecord:
    failures: list[str] = []
    blob = _visual_text_blob(state)
    missing = [t for t in intent.required_topics if t and t.lower() not in blob]
    if missing:
        failures.append(
            "visual direction never covers required_topic(s): "
            + ", ".join(repr(t) for t in missing)
        )

    target_aspect = _format_aspect_ratio({"format_hints": intent.format_hints})
    produced_aspect: Optional[str] = None
    raw_concepts = state.get("visual_concepts")
    if isinstance(raw_concepts, Mapping):
        produced_aspect = (
            raw_concepts.get("aspect_ratio") or raw_concepts.get("aspect")
        )
    if target_aspect and produced_aspect and target_aspect != produced_aspect:
        failures.append(
            f"visual concepts aspect_ratio {produced_aspect!r} ≠ "
            f"requested {target_aspect!r}"
        )

    return VerificationRecord(
        stage=STAGE_VISUAL,
        passed=not failures,
        failures=failures,
        metrics={
            "missing_required_topics": missing,
            "target_aspect_ratio": target_aspect,
            "produced_aspect_ratio": produced_aspect,
        },
    )


def _verify_production(intent, state: Mapping[str, Any]) -> VerificationRecord:
    failures: list[str] = []

    target_aspect = _format_aspect_ratio({"format_hints": intent.format_hints})
    clips = state.get("clips") or state.get("produced_clips") or []
    bad_aspect: list[str] = []
    zero_length: list[str] = []
    if isinstance(clips, list):
        for clip in clips:
            if not isinstance(clip, Mapping):
                continue
            clip_id = str(clip.get("id") or clip.get("slot_id") or "?")
            if target_aspect:
                aspect = clip.get("aspect_ratio") or clip.get("aspect")
                if aspect and aspect != target_aspect:
                    bad_aspect.append(f"{clip_id}:{aspect}")
            duration = clip.get("duration_sec")
            try:
                if duration is not None and float(duration) <= 0:
                    zero_length.append(clip_id)
            except (TypeError, ValueError):
                zero_length.append(clip_id)
    if bad_aspect:
        failures.append(
            f"clips with wrong aspect_ratio: {', '.join(bad_aspect)} "
            f"(target {target_aspect!r})"
        )
    if zero_length:
        failures.append(
            f"clips with non-positive duration: {', '.join(zero_length)}"
        )

    return VerificationRecord(
        stage=STAGE_PRODUCTION,
        passed=not failures,
        failures=failures,
        metrics={
            "clip_count": len(clips) if isinstance(clips, list) else 0,
            "target_aspect_ratio": target_aspect,
        },
    )


def _verify_assembly(intent, state: Mapping[str, Any]) -> VerificationRecord:
    failures: list[str] = []
    final = state.get("final_duration_sec") or state.get("assembled_duration_sec")
    measured: Optional[float] = None
    if final is not None:
        try:
            measured = float(final)
        except (TypeError, ValueError):
            measured = None

    if measured is None:
        measured = _measured_narration_duration_sec(state)
    if measured is None:
        measured = _sum_scene_duration_sec(_load_scenes(state))

    lower = intent.duration_sec - intent.tolerance_sec
    upper = intent.duration_sec + intent.tolerance_sec
    if measured < lower or measured > upper:
        failures.append(
            f"final film duration {measured:.1f}s outside "
            f"{intent.duration_sec:.1f}s ± {intent.tolerance_sec:.1f}s"
        )

    return VerificationRecord(
        stage=STAGE_ASSEMBLY,
        passed=not failures,
        failures=failures,
        metrics={
            "final_duration_sec": measured,
            "target_duration_sec": intent.duration_sec,
            "tolerance_sec": intent.tolerance_sec,
        },
    )


_STAGE_DISPATCH = {
    STAGE_SCENARIO: _verify_scenario,
    STAGE_AUDIO: _verify_audio,
    STAGE_VISUAL: _verify_visual,
    STAGE_PRODUCTION: _verify_production,
    STAGE_ASSEMBLY: _verify_assembly,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def verify_stage_constraints(
    stage: str,
    state: Mapping[str, Any],
) -> VerificationRecord:
    """Run the stage-owned R0 re-verification and return the record.

    Pure function — never raises on R0 drift (callers inspect
    ``record.passed`` and decide whether to halt).  Raises
    :class:`ValueError` for unknown stages so mis-wired callers fail
    loudly rather than silently skipping the check.
    """
    if stage not in _STAGE_DISPATCH:
        raise ValueError(
            f"verify_stage_constraints: unknown stage {stage!r} "
            f"(known: {_KNOWN_STAGES})"
        )
    from agents.intent_extractor import get_brief_intent

    intent = get_brief_intent(state)
    if intent is None:
        # No R0 == no contract to verify.  Return a passed record with
        # a metric noting the absence so log consumers can distinguish
        # "no R0" from "R0 satisfied".
        return VerificationRecord(
            stage=stage,
            passed=True,
            failures=[],
            metrics={"no_brief_intent": True},
        )
    verifier = _STAGE_DISPATCH[stage]
    return verifier(intent, state)


def log_verification(
    record: VerificationRecord,
    state: Optional[MutableMapping[str, Any]] = None,
) -> None:
    """Append ``record`` to :data:`VERIFICATION_LOG_KEY` and log it.

    When ``state`` is ``None`` the record still goes to the standard
    logger; this is the pattern used by tests that want to invoke the
    verifier without a full blackboard.
    """
    level = logging.INFO if record.passed else logging.ERROR
    logger.log(
        level,
        "intent_verifier: stage=%s passed=%s failures=%s metrics=%s",
        record.stage, record.passed, record.failures, record.metrics,
    )
    if state is None:
        return
    existing = state.get(VERIFICATION_LOG_KEY)
    if isinstance(existing, str):
        try:
            entries = json.loads(existing)
        except json.JSONDecodeError:
            entries = []
    elif isinstance(existing, list):
        entries = list(existing)
    else:
        entries = []
    entries.append(record.to_dict())
    state[VERIFICATION_LOG_KEY] = json.dumps(entries)


def emit_drift_narration(record: VerificationRecord) -> None:
    """Push a plain-English ``halt_fired`` chat turn for a failed record.

    The narrator is best-effort UI sugar — any failure here is swallowed
    so the halt path itself remains the load-bearing signal.
    """
    if record.passed:
        return
    try:
        from agents.chat_narrator import emit_narrator_event

        emit_narrator_event(
            "halt_fired",
            fields={
                "stage": record.stage,
                "checkpoint": "intent_verifier",
                "message": "; ".join(record.failures) or "R0 drift",
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        logger.debug("intent_verifier: halt narration failed: %s", exc)


def verify_and_log(
    stage: str,
    state: MutableMapping[str, Any],
) -> VerificationRecord:
    """Convenience: verify + log + narrate on drift.

    Callers that want fail-closed behaviour check ``record.passed`` on
    the returned record and raise / short-circuit accordingly.
    """
    record = verify_stage_constraints(stage, state)
    log_verification(record, state)
    if not record.passed:
        emit_drift_narration(record)
    return record


__all__ = [
    "STAGE_ASSEMBLY",
    "STAGE_AUDIO",
    "STAGE_PRODUCTION",
    "STAGE_SCENARIO",
    "STAGE_VISUAL",
    "VERIFICATION_LOG_KEY",
    "VerificationRecord",
    "emit_drift_narration",
    "log_verification",
    "verify_and_log",
    "verify_stage_constraints",
]
