"""
Structural evaluator checks for the scenario director output.

These checks run BEFORE the LLM evaluator and are **deterministic** — they
don't require an LLM round-trip for anything that can be answered with a
regex, a sum, or a set lookup.  They exist because the PAG production run
exposed a pattern where the LLM evaluator cheerfully rated output GOOD
despite obvious structural failures:

  * User said "7-minute documentary"; pipeline produced 3:50.
    Evaluator rated it GOOD despite a 36% duration shortfall.
  * TTS pronounced "PAG" as a word (bag-rhyme) rather than P-A-G.
  * Visual whiplash: anime, watercolor, cyberpunk, live-action, 3D brain
    all in the same 3-minute documentary.
  * Rhetorical questions "What happens when...?" and "Can we harness...?"
    slipped past the evaluator.
  * Documentary ended on a fade with no outro spec.
  * Opening hook was a generic blurry 3D brain with no tie to the topic.
  * ~40% of scenes were off-topic (EU parliament, cyberpunk cityscape,
    transhumanist cyborg, group prayer).

Each check returns a ``CheckResult`` with a verdict cap.  The caller (the
scenario evaluator agent, or a test) aggregates verdicts and caps the
overall rating at the strictest cap — so any single hard failure pins the
verdict at POOR regardless of how the LLM feels.

Where a check needs semantic judgment (topic fidelity, rhetorical-vs-direct
question classification), it calls out to a lightweight LLM via a callable
injected by the caller.  This keeps the module importable and unit-testable
without any LLM configuration.

Scope: this module is called from the evaluator flow inside
``server/agents/scenario_director.py``.  It does NOT mutate scenes — only
reports.  Refinement (editing scenes) stays the scenario refiner's job.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from contracts import (
    DEFAULT_FORBIDDEN_STYLES,
    HookSpec,
    OutroSpec,
    StyleLock,
    scene_pronunciation_hints,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verdict types
# ---------------------------------------------------------------------------


# Rating ladder: POOR < FAIR < GOOD < EXCELLENT.  A check "caps" the overall
# rating — e.g. a failed duration check caps at POOR even if the LLM wants
# to give EXCELLENT.
VERDICTS = ("POOR", "FAIR", "GOOD", "EXCELLENT")
_VERDICT_RANK: dict[str, int] = {v: i for i, v in enumerate(VERDICTS)}


def cap_verdict(current: str, cap: str) -> str:
    """Return the stricter of ``current`` and ``cap`` (lower rank wins)."""
    c = _VERDICT_RANK.get(current.upper(), _VERDICT_RANK["EXCELLENT"])
    k = _VERDICT_RANK.get(cap.upper(), _VERDICT_RANK["EXCELLENT"])
    return VERDICTS[min(c, k)]


@dataclass
class CheckResult:
    """Result of a single structural check."""

    name: str
    passed: bool
    verdict_cap: str  # cap applied when passed=False; ignored when passed=True
    details: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "verdict_cap": self.verdict_cap if not self.passed else "",
            "details": self.details,
            "data": self.data,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_WORDS_PER_MINUTE_DEFAULT = 150


def _extract_narration_from_voices(voices: list[dict[str, Any]]) -> str:
    """Concatenate voice .text fields, stripping language tags like [RU]/[EN]."""
    pieces: list[str] = []
    for v in voices or []:
        if not isinstance(v, dict):
            continue
        text = str(v.get("text", "") or "").strip()
        # Strip inline language tags that appear in dual-language mode.
        text = re.sub(r"\[(?:RU|EN)\]\s*", " ", text)
        if text:
            pieces.append(text)
    return " ".join(pieces).strip()


def collect_narration(scenes: list[dict[str, Any]]) -> str:
    """Return all narration text concatenated across every scene's voices."""
    chunks: list[str] = []
    for s in scenes or []:
        if not isinstance(s, dict):
            continue
        narration = _extract_narration_from_voices(s.get("voices") or [])
        if narration:
            chunks.append(narration)
    return "\n\n".join(chunks)


def _word_count(text: str) -> int:
    # Match letter/number runs including apostrophes; ignore punctuation.
    return len(re.findall(r"[A-Za-z\u00C0-\u024F\u0400-\u04FF0-9][A-Za-z\u00C0-\u024F\u0400-\u04FF0-9'’\-]*", text))


