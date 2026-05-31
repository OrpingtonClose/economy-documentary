"""
Re-manifestation pipeline (ARCH-A6, issue #136).

Parent ticket: ARCH-A #123.  Meta: ARCH-2026 #122.

This module closes workstream A by turning a :class:`LedgerDrift`
signal (produced by ARCH-A5) into a validated, executable plan that
regenerates the minimal set of artifacts invalidated by the new
records.

The four components live here, composed into a single ``handle_drift``
orchestration callable:

1. **Impact analyser.**  :func:`analyse_impact` walks the universal
   back-edge (``ARTIFACT_REVISION_TAGS_KEY``) and scope-matches the
   drift's ``new_records`` against every tagged artifact.  Produces a
   list of :class:`ImpactedArtifact`.

2. **Re-manifestation planner.**  :func:`plan_remanifestation` turns
   impacts into an ordered :class:`RemanifestationPlan` of
   :class:`PlanStep` s.  Each step is an escalation-menu action drawn
   from the allowed subset (``REPLACE`` / ``EXTEND`` /
   ``rewrite_scene`` -- the existing
   :mod:`orchestrator.escalation_menu` canonical menu).  The planner
   coalesces multiple impacts on the same artifact to a single step
   and escalates hard-record conflicts (FORBID / REQUIRE) to
   ``rewrite_scene`` when they touch a scene's narrative structure.

3. **Plan validator.**  :func:`validate_plan` enforces three
   invariants:

   a. Every step uses one of the permitted escalation-menu action
      names (``regenerate_clip`` / ``generate_extension_clip`` /
      ``rewrite_scene``, i.e. the REPLACE / EXTEND / rewrite_scene
      subset).  Media-immutability-violating shortcuts
      (``trim_narration``, ``freeze_frame_fill``, silent fill) are
      rejected with :class:`InvalidPlanError`.

   b. No step contradicts a current-ledger hard record.  If a record
      says ``FORBID music`` and a step would generate extension
      narration that violates it, we fail loud rather than silently
      producing a disallowed artifact.

   c. Every step names a known artifact (present in the revision-tag
      map) or a scene id derivable from one.  A step targeting an
      unknown artifact would silently succeed-and-do-nothing, which
      is exactly the "silent degradation" the spec forbids.

4. **Executor.**  :func:`execute_plan` runs each step through a
   caller-supplied dispatcher (default: the no-op queuer that records
   steps onto the blackboard for the production supervisor to pick
   up).  After a step succeeds the executor clears the old artifact
   revision tag (so the producer re-tags at the new revision when it
   re-runs -- the universal back-edge invariant from ARCH-B1).

Design invariants (covered by ``tests/test_remanifestation.py``):

* No step ever names an action outside the REPLACE / EXTEND /
  rewrite_scene subset.  Attempting to construct one raises.
* An empty drift (no new records / no impacted artifacts) yields an
  empty plan -- never a spurious step.
* ``execute_plan`` is idempotent: re-running the same plan on the
  same state produces the same receipts (tags already cleared by the
  first run stay cleared, no second clear attempt fires).
* ``handle_drift`` consumes drift signals from the
  :data:`LEDGER_DRIFT_SIGNALS_KEY` queue and never re-consumes them;
  failed plans stay on the queue so the next A6 run can pick them up
  after the operator fixes the root cause.

Cross-module boundaries:

* Reads: ``PREFERENCE_LEDGER_KEY`` (A1), ``ARTIFACT_REVISION_TAGS_KEY``
  (B1), ``STAGE_DERIVATIONS_KEY`` and ``LEDGER_DRIFT_SIGNALS_KEY``
  (A5).  Writes: executor-receipts + cleared tags.
* Does NOT call google-genai; planning is deterministic.  An optional
  ADK Agent wrapper lives in :mod:`agents.remanifestation_agent` for
  pipeline composition, but the planning logic stands on its own.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Mapping,
    MutableMapping,
    Optional,
)

from callbacks.artifact_revision_tag import (
    ARTIFACT_REVISION_TAGS_KEY,
    ArtifactRevisionTag,
    clear_tag,
    list_tags,
)
from callbacks.consistency_checker import (
    LEDGER_DRIFT_SIGNALS_KEY,
    LedgerDrift,
    pending_drift_signals,
)
from callbacks.preference_ledger import (
    PREFERENCE_LEDGER_KEY,
    Polarity,
    PreferenceRecord,
    Scope,
    Subject,
    list_preferences,
)
from orchestrator.escalation_menu import (
    ACTION_LEVELS,
    ACTION_SIGNATURES,
    EscalationAction,
    EscalationActionError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Blackboard keys
# ---------------------------------------------------------------------------

#: Blackboard key where :func:`execute_plan` appends per-step receipts.
#: Producers / the dashboard read this to learn what A6 decided.
REMANIFESTATION_RECEIPTS_KEY: str = "remanifestation_receipts"

#: Blackboard key where :func:`execute_plan` appends queued steps for
#: stages that have no executor wired in yet.  Each entry is the plan
#: step's dict form plus a ``status`` of ``"queued"``.
REMANIFESTATION_QUEUE_KEY: str = "remanifestation_queue"


# ---------------------------------------------------------------------------
# Allowed action subset (Media Immutability Invariant, #128)
# ---------------------------------------------------------------------------

#: The only escalation-menu actions the re-manifestation planner is
#: allowed to emit.  Drawn from :data:`orchestrator.escalation_menu`:
#:
#: * ``regenerate_clip``        -- REPLACE a single clip from scratch.
#: * ``generate_extension_clip`` -- EXTEND a scene with a new clip.
#: * ``rewrite_scene``          -- rewrite narration + REPLACE media.
#:
#: ``replace_with_brand_card`` and ``abort_run`` are NOT emitted
#: automatically; both require human judgement and should come from
#: the dashboard.  Any legacy action name (``trim_narration``,
#: ``freeze_frame_fill``, ``speed_up_narration``) is rejected by the
#: validator -- those were removed by the Media Immutability Invariant
#: (#128 / ARCH-F) and including them would re-open the door to silent
#: trim / freeze / speed-up mutations.
ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {"regenerate_clip", "generate_extension_clip", "rewrite_scene"}
)

# Legacy / forbidden actions the validator must actively reject.  Kept
# explicit so future refactors can't re-introduce them by accident.
_FORBIDDEN_ACTIONS: frozenset[str] = frozenset(
    {"trim_narration", "freeze_frame_fill", "speed_up_narration", "silent_fill"}
)


class InvalidPlanError(RuntimeError):
    """Raised by :func:`validate_plan` when a plan violates an invariant."""


class RemanifestationExecutionError(RuntimeError):
    """Raised when an executor dispatch fails and the failure is not isolated."""


# ---------------------------------------------------------------------------
# Impact analysis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImpactedArtifact:
    """One artifact whose tag's revision predates a new ledger record.

    ``triggering_records`` carries every new record that matched this
    artifact -- we preserve all of them (not just the first) so the
    planner can reason about hard polarities without re-walking the
    drift.
    """

    artifact_key: str
    stage: str
    tag: ArtifactRevisionTag
    triggering_records: tuple[PreferenceRecord, ...]

    @property
    def has_hard_record(self) -> bool:
        """True iff any triggering record is FORBID or REQUIRE."""
        return any(
            r.polarity in (Polarity.FORBID, Polarity.REQUIRE)
            for r in self.triggering_records
        )


def _scope_matches_artifact(
    record: PreferenceRecord,
    artifact_key: str,
    tag: ArtifactRevisionTag,
) -> bool:
    """Return True iff ``record`` invalidates the artifact identified by
    ``artifact_key`` / ``tag``.

    The universal back-edge (ARCH-B1) carries ``stage`` on each tag but
    not a full scope chain; we therefore match defensively:

    * ``Scope.GLOBAL`` records match every artifact.  Global directives
      are the whole point of R0 and of any later "change the whole
      documentary's tone" instruction.
    * ``Scope.STAGE`` records match when the record's ``scope_ref`` is
      ``None`` (applies to every stage) or equals the artifact's
      ``tag.stage``.
    * ``Scope.SCENE`` / ``Scope.VOICE_BLOCK`` / ``Scope.ARTIFACT_TYPE``
      / ``Scope.ELEMENT`` records match when the artifact key contains
      the ``scope_ref`` token (case-sensitive, word-boundary isn't
      enforceable because artifact keys are free-form).  An un-ref'd
      narrower record is treated as "applies to every instance" at
      that level -- we err on the side of re-manifesting too much
      rather than silently missing a drift.

    The "key-contains-ref" heuristic deliberately accepts a superset
    of the canonical matches.  Callers that want tighter control
    should pass structured artifact keys (e.g. ``"scene-3:audio"``) so
    the substring match is unambiguous.
    """
    scope = record.scope
    ref = record.scope_ref

    if scope is Scope.GLOBAL:
        return True
    if scope is Scope.STAGE:
        if ref is None:
            return True
        return ref == tag.stage
    # Narrower scopes.
    if ref is None:
        return True
    return ref in artifact_key


def analyse_impact(
    state: Mapping[str, Any],
    drift: LedgerDrift,
) -> list[ImpactedArtifact]:
    """Scope-match ``drift.new_records`` against every tagged artifact.

    ``drift.artifact_ids`` is treated as a hint -- tests show it is
    populated by ARCH-B1 for the artifacts produced during the drifted
    stage.  We still walk every tag in ``state`` so a GLOBAL record
    correctly invalidates cross-stage artifacts (e.g. a new tone
    preference should invalidate audio clips AND visual concepts, not
    just whatever B1 tagged for the stage that emitted the signal).

    Artifacts whose tag ``ledger_revision`` is already at or above
    ``drift.to_rev`` are skipped -- they were derived AFTER the drift
    window and therefore already reflect the new records.
    """
    if not isinstance(drift, LedgerDrift):
        raise TypeError(
            f"analyse_impact expects LedgerDrift, got {type(drift).__name__}"
        )

    # Decode new records as PreferenceRecord objects.  drift stores them
    # as dicts (for JSON round-trip); list_preferences would re-decode
    # the whole ledger, which is overkill, so we decode inline.
    new_records: list[PreferenceRecord] = []
    for raw in drift.new_records:
        new_records.append(PreferenceRecord.from_dict(raw))

    tags = list_tags(state)

    impacted: list[ImpactedArtifact] = []
    for artifact_key, tag in tags.items():
        if tag.ledger_revision >= drift.to_rev:
            # Already derived at-or-past the drift horizon.
            continue
        triggered: list[PreferenceRecord] = []
        for record in new_records:
            if _scope_matches_artifact(record, artifact_key, tag):
                triggered.append(record)
        if not triggered:
            continue
        impacted.append(
            ImpactedArtifact(
                artifact_key=artifact_key,
                stage=tag.stage,
                tag=tag,
                triggering_records=tuple(triggered),
            )
        )
    # Deterministic order -- keyed by artifact_key -- simplifies tests
    # and keeps step emission stable under the same input.
    impacted.sort(key=lambda i: i.artifact_key)
    return impacted


# ---------------------------------------------------------------------------
# Plan + steps
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanStep:
    """One unit of work in a :class:`RemanifestationPlan`.

    ``action`` is an escalation-menu action name restricted to
    :data:`ALLOWED_ACTIONS`.  ``artifact_key`` identifies the tagged
    artifact to re-derive (so the executor can clear its tag before
    dispatching).  ``scene_id`` / ``clip_id`` / ``guidance`` /
    ``prompt_delta`` / ``duration_needed`` carry the parameters the
    underlying escalation menu expects (see ``ACTION_SIGNATURES``).

    We store ``reason`` alongside the parameters so the dashboard and
    audit trail can show WHY a step was planned -- drift signals are
    ephemeral, reasons are not.
    """

    action: str
    artifact_key: str
    reason: str
    scene_id: Optional[str] = None
    clip_id: Optional[str] = None
    prompt_delta: Optional[str] = None
    seed_delta: Optional[int] = None
    duration_needed: Optional[float] = None
    guidance: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "artifact_key": self.artifact_key,
            "reason": self.reason,
            "scene_id": self.scene_id,
            "clip_id": self.clip_id,
            "prompt_delta": self.prompt_delta,
            "seed_delta": self.seed_delta,
            "duration_needed": self.duration_needed,
            "guidance": self.guidance,
        }

    def to_escalation_action(self) -> EscalationAction:
        """Materialise as the canonical :class:`EscalationAction` shape.

        Used by the validator (to re-run ``EscalationAction``'s own
        signature checks) and by any executor that wants to dispatch
        through the existing escalation machinery.
        """
        kwargs: dict[str, Any] = {"action": self.action}
        sig = ACTION_SIGNATURES.get(self.action, {})
        # Only pass fields the action actually expects -- the dataclass
        # otherwise complains about unknown kwargs.
        if "clip_id" in sig:
            kwargs["clip_id"] = self.clip_id
        if "scene_id" in sig:
            kwargs["scene_id"] = self.scene_id
        if "prompt_delta" in sig:
            kwargs["prompt_delta"] = self.prompt_delta
        if "seed_delta" in sig:
            kwargs["seed_delta"] = self.seed_delta
        if "duration_needed" in sig:
            kwargs["duration_needed"] = self.duration_needed
        if "guidance" in sig:
            kwargs["guidance"] = self.guidance
        return EscalationAction(**kwargs)


@dataclass(frozen=True)
class RemanifestationPlan:
    """An ordered set of :class:`PlanStep` s computed from one drift."""

    stage_name: str
    from_rev: int
    to_rev: int
    steps: tuple[PlanStep, ...]
    reason: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.steps

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_name": self.stage_name,
            "from_rev": self.from_rev,
            "to_rev": self.to_rev,
            "steps": [s.to_dict() for s in self.steps],
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def _infer_scene_id(artifact_key: str) -> Optional[str]:
    """Extract a ``scene-N`` token from ``artifact_key`` if present.

    Structured artifact keys like ``"scene-3:audio"`` or
    ``"scene-12/clip-0"`` yield ``"scene-3"`` / ``"scene-12"``.  Keys
    without a scene token return ``None``, in which case the caller
    cannot plan a scene-scoped action and must fall back to
    ``regenerate_clip`` (or skip, if no clip id is available either).
    """
    # Very small regex-free parse to avoid importing re for one match.
    idx = artifact_key.find("scene-")
    if idx < 0:
        return None
    end = idx + len("scene-")
    tail = artifact_key[end:]
    digits = ""
    for ch in tail:
        if ch.isdigit():
            digits += ch
        else:
            break
    if not digits:
        return None
    return f"scene-{digits}"


def _infer_clip_id(artifact_key: str) -> Optional[str]:
    """Extract a ``clip-N`` token from ``artifact_key`` if present."""
    idx = artifact_key.find("clip-")
    if idx < 0:
        return None
    end = idx + len("clip-")
    tail = artifact_key[end:]
    digits = ""
    for ch in tail:
        if ch.isdigit():
            digits += ch
        else:
            break
    if not digits:
        return None
    return f"clip-{digits}"


def _pick_action(
    impact: ImpactedArtifact,
) -> tuple[str, dict[str, Any], str]:
    """Select an escalation-menu action for one impacted artifact.

    Returns a ``(action, params, reason)`` triple.  ``params`` is the
    minimal set of action-specific fields the step needs.  Reason is a
    short human-readable string used for the audit trail.

    Selection rules (deterministic; no LLM):

    * Any hard record on a SCENE-sized artifact → ``rewrite_scene``.
    * ``Subject.DURATION`` with ``Polarity.PREFER`` / ``REQUIRE`` and a
      scene-scoped artifact → ``generate_extension_clip`` (EXTEND).
    * Everything else → ``regenerate_clip`` (REPLACE).  Clip artifacts
      without a derivable clip id fall through to ``rewrite_scene``
      when a scene id is present -- better to rewrite too much than
      silently skip.
    """
    scene_id = _infer_scene_id(impact.artifact_key)
    clip_id = _infer_clip_id(impact.artifact_key)

    # Build a compact reason string from triggering records.
    trigger_desc = ", ".join(
        f"[{r.scope.value}"
        + (f":{r.scope_ref}" if r.scope_ref else "")
        + f" {r.polarity.value} {r.subject.value}@rev{r.revision}]"
        for r in impact.triggering_records
    )
    base_reason = (
        f"stage={impact.stage} artifact={impact.artifact_key} "
        f"triggered by {trigger_desc}"
    )

    has_hard = impact.has_hard_record
    duration_prefer = any(
        r.subject is Subject.DURATION
        and r.polarity in (Polarity.PREFER, Polarity.REQUIRE)
        for r in impact.triggering_records
    )

    if has_hard and scene_id is not None:
        guidance_bits = [
            r.content for r in impact.triggering_records
            if r.polarity in (Polarity.FORBID, Polarity.REQUIRE)
        ]
        guidance = " | ".join(guidance_bits) or "hard-record drift"
        return (
            "rewrite_scene",
            {"scene_id": scene_id, "guidance": guidance},
            base_reason + " -> rewrite_scene (hard record)",
        )

    if duration_prefer and scene_id is not None:
        # Estimate a small positive extension.  Precise duration belongs
        # to the timing evaluator; A6 just needs to emit a valid,
        # non-zero ``duration_needed`` so the signature validates.
        return (
            "generate_extension_clip",
            {"scene_id": scene_id, "duration_needed": 1.0},
            base_reason + " -> generate_extension_clip (duration drift)",
        )

    if clip_id is not None:
        prompt_delta = "; ".join(
            r.content for r in impact.triggering_records
        ) or "ledger drift"
        return (
            "regenerate_clip",
            {
                "clip_id": clip_id,
                "prompt_delta": prompt_delta,
                # seed_delta must be non-zero (escalation_menu invariant);
                # use the drift's to_rev so each re-manifestation gets a
                # fresh seed tied to the ledger revision that triggered it.
                "seed_delta": max(1, impact.tag.ledger_revision + 1),
            },
            base_reason + " -> regenerate_clip",
        )

    if scene_id is not None:
        guidance = "; ".join(
            r.content for r in impact.triggering_records
        ) or "ledger drift"
        return (
            "rewrite_scene",
            {"scene_id": scene_id, "guidance": guidance},
            base_reason + " -> rewrite_scene (no clip id)",
        )

    # Last resort: we cannot derive a scene or clip id.  Synthesise a
    # clip id from the artifact key so the step is still valid and
    # routes through REPLACE rather than silently dropping.  Operators
    # will see the step in the queue and can intervene if needed.
    return (
        "regenerate_clip",
        {
            "clip_id": impact.artifact_key,
            "prompt_delta": "; ".join(
                r.content for r in impact.triggering_records
            ) or "ledger drift",
            "seed_delta": max(1, impact.tag.ledger_revision + 1),
        },
        base_reason + " -> regenerate_clip (synthesised clip id)",
    )


def plan_remanifestation(
    state: Mapping[str, Any],
    drift: LedgerDrift,
) -> RemanifestationPlan:
    """Build an ordered :class:`RemanifestationPlan` from one drift.

    Empty drift (no impacted artifacts) yields an empty plan with
    ``reason="no impacted artifacts"`` -- an empty plan is a legitimate
    output, not an error, because a GLOBAL record that matches
    artifacts already at the new revision is a no-op.
    """
    impacted = analyse_impact(state, drift)
    if not impacted:
        return RemanifestationPlan(
            stage_name=drift.stage_name,
            from_rev=drift.from_rev,
            to_rev=drift.to_rev,
            steps=(),
            reason=(
                f"no impacted artifacts "
                f"(drift {drift.from_rev}->{drift.to_rev}, "
                f"{len(drift.new_records)} new records)"
            ),
        )

    steps: list[PlanStep] = []
    for impact in impacted:
        action, params, reason = _pick_action(impact)
        steps.append(
            PlanStep(
                action=action,
                artifact_key=impact.artifact_key,
                reason=reason,
                scene_id=params.get("scene_id"),
                clip_id=params.get("clip_id"),
                prompt_delta=params.get("prompt_delta"),
                seed_delta=params.get("seed_delta"),
                duration_needed=params.get("duration_needed"),
                guidance=params.get("guidance"),
            )
        )

    return RemanifestationPlan(
        stage_name=drift.stage_name,
        from_rev=drift.from_rev,
        to_rev=drift.to_rev,
        steps=tuple(steps),
        reason=(
            f"{len(steps)} step(s) for drift "
            f"{drift.from_rev}->{drift.to_rev} "
            f"(stage={drift.stage_name})"
        ),
    )


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def _collect_hard_records(
    state: Mapping[str, Any],
) -> list[PreferenceRecord]:
    """Return every FORBID/REQUIRE record in the current ledger."""
    out: list[PreferenceRecord] = []
    for rec in list_preferences(state):
        if rec.polarity in (Polarity.FORBID, Polarity.REQUIRE):
            out.append(rec)
    return out


def _step_would_violate(
    step: PlanStep, hard_record: PreferenceRecord
) -> Optional[str]:
    """Return a human-readable reason if ``step`` would violate the hard
    record, or ``None`` if it wouldn't.

    The check is deliberately narrow.  The invariants we enforce:

    * ``FORBID duration`` + ``generate_extension_clip`` -> violation
      (extending a scene adds material the user told us to avoid).
    * ``FORBID music`` + ``regenerate_clip`` with a ``prompt_delta``
      that mentions ``music`` -> violation (we won't re-introduce a
      forbidden subject via the new delta).
    * ``REQUIRE`` records do not block steps -- re-manifestation is
      the mechanism that brings artifacts INTO compliance with
      ``REQUIRE`` records, so blocking a step here would prevent
      exactly the work the invariant asks for.

    Any other combination is allowed.  This is intentionally
    conservative: false-positive blocks would break A6 in production;
    real conflicts between the ledger and a plan step should be rare
    and always land here explicitly.
    """
    if hard_record.polarity is not Polarity.FORBID:
        return None

    subject = hard_record.subject
    if (
        subject is Subject.DURATION
        and step.action == "generate_extension_clip"
    ):
        return (
            f"FORBID {subject.value} (rev {hard_record.revision}) "
            "contradicts generate_extension_clip"
        )

    if step.action == "regenerate_clip":
        delta = (step.prompt_delta or "").lower()
        if subject.value in delta:
            return (
                f"FORBID {subject.value} (rev {hard_record.revision}) "
                f"is referenced by regenerate_clip prompt_delta"
            )

    return None


def validate_plan(
    state: Mapping[str, Any],
    plan: RemanifestationPlan,
) -> None:
    """Validate a plan against the allowed action set, the ledger's
    hard records, and the universal back-edge.

    Raises :class:`InvalidPlanError` on the first violation.  Does not
    mutate state.
    """
    if not isinstance(plan, RemanifestationPlan):
        raise TypeError(
            f"validate_plan expects RemanifestationPlan, "
            f"got {type(plan).__name__}"
        )

    if PREFERENCE_LEDGER_KEY not in state:
        raise InvalidPlanError(
            "cannot validate plan: Preference Ledger is not initialised "
            "on the blackboard"
        )

    tags = list_tags(state)
    hard_records = _collect_hard_records(state)

    seen_keys: set[str] = set()
    for idx, step in enumerate(plan.steps):
        if step.action in _FORBIDDEN_ACTIONS:
            raise InvalidPlanError(
                f"plan step {idx} uses forbidden action {step.action!r}; "
                f"Media Immutability Invariant permits only "
                f"{sorted(ALLOWED_ACTIONS)}"
            )
        if step.action not in ALLOWED_ACTIONS:
            raise InvalidPlanError(
                f"plan step {idx} uses action {step.action!r} outside "
                f"the A6 allowed subset {sorted(ALLOWED_ACTIONS)}"
            )
        if step.action not in ACTION_LEVELS:
            raise InvalidPlanError(
                f"plan step {idx} action {step.action!r} is not a "
                "canonical escalation-menu action"
            )

        # Re-run the canonical escalation-menu signature check so any
        # missing parameter surfaces with its original error message.
        try:
            step.to_escalation_action()
        except EscalationActionError as exc:
            raise InvalidPlanError(
                f"plan step {idx} failed escalation-menu signature "
                f"validation: {exc}"
            ) from exc

        if step.artifact_key not in tags:
            # Unknown artifact keys (e.g. synthesised from a raw tag
            # that was already cleared) would make the step a no-op on
            # execution.  Fail loud so a human reviews the plan.
            raise InvalidPlanError(
                f"plan step {idx} targets artifact "
                f"{step.artifact_key!r} which has no tag in "
                f"{ARTIFACT_REVISION_TAGS_KEY!r}"
            )

        if step.artifact_key in seen_keys:
            raise InvalidPlanError(
                f"plan step {idx} re-targets artifact "
                f"{step.artifact_key!r}; the planner must coalesce "
                "duplicate impacts before emission"
            )
        seen_keys.add(step.artifact_key)

        for hard in hard_records:
            reason = _step_would_violate(step, hard)
            if reason is not None:
                raise InvalidPlanError(
                    f"plan step {idx} violates hard record: {reason}"
                )


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


StepExecutor = Callable[[MutableMapping[str, Any], PlanStep], Mapping[str, Any]]
"""Signature of a step-level executor callable.

