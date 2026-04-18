"""Narration reconciliation loop (ARCH-E2, issue #148).

For every narration block, compare the WhisperX-measured speech
duration against the scripted pacing declared by the scenario (i.e.
the voice-budget slice of ``scene.duration_sec``).  If the measurement
deviates from the scripted pacing beyond tolerance, the block is
scheduled for audio-ladder re-entry — this module signals the
escalation; the audio ladder (ARCH-D1) is the retry mechanism.

Crystallisation (ARCH-E1 draft → authoritative) must wait for:

1. This reconciliation pass to report every block within tolerance
   (``_narration_reconciliation_passed = True``), AND
2. The stylistic QA pass (ARCH-E3) to report every block within its
   invariants (``_stylistic_qa_passed = True``).

Spec references:
    - Issue #148 (ARCH-E2 narration reconciliation loop)
    - Parent issue #127 (ARCH-E Authoritative OTIO + reconciliation + QA)
    - Meta issue #122 (ARCH-2026 architecture conformance)
    - ``docs/ARCHITECTURE_DIAGRAMS.md`` diagram 2 (audio ladder, stylistic
      QA, crystallise)

ADK idioms (per meta #122):

- :func:`build_narration_reconciliation_agent` returns a
  ``google.adk.agents.Agent`` subclass instance with the measurement
  callables registered as plain ``tools=[...]``.
- Cross-stage state flows through the blackboard under
  :data:`NARRATION_RECONCILIATION_STATE_KEY` (full per-block report)
  and :data:`NARRATION_RECONCILIATION_PASSED_KEY` (boolean gate read
  by :func:`server.callbacks.otio_state.authoritative_transition_callback`).
- Stage-boundary enforcement runs as an ``after_agent_callback``
  (:func:`narration_reconciliation_after_agent_callback`) — same
  pattern as the Timeline Guardian and Stylistic QA callbacks.
- :data:`NARRATION_RECONCILIATION_OPERATION` is the recovery-ladder
  operation name; audio-stage callers pass it to
  :func:`server.recovery.escalate_pipeline_error`.
- Fail-loud: malformed blackboard state, unparseable JSON, or any
  FAIL verdict raises :class:`NarrationReconciliationFailure` — we
  never silently downgrade to a warning.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, MutableMapping, Optional, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tolerance constants
# ---------------------------------------------------------------------------

#: Relative-tolerance band on scripted pacing (±fraction of the scripted
#: duration). 15 % is wide enough to absorb natural TTS jitter but tight
#: enough to catch a block that ran 50 % short (the canonical failure
#: pattern when the scenario generated too little text for the slot).
DEFAULT_TOLERANCE_RATIO: float = 0.15

#: Minimum absolute tolerance (seconds). Very short blocks have tiny
#: absolute ratio bands (e.g. a 2 s block at 15 % = ±0.3 s) that are
#: below the measurement noise floor; we floor the tolerance to this
#: value so the reconciliation loop never flags a block that is closer
#: to target than the measurement jitter itself.
DEFAULT_ABS_TOLERANCE_SEC: float = 0.25


# ---------------------------------------------------------------------------
# Blackboard keys
# ---------------------------------------------------------------------------

#: Blackboard key under which the per-block reconciliation report is
#: persisted. Value is a JSON-encoded list of
#: :meth:`NarrationTimingResult.to_dict` outputs.
NARRATION_RECONCILIATION_STATE_KEY = "_narration_reconciliation_report"

#: Blackboard boolean gate (``True`` when every scheduled block passed
#: timing reconciliation, ``False`` otherwise). The authoritative OTIO
#: transition reads this to decide whether to crystallise.
NARRATION_RECONCILIATION_PASSED_KEY = "_narration_reconciliation_passed"

#: Recovery-ladder operation name. Callers of
#: :func:`server.recovery.escalate_pipeline_error` pass this so the
#: audio ladder's agent policy recognises a narration-reconciliation
#: violation and re-enters the ladder with the timing delta as the
#: failure signal.
NARRATION_RECONCILIATION_OPERATION = "audio_narration_reconciliation"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class NarrationTimingVerdict(str, Enum):
    """Verdict for a single narration block's timing measurement."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"  # e.g. no measurement available (upstream gap)