# ---------------------------------------------------------------------------
# Structural checks
# ---------------------------------------------------------------------------


def check_duration_compliance(
    scenes: list[dict[str, Any]],
    target_duration_sec: float,
    *,
    tolerance: float = 0.05,
) -> CheckResult:
    """Sum of scene duration_sec must be >= target (within tolerance).

    PAG run: user asked for 7 minutes (420s), pipeline produced 3:50 (230s).
    Evaluator rated GOOD despite a 36% shortfall.  We cap at POOR on any
    shortfall beyond ``tolerance`` (default 5%).
    """
    total = 0.0
    for s in scenes or []:
        try:
            total += float(s.get("duration_sec", 0) or 0)
        except (TypeError, ValueError):
            continue

    target = max(0.0, float(target_duration_sec or 0))
    if target <= 0:
        # No target given — pass trivially but record.
        return CheckResult(
            name="duration_compliance",
            passed=True,
            verdict_cap="EXCELLENT",
            details=f"no target duration provided; sum={total:.1f}s",
            data={"sum_duration_sec": total, "target_duration_sec": target},
        )

    min_acceptable = target * (1.0 - tolerance)
    passed = total >= min_acceptable
    shortfall_pct = 0.0 if passed else (1.0 - total / target) * 100.0
    return CheckResult(
        name="duration_compliance",
        passed=passed,
        verdict_cap="POOR",
        details=(
            f"sum(duration_sec)={total:.1f}s vs target={target:.1f}s "
            f"(min acceptable={min_acceptable:.1f}s, "
            f"shortfall={shortfall_pct:.1f}%)"
        ),
        data={
            "sum_duration_sec": total,
            "target_duration_sec": target,
            "min_acceptable_sec": min_acceptable,
            "shortfall_pct": shortfall_pct,
        },
    )


def check_scene_count(
    scenes: list[dict[str, Any]],
    target_duration_sec: float,
    *,
    seconds_per_scene: int = 45,
) -> CheckResult:
    """At least ceil(target / seconds_per_scene) scenes.

    Fewer scenes = longer scenes = worse ADHD compliance + no variety.
    """
    target = max(0.0, float(target_duration_sec or 0))
    if target <= 0:
        return CheckResult(
            name="scene_count",
            passed=True,
            verdict_cap="EXCELLENT",
            details="no target duration; skipping",
            data={"count": len(scenes or [])},
        )

    minimum = max(1, math.ceil(target / seconds_per_scene))
    count = len(scenes or [])
    passed = count >= minimum
    return CheckResult(
        name="scene_count",
        passed=passed,
        verdict_cap="POOR",
        details=f"{count} scenes vs minimum {minimum} (target={target:.0f}s / {seconds_per_scene}s per scene)",
        data={"count": count, "minimum": minimum, "target_duration_sec": target},
    )


def check_word_count(
    scenes: list[dict[str, Any]],
    target_duration_sec: float,
    *,
    wpm: int = _WORDS_PER_MINUTE_DEFAULT,
) -> CheckResult:
    """Sum of narration words must be >= target/60 * wpm.

    This catches the class of failure where the LLM declares 45s of
    duration_sec but writes only 30s worth of words.  TTS will then
    either rush the delivery (unusable) or run short (documentary shorter
    than promised).
    """
    target = max(0.0, float(target_duration_sec or 0))
    if target <= 0:
        return CheckResult(
            name="word_count",
            passed=True,
            verdict_cap="EXCELLENT",
            details="no target duration; skipping",
        )

    narration = collect_narration(scenes or [])
    total_words = _word_count(narration)
    expected_words = math.ceil(target / 60.0 * wpm)
    passed = total_words >= expected_words
    return CheckResult(
        name="word_count",
        passed=passed,
        verdict_cap="POOR",
        details=(
            f"{total_words} words vs expected >= {expected_words} "
            f"(target={target:.0f}s @ {wpm} wpm)"
        ),
        data={
            "total_words": total_words,
            "expected_words": expected_words,
            "wpm": wpm,
            "target_duration_sec": target,
        },
    )