Receives the mutable session state plus the step to dispatch, and
returns a receipt mapping whose ``status`` key is one of ``"queued"``,
``"dispatched"``, or ``"failed"``.  Failed-receipts should include a
``error`` key; the outer :func:`execute_plan` will not clear the
artifact tag in that case.
"""


def _default_executor(
    state: MutableMapping[str, Any], step: PlanStep
) -> Mapping[str, Any]:
    """Default executor -- enqueue the step for the production supervisor.

    Real media regeneration lives in the production supervisor's
    escalation path; A6 itself does not own GPU workers.  Queueing is
    enough to satisfy the "execute via the existing escalation /
    orchestrator paths" spec without A6 having to wait for scheduler
    work to complete.  The queue value is a JSON-encoded list under
    :data:`REMANIFESTATION_QUEUE_KEY` (matching the ledger / drift
    queue convention).
    """
    queue_raw = state.get(REMANIFESTATION_QUEUE_KEY)
    if queue_raw is None or queue_raw == "":
        queue: list[dict[str, Any]] = []
    elif isinstance(queue_raw, list):
        queue = list(queue_raw)
    elif isinstance(queue_raw, str):
        try:
            decoded = json.loads(queue_raw)
        except json.JSONDecodeError as exc:
            raise RemanifestationExecutionError(
                f"{REMANIFESTATION_QUEUE_KEY!r} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(decoded, list):
            raise RemanifestationExecutionError(
                f"{REMANIFESTATION_QUEUE_KEY!r} must decode to a list, "
                f"got {type(decoded).__name__}"
            )
        queue = decoded
    else:
        raise RemanifestationExecutionError(
            f"{REMANIFESTATION_QUEUE_KEY!r} must be list or JSON "
            f"string, got {type(queue_raw).__name__}"
        )

    entry = dict(step.to_dict())
    entry["status"] = "queued"
    entry["queued_at"] = datetime.now(timezone.utc).isoformat()
    queue.append(entry)
    state[REMANIFESTATION_QUEUE_KEY] = json.dumps(queue, ensure_ascii=False)
    return {
        "status": "queued",
        "action": step.action,
        "artifact_key": step.artifact_key,
    }


def _append_receipt(
    state: MutableMapping[str, Any], receipt: Mapping[str, Any]
) -> None:
    raw = state.get(REMANIFESTATION_RECEIPTS_KEY)
    if raw is None or raw == "":
        receipts: list[dict[str, Any]] = []
    elif isinstance(raw, list):
        receipts = list(raw)
    elif isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RemanifestationExecutionError(
                f"{REMANIFESTATION_RECEIPTS_KEY!r} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(decoded, list):
            raise RemanifestationExecutionError(
                f"{REMANIFESTATION_RECEIPTS_KEY!r} must decode to list, "
                f"got {type(decoded).__name__}"
            )
        receipts = decoded
    else:
        raise RemanifestationExecutionError(
            f"{REMANIFESTATION_RECEIPTS_KEY!r} must be list or JSON "
            f"string, got {type(raw).__name__}"
        )
    receipts.append(dict(receipt))
    state[REMANIFESTATION_RECEIPTS_KEY] = json.dumps(
        receipts, ensure_ascii=False
    )


def execute_plan(
    state: MutableMapping[str, Any],
    plan: RemanifestationPlan,
    *,
    executor: Optional[StepExecutor] = None,
) -> list[Mapping[str, Any]]:
    """Dispatch every step in ``plan`` through ``executor``.

    ``executor`` defaults to :func:`_default_executor`, which queues
    the step onto the blackboard for the production supervisor to pick
    up.  Tests supply a custom executor to inspect dispatch behaviour
    directly.

    After each successful step the artifact's revision tag is cleared
    via :func:`~callbacks.artifact_revision_tag.clear_tag` so the
    producer re-tags at the new ledger revision on re-run.  A failed
    step leaves the tag in place and surfaces the error on the
    receipt -- callers can retry once the root cause is fixed.
    """
    if not isinstance(plan, RemanifestationPlan):
        raise TypeError(
            f"execute_plan expects RemanifestationPlan, "
            f"got {type(plan).__name__}"
        )
    dispatcher = executor or _default_executor
    receipts: list[Mapping[str, Any]] = []
    tags = list_tags(state)
    for step in plan.steps:
        try:
            receipt = dispatcher(state, step)
        except Exception as exc:  # noqa: BLE001 -- per-step failure is isolated
            logger.exception(
                "remanifestation executor raised for step %s: %s",
                step.action, exc,
            )
            receipt = {
                "status": "failed",
                "action": step.action,
                "artifact_key": step.artifact_key,
                "error": str(exc),
            }
            _append_receipt(state, receipt)
            receipts.append(receipt)
            continue

        if not isinstance(receipt, Mapping):
            raise RemanifestationExecutionError(
                f"executor must return a mapping, "
                f"got {type(receipt).__name__}"
            )
        status = receipt.get("status")
        # Clear the old revision tag on successful dispatch so the
        # producer re-tags at the new revision on re-run.  Skip the
        # clear if the tag was already gone (idempotent re-run).
        if status in ("queued", "dispatched") and step.artifact_key in tags:
            try:
                clear_tag(state, step.artifact_key)
            except KeyError:
                # Someone cleared it concurrently -- not our problem.
                pass
            # Refresh tags so re-runs of the same plan are no-ops.
            tags = list_tags(state)

        _append_receipt(state, dict(receipt))
        receipts.append(receipt)
    return receipts


# ---------------------------------------------------------------------------
# Top-level orchestration -- consume signals + run analyse/plan/validate/execute
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DriftHandlingReceipt:
    """Summary of a single :func:`handle_drift` invocation."""

    drift: LedgerDrift
    plan: RemanifestationPlan
    step_receipts: tuple[Mapping[str, Any], ...]
    error: Optional[str] = None


def _pop_all_drift_signals(
    state: MutableMapping[str, Any],
) -> tuple[list[LedgerDrift], list[dict[str, Any]]]:
    """Pop every queued drift signal, returning (signals, remaining_raw).

    Remaining-raw is empty on success; if handling later fails we
    re-persist it so the queue is not lost.  Matches the
    "fail-loud, don't-drop" convention the signal queue documents.
    """
    signals = pending_drift_signals(state)
    # Reset the queue; if a caller wants to preserve on-failure we
    # re-persist below.
    state[LEDGER_DRIFT_SIGNALS_KEY] = "[]"
    return signals, []


def handle_drift(
    state: MutableMapping[str, Any],
    *,
    executor: Optional[StepExecutor] = None,
    drain: bool = True,
) -> list[DriftHandlingReceipt]:
    """Consume pending drift signals and run A6 for each.

    ``drain=True`` (default) pops every queued signal up front and
    processes them in FIFO order.  ``drain=False`` processes only the
    first queued signal -- useful for step-at-a-time inspection from
    tests or the dashboard.

    On validator failure the plan is NOT executed; the receipt records
    the error and the drift signal is re-enqueued so a human can
    inspect and rerun.
    """
    if drain:
        signals, _ = _pop_all_drift_signals(state)
    else:
        all_signals = pending_drift_signals(state)
        if not all_signals:
            return []
        signals = [all_signals[0]]
        # Rewrite queue without the first entry.
        tail = [d.to_dict() for d in all_signals[1:]]
        state[LEDGER_DRIFT_SIGNALS_KEY] = json.dumps(tail, ensure_ascii=False)

    outcomes: list[DriftHandlingReceipt] = []
    re_enqueue: list[dict[str, Any]] = []
    for drift in signals:
        plan = plan_remanifestation(state, drift)
        try:
            validate_plan(state, plan)
        except InvalidPlanError as exc:
            logger.error(
                "remanifestation plan failed validation (stage=%s, "
                "drift %d->%d): %s",
                drift.stage_name, drift.from_rev, drift.to_rev, exc,
            )
            outcomes.append(
                DriftHandlingReceipt(
                    drift=drift,
                    plan=plan,
                    step_receipts=(),
                    error=str(exc),
                )
            )
            re_enqueue.append(drift.to_dict())
            continue

        step_receipts = tuple(execute_plan(state, plan, executor=executor))
        outcomes.append(
            DriftHandlingReceipt(
                drift=drift,
                plan=plan,
                step_receipts=step_receipts,
            )
        )

    if re_enqueue:
        # Preserve any drifts whose plans failed validation.  Anything
        # that succeeded (or hit an empty plan) is considered handled.
        existing = pending_drift_signals(state)
        merged = [d.to_dict() for d in existing] + re_enqueue
        state[LEDGER_DRIFT_SIGNALS_KEY] = json.dumps(
            merged, ensure_ascii=False
        )
    return outcomes


# ---------------------------------------------------------------------------
# ADK callback surface
# ---------------------------------------------------------------------------


def after_agent_remanifestation_callback(callback_context: Any) -> None:
    """ADK ``after_agent_callback`` -- consume drift signals and run A6.

    Typically wired onto the outer pipeline SequentialAgent's
    ``after_agent_callback`` so A6 runs once per stage boundary,
    AFTER ARCH-A5's consistency check has had a chance to queue any
    drift signals.
    """
    state = callback_context.state
    handle_drift(state)
    return None


__all__ = ["DriftHandlingReceipt",
    "ImpactedArtifact",
    "InvalidPlanError",
    "PlanStep",
    "RemanifestationExecutionError",
    "RemanifestationPlan",
    "StepExecutor",
    "after_agent_remanifestation_callback",
    "analyse_impact",
    "execute_plan",
    "handle_drift",
    "plan_remanifestation",
    "validate_plan",]