@dataclass(frozen=True)
class NarrationTimingResult:
    """Outcome of a single block's timing reconciliation.

    Attributes:
        block_id: Stable identifier (e.g. ``"scene_003_V1_RU"``).
        scene_num: Scene number the block belongs to.
        voice_role: Speaker role (e.g. ``"V1"``).
        language: Language code (``"ru"``, ``"en"``; empty for
            single-lang runs).
        scripted_sec: Scripted pacing from the scenario (seconds).
        measured_sec: WhisperX-measured actual duration (seconds).
        delta_sec: ``measured_sec - scripted_sec``.
        ratio: Fractional drift relative to scripted (``delta_sec /
            scripted_sec``); ``0.0`` when scripted is zero.
        tolerance_sec: Effective absolute tolerance band applied.
        verdict: PASS / FAIL / SKIP.
        message: Human-readable description.
    """

    block_id: str
    scene_num: int
    voice_role: str
    language: str
    scripted_sec: float
    measured_sec: float
    delta_sec: float
    ratio: float
    tolerance_sec: float
    verdict: NarrationTimingVerdict
    message: str = ""
    metadata: dict = field(default_factory=dict)

    def is_failure(self) -> bool:
        return self.verdict is NarrationTimingVerdict.FAIL

    def to_dict(self) -> dict:
        return {
            "block_id": self.block_id,
            "scene_num": self.scene_num,
            "voice_role": self.voice_role,
            "language": self.language,
            "scripted_sec": round(self.scripted_sec, 3),
            "measured_sec": round(self.measured_sec, 3),
            "delta_sec": round(self.delta_sec, 3),
            "ratio": round(self.ratio, 4),
            "tolerance_sec": round(self.tolerance_sec, 3),
            "verdict": self.verdict.value,
            "message": self.message,
            "metadata": dict(self.metadata),
        }


class NarrationReconciliationFailure(RuntimeError):
    """Raised when one or more narration blocks deviate beyond tolerance.

    ``failures`` carries the structured list of
    :class:`NarrationTimingResult` with ``FAIL`` verdicts; the audio
    ladder recovery agent reads
    :meth:`diagnostic_data` instead of parsing the string message.
    Retries are the mechanism — this exception is the ladder re-entry
    signal, not a terminal error.
    """

    def __init__(self, failures: Sequence[NarrationTimingResult]) -> None:
        self.failures: list[NarrationTimingResult] = list(failures)
        affected = sorted({f.block_id for f in self.failures})
        worst = max(
            (abs(f.delta_sec) for f in self.failures), default=0.0
        )
        super().__init__(
            f"narration reconciliation violation on {len(affected)} "
            f"block(s) (worst drift {worst:.2f}s): {affected}"
        )

    def diagnostic_data(self) -> dict:
        """Structured payload for ``escalate_pipeline_error``.

        The audio recovery agent reads ``timing_violations`` to decide
        which blocks need TTS re-synthesis and what the target drift
        direction is (short → lengthen scene budget, long → trim text).
        """
        return {
            "timing_violations": [f.to_dict() for f in self.failures],
            "affected_blocks": sorted({f.block_id for f in self.failures}),
            "operation": NARRATION_RECONCILIATION_OPERATION,
        }


# ---------------------------------------------------------------------------
# Blackboard parsing
# ---------------------------------------------------------------------------


def _parse_json_mapping(
    state: Mapping[str, Any], key: str, default: str = "{}",
) -> dict:
    """Extract a JSON-encoded dict from ``state[key]``. Fail-loud on drift."""
    raw = state.get(key, default)
    if isinstance(raw, Mapping):
        return dict(raw)
    if raw is None or raw == "":
        return {}
    if not isinstance(raw, str):
        raise TypeError(
            f"{key!r} must be a JSON string or mapping, got "
            f"{type(raw).__name__}"
        )
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{key!r} is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ValueError(
            f"{key!r} must decode to a dict, got {type(decoded).__name__}"
        )
    return decoded