def check_hook_spec_present(scenes: list[dict[str, Any]], user_prompt: str = "") -> CheckResult:
    """Scene 0 must have a non-empty, topic-specific HookSpec.

    PAG run: opening was a generic blurry 3D brain — no tie to the actual
    subject (periaqueductal gray).  The hook must reference something
    concrete about the documentary's topic.
    """
    if not scenes:
        return CheckResult(
            name="hook_spec_present",
            passed=False,
            verdict_cap="POOR",
            details="no scenes",
        )

    scene0 = scenes[0]
    raw = scene0.get("hook_spec") if isinstance(scene0, dict) else None
    if not isinstance(raw, dict) or not raw:
        return CheckResult(
            name="hook_spec_present",
            passed=False,
            verdict_cap="POOR",
            details="scene 0 missing hook_spec",
        )

    try:
        spec = HookSpec.from_dict(raw)
    except ValueError as exc:
        return CheckResult(
            name="hook_spec_present",
            passed=False,
            verdict_cap="POOR",
            details=f"scene 0 hook_spec invalid: {exc}",
        )

    ok, reason = spec.is_valid(user_prompt=user_prompt)
    return CheckResult(
        name="hook_spec_present",
        passed=ok,
        verdict_cap="POOR",
        details=reason or "hook_spec present and non-empty",
        data={"motif": spec.topic_specific_motif},
    )


def check_outro_spec_present(scenes: list[dict[str, Any]]) -> CheckResult:
    """Final scene must have a non-empty OutroSpec.

    PAG run: documentary ended on a fade — no recap, no CTA, no brand
    card.  The final scene must have an explicit closing shot, recap
    sentence, CTA, and brand card.
    """
    if not scenes:
        return CheckResult(
            name="outro_spec_present",
            passed=False,
            verdict_cap="POOR",
            details="no scenes",
        )

    final = scenes[-1]
    raw = final.get("outro_spec") if isinstance(final, dict) else None
    if not isinstance(raw, dict) or not raw:
        return CheckResult(
            name="outro_spec_present",
            passed=False,
            verdict_cap="POOR",
            details="final scene missing outro_spec",
        )

    try:
        spec = OutroSpec.from_dict(raw)
    except ValueError as exc:
        return CheckResult(
            name="outro_spec_present",
            passed=False,
            verdict_cap="POOR",
            details=f"final scene outro_spec invalid: {exc}",
        )

    ok, reason = spec.is_valid()
    return CheckResult(
        name="outro_spec_present",
        passed=ok,
        verdict_cap="POOR",
        details=reason or "outro_spec present and non-empty",
        data={"closing_shot": spec.closing_shot[:80]},
    )


def check_style_lock_present(scenario: dict[str, Any]) -> CheckResult:
    """``scenario['style_lock']`` must parse and be non-empty.

    PAG run: visual whiplash because no global style was locked at
    scenario-creation time.  The scenario director MUST pick one style
    family up-front and emit a StyleLock alongside the scenes.
    """
    raw = scenario.get("style_lock") if isinstance(scenario, dict) else None
    if not isinstance(raw, dict) or not raw:
        return CheckResult(
            name="style_lock_present",
            passed=False,
            verdict_cap="POOR",
            details="scenario missing style_lock",
        )
    try:
        lock = StyleLock.from_dict(raw)
    except ValueError as exc:
        return CheckResult(
            name="style_lock_present",
            passed=False,
            verdict_cap="POOR",
            details=f"style_lock invalid: {exc}",
        )
    ok, reason = lock.is_valid()
    return CheckResult(
        name="style_lock_present",
        passed=ok,
        verdict_cap="POOR",
        details=reason or f"style_lock={lock.dominant_style}",
        data={
            "dominant_style": lock.dominant_style,
            "forbidden_count": len(lock.forbidden_styles),
        },
    )


# Obvious English words that look like initialisms but aren't.  Anything
# not in this list that is all-caps and >= 2 chars MUST appear in
# pronunciation_hints.  Keep this list small and uncontroversial — when in
# doubt, force the scenario director to declare a hint.
_ALL_CAPS_WHITELIST: frozenset[str] = frozenset(
    {
        "I",
        "A",
        "OK",
        "OH",
        "NO",
        "SO",
        "UP",
        "GO",
        "YES",
        "THE",
        "AND",
        "BUT",
        "FOR",
        "WHY",
        "HOW",
        "YOU",
        "NOW",
        "NEW",
    }
)
_ALL_CAPS_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]{1,}\b")


