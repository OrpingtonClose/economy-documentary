"""Pre-flight constraint gate — INTENT-02 (#266) + INTENT-05 (#269).

The gate sits between :mod:`agents.scenario_director` and the downstream
narration / audio / video stages.  Its contract:

1. Read the R0 :class:`~agents.intent_extractor.BriefIntent` and the
   scenario director's ``state['scenes']`` draft.
2. Evaluate hard constraints (duration, required / forbidden topics,
   audience fit) fail-closed.
3. On failure, append a targeted critique directive into the
   blackboard (``state[GATE_CRITIQUE_KEY]``) so the outer wrapper can
   re-run the scenario director with the critique visible in its
   prompt context.  Up to :data:`MAX_GATE_ATTEMPTS` retries.
4. After the max-retries ceiling we halt the run and emit a plain-
   English ``halt_fired`` chat turn via the narrator.
5. Only after a **pass** is :data:`INTENT_GATE_PASSED` signalled.  The
   lazy GPU worker provisioner (INTENT-05) blocks on this event so a
   brief that never parses correctly costs zero GPU-seconds.

The module exposes pure functions (:func:`evaluate_gate`,
:func:`build_critique`) plus an ADK-shaped callback
(:func:`run_preflight_gate`).  The callback mutates session state and
returns an ADK ``Content`` when the gate halts the run.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, MutableMapping, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Blackboard key holding the most recent gate verdict (JSON).
GATE_VERDICT_KEY: str = "_intent_gate_verdict"

#: Blackboard key holding the targeted critique the scenario director
#: should address on the next attempt.  Absent when the gate passed.
GATE_CRITIQUE_KEY: str = "_intent_gate_critique"

#: Blackboard key storing the attempt counter across director reruns.
GATE_ATTEMPT_KEY: str = "_intent_gate_attempt"

#: Maximum director attempts before the gate halts the pipeline.  The
#: scenario director's own LoopAgent already retries internally; this
#: limit wraps those retries at the R0 gate boundary.
MAX_GATE_ATTEMPTS: int = 3


#: Process-wide event that flips once INTENT-02 has accepted a scenario
#: draft against R0.  :mod:`worker_provisioner` (via
#: :func:`wait_for_intent_gate`) blocks on this so GPU VMs only start
#: booting after the brief is known to be understood correctly.
INTENT_GATE_PASSED: threading.Event = threading.Event()


class IntentGateHalt(RuntimeError):
    """Raised internally when the gate has exhausted its retries."""


# ---------------------------------------------------------------------------
# Verdict data class
# ---------------------------------------------------------------------------


@dataclass
class GateVerdict:
    """Structured outcome of a single gate evaluation.

    ``passed`` is the fail-closed boolean.  ``failures`` is a list of
    short strings, each describing one violated constraint — the gate
    surfaces them verbatim into the critique so the director's next
    attempt knows exactly what to fix.

    ``total_scene_duration_sec`` is the raw narration-only sum (legacy
    metric kept for back-compat with external callers and tests).
    ``movie_duration_sec`` is the expected final-film runtime including
    the silence gaps the audio stage will insert — this is what the
    gate actually compares against the user's target, because that is
    the duration the user sees in the delivered mp4.
    """

    passed: bool
    total_scene_duration_sec: float
    target_duration_sec: float
    tolerance_sec: float
    missing_required_topics: list[str] = field(default_factory=list)
    present_forbidden_topics: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    attempt: int = 1
    movie_duration_sec: float = 0.0
    gap_overhead_sec: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Pure evaluation
# ---------------------------------------------------------------------------


def _extract_scenes(state: Mapping[str, Any]) -> list[dict]:
    raw = state.get("scenes")
    if raw is None:
        return []
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, Mapping)]
    try:
        data = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return [s for s in data if isinstance(s, Mapping)]
    return []


def _sum_scene_duration_sec(scenes: list[dict]) -> float:
    total = 0.0
    for scene in scenes:
        val = scene.get("duration_sec")
        try:
            total += float(val)
        except (TypeError, ValueError):
            continue
    return total


# Inter-voice / inter-scene silence gap constants.  These MUST match the
# values used by :mod:`callbacks.deterministic_steps` (which scales scene
# durations so ``scene_sum + gaps = movie_target``) and by
# :mod:`callbacks.intent_verifier`.  Hard-coded here to avoid a runtime
# import cycle with ``callbacks.deterministic_steps`` — if the constants
# ever move, this list moves with them.
_INTER_VOICE_PAUSE_SEC: float = 1.5
_INTER_SCENE_PAUSE_SEC: float = 2.5


def _compute_gap_overhead_sec(scenes: list[dict]) -> float:
    """Return the total silence gap runtime the audio stage will insert.

    Mirrors the calculation in :mod:`callbacks.deterministic_steps`
    so the gate's notion of "movie duration" matches the value the
    deterministic audio callback will actually produce.
    """
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


def _compute_movie_duration_sec(scenes: list[dict]) -> float:
    """Return the expected final-film runtime (narration + silence gaps)."""
    return _sum_scene_duration_sec(scenes) + _compute_gap_overhead_sec(scenes)


_TOPIC_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "or", "of", "the", "in", "on", "for", "to",
        "with", "by", "as", "at", "from", "about", "over", "under",
        "its", "their", "this", "that", "these", "those", "is", "are",
        "be", "do", "did", "does", "discuss", "cover", "include", "mention",
    }
)


def _topic_keywords(topic: str) -> list[str]:
    """Tokenise a required/forbidden topic into matchable keywords.

    Splits on any non-alphanumeric character (so hyphenated phrases
    like ``fight-flight-freeze circuitry`` become
    ``["fight", "flight", "freeze", "circuitry"]``), lower-cases each
    word and drops structural stopwords.  Single-letter tokens are
    kept (abbreviations like "PAG" are upper-case in the brief but
    reduced to "pag" here).
    """
    if not topic:
        return []
    # Split on non-alphanumeric chars.  Preserves meaningful tokens
    # even when the brief uses hyphens, slashes, or punctuation.
    raw = re.split(r"[^a-zA-Z0-9]+", topic.lower())
    return [w for w in raw if w and w not in _TOPIC_STOPWORDS]


def _topic_covered(topic: str, blob_lower: str) -> bool:
    """Return True if ``topic`` is adequately covered by ``blob_lower``.

    The earlier implementation required an exact case-insensitive
    substring match which was unrealistically strict -- an LLM writing
    scenes about ``endogenous opioid receptors and endorphin release``
    was rightly judged to cover "opioid chemistry" but the literal
    two-word substring never appeared, triggering a HALT after three
    retries.

    New rule: tokenise the required topic into significant keywords
    (see :func:`_topic_keywords`) and require at least
    ``ceil(N/2)`` of those keywords to appear anywhere in the blob
    (minimum one).  This mirrors how a human reader decides whether a
    script "covers" a topic: you don't need the exact phrase, you need
    the concept to clearly appear.
    """
    keywords = _topic_keywords(topic)
    if not keywords:
        # No significant keywords -- fall back to substring match so we
        # never silently accept an empty topic as "covered".
        return bool(topic and topic.lower() in blob_lower)
    required_hits = max(1, (len(keywords) + 1) // 2)
    hits = sum(1 for kw in keywords if kw in blob_lower)
    return hits >= required_hits


def _scenes_text_blob(scenes: list[dict]) -> str:
    """Concatenate every voice/narration string in the draft.

    Used for topic coverage checks.  Lowercased to keep substring
    matching case-insensitive and closed over well-known scene fields.
    """
    parts: list[str] = []
    for scene in scenes:
        for key in ("title", "narration", "visual_notes", "dopamine_hook"):
            val = scene.get(key)
            if isinstance(val, str):
                parts.append(val)
        voices = scene.get("voices")
        if isinstance(voices, list):
            for voice in voices:
                if isinstance(voice, Mapping):
                    text = voice.get("text") or voice.get("line") or ""
                    if isinstance(text, str):
                        parts.append(text)
    return "\n".join(parts).lower()


def evaluate_gate(
    intent: "BriefIntent",  # noqa: F821 - forward ref, imported in callers
    scenes: list[dict],
    *,
    attempt: int = 1,
) -> GateVerdict:
    """Evaluate R0 hard constraints against a scenario draft.

    Pure function — no state mutation, no I/O.  Returns a
    :class:`GateVerdict` describing every violation found so callers
    can compose a critique or halt message.
    """
    total = _sum_scene_duration_sec(scenes)
    gap_overhead = _compute_gap_overhead_sec(scenes)
    movie_duration = total + gap_overhead
    failures: list[str] = []

    # Compare against MOVIE duration (narration + silence gaps) because
    # that is the runtime of the delivered mp4 — and therefore the
    # duration the user stated in the brief.  The raw narration sum is
    # kept on the verdict for diagnostics.  See also the matching check
    # in :mod:`callbacks.intent_verifier` which must stay in lockstep.
    lower_target = intent.duration_sec - intent.tolerance_sec
    upper_target = intent.duration_sec + intent.tolerance_sec
    if not scenes:
        failures.append("scenario draft has zero scenes")
    elif movie_duration < lower_target or movie_duration > upper_target:
        failures.append(
            f"expected film runtime {movie_duration:.1f}s (narration "
            f"{total:.1f}s + silence gaps {gap_overhead:.1f}s) is "
            f"outside the {intent.duration_sec:.1f}s ± "
            f"{intent.tolerance_sec:.1f}s window (acceptable range: "
            f"{lower_target:.1f}s — {upper_target:.1f}s)"
        )

    blob = _scenes_text_blob(scenes)
    missing_required: list[str] = []
    for topic in intent.required_topics:
        if not _topic_covered(topic, blob):
            missing_required.append(topic)
    if missing_required:
        failures.append(
            "scenario draft never mentions required topic(s): "
            + ", ".join(repr(t) for t in missing_required)
        )

    # Forbidden-topic match must be stricter than required-topic match:
    # we only halt on an actual meaningful phrase appearing, not on
    # incidental single keywords.  Use the original substring semantics
    # so "discuss recreational drug use" doesn't trigger on any scene
    # that happens to mention the word "use".
    present_forbidden: list[str] = []
    for topic in intent.forbidden_topics:
        if topic and topic.lower() in blob:
            present_forbidden.append(topic)
    if present_forbidden:
        failures.append(
            "scenario draft mentions forbidden topic(s): "
            + ", ".join(repr(t) for t in present_forbidden)
        )

    return GateVerdict(
        passed=not failures,
        total_scene_duration_sec=total,
        target_duration_sec=intent.duration_sec,
        tolerance_sec=intent.tolerance_sec,
        missing_required_topics=missing_required,
        present_forbidden_topics=present_forbidden,
        failures=failures,
        attempt=attempt,
        movie_duration_sec=movie_duration,
        gap_overhead_sec=gap_overhead,
    )


# ---------------------------------------------------------------------------
# Critique + halt messages
# ---------------------------------------------------------------------------


def build_critique(verdict: GateVerdict) -> str:
    """Turn a failing :class:`GateVerdict` into a director-facing critique.

    The critique is plain prose the scenario director can consume on
    its next attempt (we inject it under ``state[GATE_CRITIQUE_KEY]``
    and the director reads it as template context).
    """
    lines = [
        "R0 constraint gate rejected the previous scenario draft.",
        f"Target film runtime: {verdict.target_duration_sec:.1f}s "
        f"± {verdict.tolerance_sec:.1f}s.",
        f"Measured film runtime (narration + silence gaps): "
        f"{verdict.movie_duration_sec:.1f}s "
        f"(narration {verdict.total_scene_duration_sec:.1f}s + "
        f"gaps {verdict.gap_overhead_sec:.1f}s).",
        "Fix every item below on your next attempt:",
    ]
    for failure in verdict.failures:
        lines.append(f"- {failure}")
    return "\n".join(lines)


def build_halt_message(verdict: GateVerdict, *, max_attempts: int) -> str:
    """Compose a plain-English chat turn shown when the gate halts."""
    target = verdict.target_duration_sec
    tolerance = verdict.tolerance_sec
    # Report MOVIE runtime (narration + silence gaps), not raw narration.
    # The gate compares movie_duration against the target, so the halt
    # summary must show the same number, otherwise users see a confusing
    # "target 420s, got 356s" even when drift was actually in movie space.
    measured = verdict.movie_duration_sec
    joined = "; ".join(verdict.failures) or "unknown drift"
    return (
        f"Halting: after {max_attempts} attempts the scenario draft "
        f"still misses your brief (target {target:.0f}s ± "
        f"{tolerance:.0f}s, got {measured:.0f}s film runtime). "
        f"Remaining issues: {joined}."
    )


# ---------------------------------------------------------------------------
# ADK-shaped callback
# ---------------------------------------------------------------------------


def _record_verdict(state: MutableMapping[str, Any], verdict: GateVerdict) -> None:
    state[GATE_VERDICT_KEY] = json.dumps(verdict.to_dict())


def _record_critique(state: MutableMapping[str, Any], critique: str) -> None:
    state[GATE_CRITIQUE_KEY] = critique


def _clear_critique(state: MutableMapping[str, Any]) -> None:
    # ADK's State object does not implement ``.pop`` / ``__delitem__``;
    # overwrite to ``None`` so downstream checks using ``state.get(KEY)``
    # still see a falsy value.  Tests that read the plain-dict fixture
    # via ``in state`` assert on ``state.get(KEY)`` instead.
    state[GATE_CRITIQUE_KEY] = None


def _emit_halt(verdict: GateVerdict, *, max_attempts: int) -> None:
    """Narrate the halt via chat_narrator.emit_narrator_event.

    Failures in the narrator never propagate — the halt itself is
    already load-bearing, and the narrator is best-effort UI sugar.
    """
    try:
        from agents.chat_narrator import emit_narrator_event

        emit_narrator_event(
            "halt_fired",
            fields={
                "stage": "scenario",
                "checkpoint": "intent_gate",
                "message": build_halt_message(verdict, max_attempts=max_attempts),
            },
        )
    except Exception as exc:  # pragma: no cover -- defensive
        logger.debug("intent_gate: narrator halt_fired emit failed: %s", exc)


def run_preflight_gate(
    state: MutableMapping[str, Any],
    *,
    max_attempts: int = MAX_GATE_ATTEMPTS,
) -> GateVerdict:
    """Evaluate the gate against ``state`` and update gate bookkeeping.

    Side effects:

    * Writes the verdict under :data:`GATE_VERDICT_KEY`.
    * On failure, writes the critique under :data:`GATE_CRITIQUE_KEY`
      and increments the attempt counter.
    * On pass, clears the critique and signals
      :data:`INTENT_GATE_PASSED` (INTENT-05).
    * On exhaustion (``attempt >= max_attempts`` and still failing),
      emits a ``halt_fired`` narrator event and raises
      :class:`IntentGateHalt`.
    """
    from agents.intent_extractor import get_brief_intent

    intent = get_brief_intent(state)
    if intent is None:
        raise IntentGateHalt(
            "intent_gate: no BriefIntent in session state — "
            "intent_extractor must run before the gate"
        )

    attempt = int(state.get(GATE_ATTEMPT_KEY) or 0) + 1
    state[GATE_ATTEMPT_KEY] = attempt

    scenes = _extract_scenes(state)
    verdict = evaluate_gate(intent, scenes, attempt=attempt)
    _record_verdict(state, verdict)

    if verdict.passed:
        _clear_critique(state)
        INTENT_GATE_PASSED.set()
        logger.info(
            "intent_gate: PASS on attempt %d (duration %.1fs, %d scenes)",
            attempt, verdict.total_scene_duration_sec, len(scenes),
        )
        return verdict

    logger.warning(
        "intent_gate: FAIL on attempt %d/%d — %s",
        attempt, max_attempts, "; ".join(verdict.failures),
    )
    _record_critique(state, build_critique(verdict))

    if attempt >= max_attempts:
        _emit_halt(verdict, max_attempts=max_attempts)
        raise IntentGateHalt(
            build_halt_message(verdict, max_attempts=max_attempts)
        )
    return verdict


# ---------------------------------------------------------------------------
# INTENT-05: lazy GPU gating helpers
# ---------------------------------------------------------------------------


def wait_for_intent_gate(timeout_sec: Optional[float] = None) -> bool:
    """Block the caller until :data:`INTENT_GATE_PASSED` is signalled.

    Returns ``True`` if the event fired, ``False`` on timeout.  The
    worker provisioner (INTENT-05) waits on this before any call to
    :func:`worker_provisioner.WorkerProvisioner.start_provisioning` so
    cancelling a run before the gate passes costs zero GPU-seconds.
    """
    start = time.time()
    fired = INTENT_GATE_PASSED.wait(timeout=timeout_sec)
    logger.info(
        "intent_gate: wait_for_intent_gate fired=%s after %.2fs",
        fired, time.time() - start,
    )
    return fired


def reset_intent_gate() -> None:
    """Reset the cross-run signal — used by pipeline init and tests."""
    INTENT_GATE_PASSED.clear()


__all__ = [
    "GATE_ATTEMPT_KEY",
    "GATE_CRITIQUE_KEY",
    "GATE_VERDICT_KEY",
    "GateVerdict",
    "INTENT_GATE_PASSED",
    "IntentGateHalt",
    "MAX_GATE_ATTEMPTS",
    "build_critique",
    "build_halt_message",
    "evaluate_gate",
    "reset_intent_gate",
    "run_preflight_gate",
    "wait_for_intent_gate",
]