def _parse_json_list(
    state: Mapping[str, Any], key: str, default: str = "[]",
) -> list:
    """Extract a JSON-encoded list from ``state[key]``. Fail-loud on drift."""
    raw = state.get(key, default)
    if isinstance(raw, list):
        return list(raw)
    if raw is None or raw == "":
        return []
    if not isinstance(raw, str):
        raise TypeError(
            f"{key!r} must be a JSON string or list, got "
            f"{type(raw).__name__}"
        )
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{key!r} is not valid JSON: {exc}") from exc
    if not isinstance(decoded, list):
        raise ValueError(
            f"{key!r} must decode to a list, got {type(decoded).__name__}"
        )
    return decoded


# ---------------------------------------------------------------------------
# Timing lookup helpers
# ---------------------------------------------------------------------------


def _budget_key_for_block(scene_num: int, voice_role: str) -> str:
    """Voice-budget key used by the audio callback: ``scene_NNN_VOICE``."""
    return f"scene_{scene_num:03d}_{voice_role}"


def _scripted_duration_for_block(
    voice_budgets: Mapping[str, float],
    scene_num: int,
    voice_role: str,
) -> Optional[float]:
    """Return the scripted pacing (seconds) for ``scene_num / voice_role``.

    ``None`` when the budget isn't in state (e.g. an alignment-only
    rehydration) — the caller treats this as SKIP, not FAIL.
    """
    key = _budget_key_for_block(scene_num, voice_role)
    if key not in voice_budgets:
        return None
    try:
        return float(voice_budgets[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"voice_budgets[{key!r}] is not a number: "
            f"{voice_budgets[key]!r}"
        ) from exc


def _measured_duration_for_block(
    alignment: Mapping[str, Any],
    block_id: str,
) -> Optional[float]:
    """Return the WhisperX-measured duration for ``block_id``.

    Looks for the alignment entry under ``block_id`` first; falls back
    to the trimmed ``block_id`` without a language suffix (so
    ``scene_001_V1_RU`` also matches ``scene_001_V1`` when the
    upstream alignment key drops the language tail).

    ``None`` when no alignment was recorded (upstream gap — the
    caller treats this as SKIP, not FAIL, because regenerating the
    block won't change an absent alignment).
    """
    data = alignment.get(block_id)
    if not isinstance(data, Mapping):
        # Strip trailing language suffix if present (e.g. "_RU", "_EN").
        parts = block_id.rsplit("_", 1)
        if len(parts) == 2 and parts[1].isalpha() and len(parts[1]) <= 4:
            data = alignment.get(parts[0])
    if not isinstance(data, Mapping):
        return None
    measured = data.get("total_duration")
    if measured is None:
        return None
    try:
        measured_f = float(measured)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"whisperx_alignment[{block_id!r}].total_duration is not a "
            f"number: {measured!r}"
        ) from exc
    return measured_f if measured_f > 0 else None


# ---------------------------------------------------------------------------
# Reconciliation kernel
# ---------------------------------------------------------------------------