def check_pronunciation_hints_coverage(
    narration: str,
    hints: dict[str, str],
    *,
    whitelist: Optional[Iterable[str]] = None,
) -> CheckResult:
    """Every all-caps token >= 2 letters in narration must appear in hints.

    PAG run: TTS said "pag" (bag-rhyme) instead of P-A-G because the
    scenario didn't declare PAG as an initialism.  The scenario director
    is responsible for inventorying EVERY initialism / all-caps
    abbreviation in the narration up-front.
    """
    wl = frozenset(w.upper() for w in (whitelist or ())) | _ALL_CAPS_WHITELIST
    hints_upper = {k.upper() for k in (hints or {}).keys()}

    # Find every all-caps token. Use a set to dedupe, but preserve counts
    # for reporting.
    tokens: list[str] = _ALL_CAPS_TOKEN.findall(narration or "")
    missing: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        if tok in wl:
            continue
        if tok in hints_upper:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        missing.append(tok)

    passed = not missing
    return CheckResult(
        name="pronunciation_hints_coverage",
        # This is a cap at POOR (hard requirement): any unguarded initialism
        # is a likely mispronunciation and will be caught by the WhisperX
        # oracle later at real GPU cost.  Better to reject now.
        passed=passed,
        verdict_cap="POOR",
        details=(
            f"{len(missing)} unguarded all-caps token(s): {missing[:10]}"
            if missing
            else "all all-caps tokens have pronunciation hints"
        ),
        data={"missing": missing},
    )


# Heuristic patterns that almost always open a rhetorical question in
# English documentary narration.  These are NOT a full classifier — they
# are cheap pre-filters.  The caller can pass a ``classify`` callable for
# per-match LLM adjudication on ambiguous cases.
_RHETORICAL_OPENERS = (
    r"^\s*(?:what|why|how|who|where|when|which)\b",
    r"^\s*(?:can|could|would|should|will|won't|isn't|aren't|don't|doesn't|do)\s+we\b",
    r"^\s*(?:can|could|would|should|will|may|might)\s+(?:you|anyone|anything)\b",
    r"^\s*(?:imagine|consider|suppose|picture)\b",
    r"^\s*(?:what if|what happens when|what does it mean|how do we|how can we)\b",
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def _looks_rhetorical(question: str) -> bool:
    q = question.strip().lower()
    if not q.endswith("?"):
        return False
    for pat in _RHETORICAL_OPENERS:
        if re.match(pat, q):
            return True
    return False


def check_no_rhetorical_questions(
    narration: str,
    classify: Optional[Callable[[str], str]] = None,
) -> CheckResult:
    """Detect rhetorical questions in narration.

    PAG run: evaluator missed "What happens when...?" and "Can we
    harness...?" — both classic rhetorical openers.  We first apply a
    regex heuristic; if a ``classify`` callable is provided, ambiguous
    matches are upgraded/confirmed by LLM (per-match: "rhetorical" or
    "direct").
    """
    if not narration:
        return CheckResult(
            name="no_rhetorical_questions",
            passed=True,
            verdict_cap="POOR",
            details="empty narration",
        )

    # Split into sentences, find those ending in "?".
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(narration) if s and s.strip()]
    questions = [s for s in sentences if s.endswith("?")]

    rhetorical: list[str] = []
    for q in questions:
        heuristic = _looks_rhetorical(q)
        if classify is not None:
            try:
                verdict = classify(q).strip().lower()
            except Exception as exc:  # classify is user-supplied
                logger.warning("rhetorical classify() failed on %r: %s", q, exc)
                verdict = "rhetorical" if heuristic else "direct"
            if verdict.startswith("rhet"):
                rhetorical.append(q)
        elif heuristic:
            rhetorical.append(q)

    passed = not rhetorical
    return CheckResult(
        name="no_rhetorical_questions",
        passed=passed,
        verdict_cap="POOR",
        details=(
            f"{len(rhetorical)} rhetorical question(s) detected"
            if rhetorical
            else "no rhetorical questions detected"
        ),
        data={"rhetorical": rhetorical[:10], "total_questions": len(questions)},
    )


