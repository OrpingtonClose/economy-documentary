"""
Artifact revision tagging — universal back-edge to the Preference Ledger
(ARCH-B1, issue #137; parent ARCH-B #124; meta ARCH-2026 #122).

Every artifact produced by the pipeline — an OTIO slot, a scene, a clip, a
narration block — must carry a ``ledger_revision_at_derivation`` tag at the
moment it is produced.  The tag is the universal back-edge: the consistency
checker (ARCH-A5, #135) walks every tag and compares it against the current
ledger revision; drift triggers impact analysis and surgical re-manifestation
(ARCH-A6 / ARCH-B3).

This module is the producer-side substrate.  It provides:

* :class:`ArtifactRevisionTag` — the frozen record shape.
* :func:`tag_artifact` — snapshot the ledger revision NOW and attach it.
* :func:`make_revision_tagging_callback` — a factory for a universal
  ``after_agent_callback`` that automatically tags the artifact an agent
  emitted via its ``output_key``.

The consumer side (ARCH-A5 / ARCH-B2 consistency checker) lives in a
sibling module and reads tags produced here.

Design invariants (tested in ``tests/test_artifact_revision_tag.py``):

1. **Immutable tags.**  Once an artifact is tagged, the tag cannot be
   overwritten.  A second :func:`tag_artifact` call on the same
   ``artifact_key`` raises ``ArtifactAlreadyTaggedError``.  Re-manifestation
   paths (ARCH-B3) must call :func:`clear_tag` first, which is itself the
   only sanctioned way to remove a tag.
2. **Fail loud on missing ledger.**  Tagging requires the Preference
   Ledger to be initialised on the blackboard (the R0 seed runs at
   pipeline start — ARCH-A3).  If :data:`~.preference_ledger.PREFERENCE_LEDGER_KEY`
   is absent from ``state``, :func:`tag_artifact` raises
   ``MissingLedgerStateError``.  An empty-but-present ledger (revision 0)
   is valid and tags cleanly.
3. **Fail loud on missing artifact.**  The producer-side callback
   :func:`make_revision_tagging_callback` asserts the agent actually
   wrote something to ``state[output_key]``.  A silently missing artifact
   would later appear untagged to the consistency checker and is a hard
   error, not a soft degradation.
4. **Blackboard-only storage.**  Tags live under
   :data:`ARTIFACT_REVISION_TAGS_KEY` as a JSON-encoded dict, matching the
   JSON-string convention used by :mod:`server.callbacks.state_manager`
   and :mod:`server.callbacks.preference_ledger`.
5. **Callback idiom.**  The producer-side helper returns an
   ``after_agent_callback`` compatible with the pipeline's agent
   framework — same pattern as :mod:`server.callbacks.timeline_guardian`.

What this module deliberately does NOT do:

* It does not invoke the consistency checker.  That is ARCH-B2 (#138).
* It does not rewire every existing producer agent.  ARCH-B1 delivers the
  mechanism + a couple of integration examples; the remaining wiring is
  follow-up work tracked under ARCH-B.
* It does not implement re-manifestation.  That is ARCH-A6 / ARCH-B3.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, MutableMapping, Optional

from callbacks.preference_ledger import PREFERENCE_LEDGER_KEY, current_revision

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State key
# ---------------------------------------------------------------------------

#: Blackboard key under which the artifact-revision tag map is stored.
#: The value is a JSON-encoded dict: ``{artifact_key: tag_dict}``.
ARTIFACT_REVISION_TAGS_KEY = "_artifact_revision_tags"


# ---------------------------------------------------------------------------
# Fail-loud exceptions
# ---------------------------------------------------------------------------


class MissingLedgerStateError(RuntimeError):
    """Raised when :func:`tag_artifact` runs with no Preference Ledger seeded.

    The ledger's R0 seed (ARCH-A3) must run before any artifact can be
    tagged — otherwise there is nothing to derive against.  Surfacing this
    as a hard failure keeps the universal back-edge invariant honest.
    """


class ArtifactAlreadyTaggedError(RuntimeError):
    """Raised when :func:`tag_artifact` is called on an already-tagged artifact.

    Tags are immutable.  Re-manifestation paths must clear the previous tag
    via :func:`clear_tag` before re-tagging.
    """


class MissingArtifactError(RuntimeError):
    """Raised when the producer-side callback fires but its artifact is absent.

    If an agent's ``output_key`` maps to an empty or missing state entry,
    the callback cannot tag anything — and a silently-untagged artifact
    would later appear as drift to the consistency checker.  Fail loud.
    """


# ---------------------------------------------------------------------------
# Tag record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactRevisionTag:
    """Snapshot of the ledger revision an artifact was derived against.

    Fields:
        ledger_revision: The Preference Ledger revision at the moment the
            artifact was produced.  ``0`` means the ledger was seeded but
            held no records — valid and common for the initial R0 run.
        derived_at: ISO-8601 UTC timestamp of the tagging call, kept as a
            string for lossless JSON round-trip (matches
            :class:`~callbacks.preference_ledger.Origin.timestamp`).
        stage: Human-readable producer identifier — typically the ADK
            agent name.  Informational; the consistency checker joins on
            the artifact key, not the stage.
    """

    ledger_revision: int
    derived_at: str
    stage: str

    def __post_init__(self) -> None:
        if not isinstance(self.ledger_revision, int) or isinstance(
            self.ledger_revision, bool
        ):
            raise TypeError(
                f"ledger_revision must be int, got "
                f"{type(self.ledger_revision).__name__}"
            )
        if self.ledger_revision < 0:
            raise ValueError(
                f"ledger_revision must be >= 0, got {self.ledger_revision}"
            )
        if not isinstance(self.derived_at, str) or not self.derived_at:
            raise ValueError(
                f"derived_at must be a non-empty string, got {self.derived_at!r}"
            )
        if not isinstance(self.stage, str) or not self.stage:
            raise ValueError(
                f"stage must be a non-empty string, got {self.stage!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain JSON-friendly dict."""
        return {
            "ledger_revision": self.ledger_revision,
            "derived_at": self.derived_at,
            "stage": self.stage,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArtifactRevisionTag":
        """Deserialise from a dict produced by :meth:`to_dict`.

        Raises ``ValueError`` / ``TypeError`` on malformed input — no
        silent coercion.
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                f"ArtifactRevisionTag.from_dict expects a mapping, "
                f"got {type(data).__name__}"
            )
        missing = {"ledger_revision", "derived_at", "stage"} - set(data)
        if missing:
            raise ValueError(
                f"ArtifactRevisionTag is missing required fields: "
                f"{sorted(missing)}"
            )
        return cls(
            ledger_revision=data["ledger_revision"],
            derived_at=data["derived_at"],
            stage=data["stage"],
        )


# ---------------------------------------------------------------------------
# Storage helpers — JSON-string convention.
# ---------------------------------------------------------------------------


def _load_raw(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the raw tag map currently stored in ``state``.

    Accepts both JSON-string storage (blackboard convention) and already
    decoded ``dict`` storage (convenient for unit tests).  An absent key
    is treated as an empty map.
    """
    raw = state.get(ARTIFACT_REVISION_TAGS_KEY)
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{ARTIFACT_REVISION_TAGS_KEY!r} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(decoded, dict):
            raise ValueError(
                f"{ARTIFACT_REVISION_TAGS_KEY!r} must decode to a dict, "
                f"got {type(decoded).__name__}"
            )
        return decoded
    raise TypeError(
        f"{ARTIFACT_REVISION_TAGS_KEY!r} must be a JSON string or dict, "
        f"got {type(raw).__name__}"
    )


def _store_raw(
    state: MutableMapping[str, Any], tags: Mapping[str, Mapping[str, Any]]
) -> None:
    """Serialise ``tags`` back into ``state`` as a JSON string."""
    state[ARTIFACT_REVISION_TAGS_KEY] = json.dumps(
        dict(tags), ensure_ascii=False
    )


# ---------------------------------------------------------------------------
# Public API — tag lifecycle
# ---------------------------------------------------------------------------


def has_tag(state: Mapping[str, Any], artifact_key: str) -> bool:
    """Return ``True`` if ``artifact_key`` is already tagged in ``state``."""
    if not isinstance(artifact_key, str) or not artifact_key:
        raise ValueError(
            f"artifact_key must be a non-empty string, got {artifact_key!r}"
        )
    return artifact_key in _load_raw(state)


def list_tags(state: Mapping[str, Any]) -> dict[str, ArtifactRevisionTag]:
    """Return all tags currently stored, keyed by artifact key.

    Raises ``ValueError`` / ``TypeError`` if the stored ledger is malformed.
    """
    return {
        key: ArtifactRevisionTag.from_dict(entry)
        for key, entry in _load_raw(state).items()
    }


def tag_artifact(
    state: MutableMapping[str, Any],
    artifact_key: str,
    *,
    stage: str,
    now: Optional[datetime] = None,
) -> ArtifactRevisionTag:
    """Snapshot :func:`current_revision` from the ledger and tag ``artifact_key``.

    This is the canonical helper the task spec asks for.  It fails loud on
    the two immutability / correctness violations the universal back-edge
    invariant depends on.

    Args:
        state: ADK session state (or any mutable mapping).  Must contain
            :data:`~callbacks.preference_ledger.PREFERENCE_LEDGER_KEY`
            (even as an empty ledger).  Mutated in place.
        artifact_key: Non-empty key identifying the artifact.  Typically
            the agent's ``output_key`` (e.g. ``"scenes"``,
            ``"visual_concepts"``).
        stage: Human-readable producer identifier — typically the ADK
            agent name.  Informational only.
        now: Optional UTC datetime override.  Tests inject a deterministic
            value; production leaves this ``None`` and uses
            ``datetime.now(timezone.utc)``.

    Returns:
        The newly attached :class:`ArtifactRevisionTag`.

    Raises:
        MissingLedgerStateError: If the Preference Ledger key is absent
            from ``state``.  Tagging without a seeded ledger would strand
            the consistency checker — fail loud.
        ArtifactAlreadyTaggedError: If ``artifact_key`` already has a tag.
            Tags are immutable; use :func:`clear_tag` first for
            re-manifestation.
        ValueError / TypeError: On invalid ``artifact_key`` or ``stage``.
    """
    if not isinstance(artifact_key, str) or not artifact_key:
        raise ValueError(
            f"artifact_key must be a non-empty string, got {artifact_key!r}"
        )
    if not isinstance(stage, str) or not stage:
        raise ValueError(f"stage must be a non-empty string, got {stage!r}")

    if PREFERENCE_LEDGER_KEY not in state:
        raise MissingLedgerStateError(
            f"cannot tag artifact {artifact_key!r}: Preference Ledger is not "
            f"initialised on the blackboard (state[{PREFERENCE_LEDGER_KEY!r}] "
            f"is missing). The R0 seed (ARCH-A3) must run first."
        )

    tags = _load_raw(state)
    if artifact_key in tags:
        existing = tags[artifact_key]
        raise ArtifactAlreadyTaggedError(
            f"artifact {artifact_key!r} is already tagged (revision="
            f"{existing.get('ledger_revision')!r}, stage="
            f"{existing.get('stage')!r}). Tags are immutable; call "
            f"clear_tag() first if re-manifesting."
        )

    revision = current_revision(state)
    ts = (now or datetime.now(timezone.utc)).isoformat()
    tag = ArtifactRevisionTag(
        ledger_revision=revision,
        derived_at=ts,
        stage=stage,
    )
    tags[artifact_key] = tag.to_dict()
    _store_raw(state, tags)
    logger.info(
        "Tagged artifact %r at ledger revision %d (stage=%s, derived_at=%s)",
        artifact_key, revision, stage, ts,
    )
    return tag


def clear_tag(
    state: MutableMapping[str, Any], artifact_key: str
) -> None:
    """Remove the tag for ``artifact_key``.  Intended for re-manifestation.

    The universal back-edge invariant treats re-derivation as a tag
    lifecycle event: the old tag is explicitly cleared, and the re-running
    producer calls :func:`tag_artifact` again with the current revision.
    This keeps :func:`tag_artifact` itself strictly immutable.

    Args:
        state: ADK session state.  Mutated in place.
        artifact_key: Non-empty key to clear.

    Raises:
        KeyError: If no tag exists for ``artifact_key``.  Silent no-ops
            would hide re-manifestation logic bugs — fail loud.
    """
    if not isinstance(artifact_key, str) or not artifact_key:
        raise ValueError(
            f"artifact_key must be a non-empty string, got {artifact_key!r}"
        )
    tags = _load_raw(state)
    if artifact_key not in tags:
        raise KeyError(
            f"cannot clear tag for {artifact_key!r}: no tag is currently set"
        )
    del tags[artifact_key]
    _store_raw(state, tags)
    logger.info("Cleared tag for artifact %r", artifact_key)


# ---------------------------------------------------------------------------
# Universal producer-side callback factory
# ---------------------------------------------------------------------------


def make_revision_tagging_callback(
    output_key: str,
    *,
    stage: Optional[str] = None,
    require_artifact: bool = True,
    retag_on_reproduce: bool = False,
) -> Callable[[Any], None]:
    """Return an ``after_agent_callback`` that tags ``state[output_key]``.

    This is the universal producer-side callback ARCH-B1 calls for.  Every
    artifact-producing agent can be tagged by appending the returned
    callback to its ``after_agent_callback`` chain — no bespoke logic per
    agent.

    Typical wiring::

        from callbacks.artifact_revision_tag import make_revision_tagging_callback

        my_agent = Agent(
            name="my_agent",
            output_key="my_artifact",
            ...,
            after_agent_callback=make_revision_tagging_callback("my_artifact"),
        )

    When the agent already has an ``after_agent_callback`` (as several of
    ours do — e.g. scenario_director's ``_clean_scenes_after_scenario``),
    compose the two::

        def _chained(ctx):
            existing_callback(ctx)
            tagging_callback(ctx)

        my_agent = Agent(..., after_agent_callback=_chained)

    Args:
        output_key: The blackboard key the agent writes to.  The callback
            reads ``state[output_key]`` to verify the artifact was
            actually produced, and uses ``output_key`` as the tag's
            ``artifact_key`` so the consistency checker can join.
        stage: Optional override for the tag's ``stage`` field.  Defaults
            to the running agent's ``ctx.agent_name`` — the natural
            producer identifier.
        require_artifact: When ``True`` (default), fail loud if
            ``state[output_key]`` is missing or an empty string / list /
            dict — a silently missing artifact would later look like
            drift to the consistency checker.  Set to ``False`` for
            optional artifacts (e.g. skipped-phase agents that return
            without writing).
        retag_on_reproduce: When ``True``, the callback clears any
            existing tag for ``output_key`` before tagging.  This is the
            mode LoopAgent-composed producers use: each iteration
            produces a fresh artifact, so the tag must refresh to the
            current revision.  Defaults to ``False`` — the strict
            immutability default matches the DoD wording exactly.

    Returns:
        A callable suitable for ``after_agent_callback``.  Always returns
        ``None`` (never short-circuits the agent's reply chain).
    """
    if not isinstance(output_key, str) or not output_key:
        raise ValueError(
            f"output_key must be a non-empty string, got {output_key!r}"
        )
    if stage is not None and (not isinstance(stage, str) or not stage):
        raise ValueError(
            f"stage must be None or a non-empty string, got {stage!r}"
        )

    def _callback(callback_context: Any) -> None:
        state = callback_context.state

        if require_artifact:
            _assert_artifact_present(state, output_key)

        if retag_on_reproduce and has_tag(state, output_key):
            clear_tag(state, output_key)

        effective_stage = stage or getattr(
            callback_context, "agent_name", None
        ) or output_key
        tag_artifact(state, output_key, stage=effective_stage)
        return None

    _callback.__name__ = f"tag_revision_after_{output_key}"
    _callback.__qualname__ = _callback.__name__
    _callback.__doc__ = (
        f"after_agent_callback: snapshot ledger revision and tag "
        f"state[{output_key!r}] (ARCH-B1)."
    )
    return _callback


def _assert_artifact_present(state: Mapping[str, Any], output_key: str) -> None:
    """Raise :class:`MissingArtifactError` if ``state[output_key]`` is empty.

    An "empty" artifact is ``None``, missing, the empty string, an empty
    list, or an empty dict.  Anything else (including JSON-encoded
    strings of non-empty collections) is considered present — we do not
    parse content-type-specific semantics here.
    """
    if output_key not in state:
        raise MissingArtifactError(
            f"agent claimed to produce {output_key!r} but state[{output_key!r}] "
            f"is missing — cannot tag (ARCH-B1 invariant)"
        )
    value = state[output_key]
    if value is None or value == "" or value == [] or value == {}:
        raise MissingArtifactError(
            f"agent claimed to produce {output_key!r} but state[{output_key!r}]"
            f"={value!r} is empty — cannot tag (ARCH-B1 invariant)"
        )