def reconcile_block(
    *,
    block_id: str,
    scene_num: int,
    voice_role: str,
    language: str,
    scripted_sec: Optional[float],
    measured_sec: Optional[float],
    tolerance_ratio: float = DEFAULT_TOLERANCE_RATIO,
    abs_tolerance_sec: float = DEFAULT_ABS_TOLERANCE_SEC,
) -> NarrationTimingResult:
    """Compare one block's scripted vs measured duration.

    Returns a PASS / FAIL / SKIP result. A block is SKIPped when we
    lack either a scripted pacing or a measurement — that is not a
    silent failure: the caller's aggregate gate (see
    :func:`run_narration_reconciliation`) still requires an explicit
    pass on every block that has the data to be checked.

    The tolerance is the larger of ``tolerance_ratio * scripted_sec``
    and ``abs_tolerance_sec`` — ratio-based for long blocks,
    absolute-floor for short blocks.
    """
    if scripted_sec is None:
        return NarrationTimingResult(
            block_id=block_id,
            scene_num=scene_num,
            voice_role=voice_role,
            language=language,
            scripted_sec=0.0,
            measured_sec=measured_sec or 0.0,
            delta_sec=0.0,
            ratio=0.0,
            tolerance_sec=0.0,
            verdict=NarrationTimingVerdict.SKIP,
            message=(
                f"no scripted pacing available for {block_id} "
                f"(voice_budgets missing key)"
            ),
        )
    if measured_sec is None:
        return NarrationTimingResult(
            block_id=block_id,
            scene_num=scene_num,
            voice_role=voice_role,
            language=language,
            scripted_sec=scripted_sec,
            measured_sec=0.0,
            delta_sec=0.0,
            ratio=0.0,
            tolerance_sec=0.0,
            verdict=NarrationTimingVerdict.SKIP,
            message=(
                f"no WhisperX measurement available for {block_id} "
                f"(whisperx_alignment missing entry)"
            ),
        )

    delta = measured_sec - scripted_sec
    ratio = (delta / scripted_sec) if scripted_sec > 0 else 0.0
    tolerance = max(tolerance_ratio * scripted_sec, abs_tolerance_sec)
    if abs(delta) <= tolerance:
        verdict = NarrationTimingVerdict.PASS
        msg = (
            f"within tolerance: measured={measured_sec:.2f}s "
            f"scripted={scripted_sec:.2f}s "
            f"delta={delta:+.2f}s (|{ratio * 100:.1f}%|) "
            f"tolerance=±{tolerance:.2f}s"
        )
    else:
        verdict = NarrationTimingVerdict.FAIL
        direction = "over" if delta > 0 else "under"
        msg = (
            f"OUT OF TOLERANCE: measured={measured_sec:.2f}s "
            f"scripted={scripted_sec:.2f}s "
            f"delta={delta:+.2f}s ({ratio * 100:+.1f}%) — "
            f"block is {direction} scripted pacing by "
            f"{abs(delta):.2f}s (tolerance ±{tolerance:.2f}s)"
        )

    return NarrationTimingResult(
        block_id=block_id,
        scene_num=scene_num,
        voice_role=voice_role,
        language=language,
        scripted_sec=scripted_sec,
        measured_sec=measured_sec,
        delta_sec=delta,
        ratio=ratio,
        tolerance_sec=tolerance,
        verdict=verdict,
        message=msg,
    )


def collect_failures(
    results: Sequence[NarrationTimingResult],
) -> list[NarrationTimingResult]:
    return [r for r in results if r.is_failure()]


# ---------------------------------------------------------------------------
# Stage-boundary entry point
# ---------------------------------------------------------------------------


def _derive_blocks_from_state(
    state: Mapping[str, Any],
) -> list[dict]:
    """Derive per-block specs from ``_stylistic_qa_blocks`` in state.

    We reuse the QA block list that the audio callback already
    populates instead of re-deriving from the raw alignment dict —
    that keeps E2 and E3 operating on the same block universe.
    """
    raw = _parse_json_list(state, "_stylistic_qa_blocks")
    blocks: list[dict] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        if not entry.get("block_id"):
            continue
        blocks.append({
            "block_id": str(entry["block_id"]),
            "scene_num": int(entry.get("scene_num", 0) or 0),
            "voice_role": str(entry.get("voice_role", "")),
            "language": str(entry.get("language", "")),
        })
    return blocks


def _persist_report(
    state: MutableMapping[str, Any],
    results: Sequence[NarrationTimingResult],
) -> None:
    state[NARRATION_RECONCILIATION_STATE_KEY] = json.dumps(
        [r.to_dict() for r in results]
    )