@dataclass
class TopicClassification:
    """Per-scene topic classification result."""

    scene_num: int
    verdict: str  # "on_topic" | "tangential" | "off_topic"
    reason: str = ""


def check_topic_fidelity(
    scenes: list[dict[str, Any]],
    user_prompt: str,
    classify: Optional[Callable[[str, str], TopicClassification]] = None,
    *,
    off_topic_threshold: int = 1,
    tangential_pct_threshold: float = 0.30,
) -> CheckResult:
    """Ensure the documentary stays on the user's topic.

    PAG run: ~40% of scenes were off-topic (EU parliament, cyberpunk
    cityscape, transhumanist cyborg, group prayer) while the user asked
    about the periaqueductal gray brain region.

    ``classify`` is an LLM-backed callable of (user_prompt, scene_text)
    -> TopicClassification.  When not provided, we return a passed=True
    result with a note — the caller is expected to wire the LLM in.  The
    unit test harness can pass a stub classifier.
    """
    if not user_prompt or not scenes:
        return CheckResult(
            name="topic_fidelity",
            passed=True,
            verdict_cap="POOR",
            details="no user_prompt or empty scenes; skipping",
        )

    if classify is None:
        # No classifier wired in.  We flag as "cannot evaluate" with
        # passed=False so the GOOD cap is actually applied by the
        # aggregator (``run_all_structural_checks`` only applies
        # verdict_cap on failing checks).  Without this, the overall
        # verdict could reach EXCELLENT despite no topic verification.
        # The caller (scenario evaluator) is expected to supply an LLM
        # classifier in production — when wired, this branch is skipped.
        return CheckResult(
            name="topic_fidelity",
            passed=False,
            verdict_cap="GOOD",
            details=(
                "no topic classifier supplied; capping at GOOD until a "
                "semantic fidelity check is wired in"
            ),
        )

    results: list[TopicClassification] = []
    for s in scenes:
        if not isinstance(s, dict):
            continue
        scene_num = int(s.get("scene_num", 0) or 0)
        narration = _extract_narration_from_voices(s.get("voices") or [])
        title = str(s.get("title", "") or "")
        visual = str(s.get("visual_notes", "") or "")
        scene_text = "\n".join(x for x in (title, narration, visual) if x)
        try:
            res = classify(user_prompt, scene_text)
        except Exception as exc:
            logger.warning("topic classify() raised on scene %d: %s", scene_num, exc)
            res = TopicClassification(scene_num=scene_num, verdict="tangential", reason=str(exc))
        # Normalize verdict just in case.
        res.verdict = res.verdict.lower().strip()
        if res.verdict not in {"on_topic", "tangential", "off_topic"}:
            res.verdict = "tangential"
        results.append(res)

    off = [r for r in results if r.verdict == "off_topic"]
    tang = [r for r in results if r.verdict == "tangential"]
    tang_pct = (len(tang) / len(results)) if results else 0.0

    passed = len(off) <= off_topic_threshold and tang_pct <= tangential_pct_threshold
    return CheckResult(
        name="topic_fidelity",
        passed=passed,
        verdict_cap="POOR",
        details=(
            f"off_topic={len(off)} (threshold<={off_topic_threshold}), "
            f"tangential={len(tang)}/{len(results)}={tang_pct:.0%} "
            f"(threshold<={tangential_pct_threshold:.0%})"
        ),
        data={
            "off_topic_scenes": [r.scene_num for r in off],
            "tangential_scenes": [r.scene_num for r in tang],
            "tangential_pct": tang_pct,
        },
    )


def check_style_consistency(scenes: list[dict[str, Any]], style_lock: dict[str, Any]) -> CheckResult:
    """No scene's visual_notes may mention any forbidden_style keyword.

    This is a cheap post-hoc check that catches the case where the
    scenario director set a StyleLock but then wrote "anime-style"
    inside a scene's visual_notes anyway.
    """
    try:
        lock = StyleLock.from_dict(style_lock or {})
    except ValueError:
        return CheckResult(
            name="style_consistency",
            passed=False,
            verdict_cap="POOR",
            details="style_lock missing or invalid",
        )

    forbidden = {f.lower() for f in lock.forbidden_styles}
    if not forbidden:
        forbidden = set(DEFAULT_FORBIDDEN_STYLES)

    violations: list[dict[str, Any]] = []
    for s in scenes or []:
        if not isinstance(s, dict):
            continue
        text_blob = " ".join(
            str(s.get(k, "") or "") for k in ("visual_notes", "title", "dopamine_hook")
        ).lower()
        for word in forbidden:
            if re.search(rf"\b{re.escape(word)}\b", text_blob):
                violations.append(
                    {"scene_num": s.get("scene_num"), "forbidden_word": word}
                )
                break

    passed = not violations
    return CheckResult(
        name="style_consistency",
        passed=passed,
        verdict_cap="POOR",
        details=(
            f"{len(violations)} scene(s) reference forbidden style keywords"
            if violations
            else f"no forbidden style keywords in visual_notes "
                 f"(lock={lock.dominant_style})"
        ),
        data={"violations": violations},
    )