def run_narration_reconciliation(
    state: MutableMapping[str, Any],
    *,
    blocks: Optional[Sequence[Mapping[str, Any]]] = None,
    tolerance_ratio: float = DEFAULT_TOLERANCE_RATIO,
    abs_tolerance_sec: float = DEFAULT_ABS_TOLERANCE_SEC,
    raise_on_failure: bool = True,
) -> list[NarrationTimingResult]:
    """Reconcile every narration block's timing against scripted pacing.

    Args:
        state: Pipeline blackboard. Read-only for measurements
            (``_voice_budgets`` and ``whisperx_alignment``); written
            to for the :data:`NARRATION_RECONCILIATION_STATE_KEY`
            report and the :data:`NARRATION_RECONCILIATION_PASSED_KEY`
            gate.
        blocks: Optional explicit block list (each entry must carry
            ``block_id``, ``scene_num``, ``voice_role``, and
            ``language``). When omitted, the blocks are derived from
            ``state["_stylistic_qa_blocks"]``.
        tolerance_ratio: Relative-tolerance band (fraction of
            scripted). See :data:`DEFAULT_TOLERANCE_RATIO`.
        abs_tolerance_sec: Absolute-tolerance floor (seconds). See
            :data:`DEFAULT_ABS_TOLERANCE_SEC`.
        raise_on_failure: When ``True`` (default), any FAIL verdict
            raises :class:`NarrationReconciliationFailure` after
            persisting the report. When ``False``, the caller gets
            the full result list and decides how to escalate.

    Returns:
        Full :class:`NarrationTimingResult` list, one per block,
        including SKIPped entries.

    Raises:
        NarrationReconciliationFailure: When ``raise_on_failure`` is
            True and at least one block's measured duration is out
            of tolerance.
        ValueError / TypeError: When the blackboard carries malformed
            JSON or a non-numeric budget / measurement. Fail-loud.
    """
    if blocks is None:
        block_specs = _derive_blocks_from_state(state)
    else:
        block_specs = [
            {
                "block_id": str(b["block_id"]),
                "scene_num": int(b.get("scene_num", 0) or 0),
                "voice_role": str(b.get("voice_role", "")),
                "language": str(b.get("language", "")),
            }
            for b in blocks
            if isinstance(b, Mapping) and b.get("block_id")
        ]

    if not block_specs:
        logger.warning(
            "narration_reconciliation: no blocks to reconcile "
            "(empty or missing _stylistic_qa_blocks) — marking pass"
        )
        state[NARRATION_RECONCILIATION_PASSED_KEY] = True
        _persist_report(state, [])
        return []

    voice_budgets = _parse_json_mapping(state, "_voice_budgets")
    alignment = _parse_json_mapping(state, "whisperx_alignment")

    # Dual-language budget handling. In ``dual_ru_en`` mode the
    # scenario produces BOTH RU and EN clips for every voice role,
    # but ``_voice_budgets`` is keyed only by ``scene_NNN_VOICE`` and
    # carries the RU timing. The existing gatekeeper at
    # ``deterministic_steps.py`` neutralises this by setting
    # ``budget = voice_budget if lang_code == 'ru' else 0`` before
    # enforcement — EN clips are intentionally unbudgeted. Mirror
    # that contract here: SKIP non-primary-language blocks rather
    # than reconciling them against the wrong (RU) budget.
    language_mode = str(state.get("language", "")).strip().lower()
    primary_language: Optional[str] = None
    if language_mode == "dual_ru_en":
        primary_language = "ru"

    results: list[NarrationTimingResult] = []
    for spec in block_specs:
        block_language = spec["language"].strip().lower()
        if (
            primary_language is not None
            and block_language
            and block_language != primary_language
        ):
            results.append(NarrationTimingResult(
                block_id=spec["block_id"],
                scene_num=spec["scene_num"],
                voice_role=spec["voice_role"],
                language=spec["language"],
                scripted_sec=0.0,
                measured_sec=0.0,
                delta_sec=0.0,
                ratio=0.0,
                tolerance_sec=0.0,
                verdict=NarrationTimingVerdict.SKIP,
                message=(
                    f"{spec['block_id']}: secondary-language block in "
                    f"{language_mode!r} mode (primary={primary_language!r}); "
                    f"voice_budgets carries only primary-language pacing, "
                    f"so secondary-language blocks are intentionally "
                    f"unbudgeted (mirrors gatekeeper at "
                    f"deterministic_steps.py)"
                ),
            ))
            continue
        scripted = _scripted_duration_for_block(
            voice_budgets,
            spec["scene_num"],
            spec["voice_role"],
        )
        measured = _measured_duration_for_block(alignment, spec["block_id"])
        results.append(reconcile_block(
            block_id=spec["block_id"],
            scene_num=spec["scene_num"],
            voice_role=spec["voice_role"],
            language=spec["language"],
            scripted_sec=scripted,
            measured_sec=measured,
            tolerance_ratio=tolerance_ratio,
            abs_tolerance_sec=abs_tolerance_sec,
        ))

    _persist_report(state, results)
    failures = collect_failures(results)
    if failures:
        state[NARRATION_RECONCILIATION_PASSED_KEY] = False
        logger.error(
            "narration_reconciliation: %d block(s) out of tolerance: %s",
            len(failures),
            [f.block_id for f in failures],
        )
        if raise_on_failure:
            raise NarrationReconciliationFailure(failures)
    else:
        state[NARRATION_RECONCILIATION_PASSED_KEY] = True
        logger.info(
            "narration_reconciliation: all %d block(s) within tolerance",
            len(results),
        )
    return results


# ---------------------------------------------------------------------------
# ADK after_agent_callback wiring
# ---------------------------------------------------------------------------


def narration_reconciliation_after_agent_callback(callback_context) -> None:
    """Stage-boundary ``after_agent_callback`` enforcing the E2 loop.

    Runs only during the ``audio`` phase; all other phases are a
    no-op (visual direction / production / assembly bind to an
    already-authoritative OTIO and must never retrigger the audio
    reconciliation loop).

    On FAIL verdict this raises :class:`NarrationReconciliationFailure`
    — the audio ladder's recovery policy converts the exception into a
    re-entry with the violation payload as the failure signal. The
    recovery ladder's permissive budgets (ARCH-D1, already on main)
    apply: retries are the mechanism, not a terminal failure mode.
    """
    state = getattr(callback_context, "state", None)
    if state is None:
        logger.debug(
            "narration_reconciliation: no state on callback_context; skipping"
        )
        return None

    phase = state.get("pipeline_phase", "")
    if phase and phase != "audio":
        logger.debug(
            "narration_reconciliation: skipping — phase=%r != 'audio'",
            phase,
        )
        return None

    run_narration_reconciliation(state, raise_on_failure=True)
    return None


# ---------------------------------------------------------------------------
# ADK Agent factory (LLM available for ad-hoc diagnosis; the boundary
# check itself is deterministic)
# ---------------------------------------------------------------------------


_AGENT_NAME = "narration_reconciliation_agent"
_AGENT_TOOLS = (
    reconcile_block,
    run_narration_reconciliation,
    collect_failures,
)
_AGENT_INSTRUCTION = (
    "You are the Narration Reconciliation Agent (ARCH-E2).\n"
    "Reconciliation is enforced deterministically by the "
    "after_agent_callback. On failure, produce a one-paragraph "
    "diagnostic for the audio recovery agent and the dashboard "
    "reviewer — do not attempt to mask or re-interpret a FAIL "
    "verdict, the callback is authoritative.\n"
)


def build_narration_reconciliation_agent():
    """Return an ADK ``Agent`` wrapping the E2 reconciliation loop.

    Same pattern as :func:`server.critique.stylistic_qa_agent.build_stylistic_qa_agent`:
    the measurement callables are registered as plain ``tools=[...]``
    so an LLM can invoke them for ad-hoc diagnosis, but the
    stage-boundary invariant is enforced deterministically by the
    ``after_agent_callback``. Cross-stage state (the report + the
    pass gate) flows through the blackboard via ``output_key`` so
    downstream callbacks like
    :func:`server.callbacks.otio_state.authoritative_transition_callback`
    can read it.

    Returns a lightweight stub Agent when ``google-adk`` is not
    importable, so unit tests can exercise this factory without the
    ADK dependency.
    """
    try:
        from google.adk.agents import Agent  # type: ignore
    except ImportError:
        logger.info(
            "google-adk not importable; returning lightweight stub Agent "
            "for unit-test environments."
        )

        class _StubAgent:
            name = _AGENT_NAME
            tools = list(_AGENT_TOOLS)
            after_agent_callback = staticmethod(
                narration_reconciliation_after_agent_callback
            )
            output_key = NARRATION_RECONCILIATION_STATE_KEY

        return _StubAgent()

    try:
        from agents.model_config import build_model  # type: ignore
        model = build_model()
    except ImportError:  # pragma: no cover — defensive for non-repo imports
        import os
        model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

    return Agent(
        name=_AGENT_NAME,
        model=model,
        instruction=_AGENT_INSTRUCTION,
        tools=list(_AGENT_TOOLS),
        after_agent_callback=narration_reconciliation_after_agent_callback,
        output_key=NARRATION_RECONCILIATION_STATE_KEY,
    )