# ---------------------------------------------------------------------------
# Aggregate runner
# ---------------------------------------------------------------------------


@dataclass
class EvaluatorReport:
    """Aggregate result of running every structural check."""

    overall: str  # final capped verdict
    results: list[CheckResult]

    def failed(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "overall_cap": self.overall,
            "results": [r.as_dict() for r in self.results],
            "failed_count": len(self.failed()),
        }


def run_all_structural_checks(
    scenario: dict[str, Any],
    *,
    user_prompt: str = "",
    target_duration_sec: float = 0.0,
    wpm: int = _WORDS_PER_MINUTE_DEFAULT,
    seconds_per_scene: int = 45,
    rhetorical_classify: Optional[Callable[[str], str]] = None,
    topic_classify: Optional[Callable[[str, str], TopicClassification]] = None,
    pronunciation_whitelist: Optional[Iterable[str]] = None,
    start_verdict: str = "EXCELLENT",
) -> EvaluatorReport:
    """Run every structural check and aggregate the verdict.

    ``scenario`` is expected to be a dict with at least:
      * scenes: list[dict]
      * style_lock: dict (optional at call time, required to pass)
    """
    scenes = list(scenario.get("scenes") or []) if isinstance(scenario, dict) else []
    style_lock = scenario.get("style_lock") if isinstance(scenario, dict) else None

    # Aggregate pronunciation_hints across all scenes for the coverage check.
    aggregate_hints: dict[str, str] = {}
    for s in scenes:
        if isinstance(s, dict):
            aggregate_hints.update(scene_pronunciation_hints(s))
    # Also honor a scenario-level aggregate hint dict if the generator
    # wrote one.
    if isinstance(scenario, dict):
        top_level = scenario.get("pronunciation_hints") or {}
        if isinstance(top_level, dict):
            aggregate_hints.update({str(k): str(v) for k, v in top_level.items() if k and v})

    narration = collect_narration(scenes)

    results: list[CheckResult] = [
        check_duration_compliance(scenes, target_duration_sec),
        check_scene_count(scenes, target_duration_sec, seconds_per_scene=seconds_per_scene),
        check_word_count(scenes, target_duration_sec, wpm=wpm),
        check_hook_spec_present(scenes, user_prompt=user_prompt),
        check_outro_spec_present(scenes),
        check_style_lock_present(scenario if isinstance(scenario, dict) else {}),
        check_style_consistency(scenes, style_lock or {}),
        check_pronunciation_hints_coverage(
            narration,
            aggregate_hints,
            whitelist=pronunciation_whitelist,
        ),
        check_no_rhetorical_questions(narration, classify=rhetorical_classify),
        check_topic_fidelity(scenes, user_prompt, classify=topic_classify),
    ]

    overall = start_verdict
    for r in results:
        if not r.passed:
            overall = cap_verdict(overall, r.verdict_cap)

    return EvaluatorReport(overall=overall, results=results)


def format_report(report: EvaluatorReport) -> str:
    """Human-readable, LLM-ingestible summary of a report."""
    lines = [f"OVERALL_CAP: {report.overall}", ""]
    for r in report.results:
        status = "PASS" if r.passed else f"FAIL (cap={r.verdict_cap})"
        lines.append(f"[{status}] {r.name}: {r.details}")
    failed = report.failed()
    if failed:
        lines.append("")
        lines.append("FAILURES REQUIRE REVISION:")
        for r in failed:
            lines.append(f"  - {r.name}: {r.details}")
    return "\n".join(lines)
