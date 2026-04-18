"""Reusable critic-squad primitive (PR-3).

A **critic squad** is an ADK :class:`ParallelAgent` of small
:class:`LlmAgent` critics that run in parallel after a stage completes,
each emitting a structured JSON critique.  The squad's
``after_agent_callback`` parses every critic's output_key from state,
translates it via :func:`critique.adapters.critic_payload_to_critique`,
and appends a :class:`Critique` record to the artifact's
:class:`ArtifactCritiqueRecord`.

Design notes
------------

* **Lazy ADK imports** — ``google.adk`` is imported *inside*
  :func:`build_critic_squad` so this module can be imported from
  unit tests that do not have the ADK installed.  Everything related
  to parsing / writing critiques is ADK-independent and lives at
  module level.
* **No ``output_schema``** — ADK's ``output_schema`` forces
  structured output and disables tool calling / sub-agent
  transfer.  Critic squads are leaf agents, so that would be safe,
  but keeping raw-text output + JSON parsing matches the existing
  scenario/coherence evaluators, lets critics include free-form
  prose, and means the callback is the single dedupe /
  normalisation seam.
* **Artifact identity is dynamic** — most squads run once per stage
  but the artifact they critique (scene id, concept id, clip id)
  depends on state.  Callers pass resolver callables that read
  ``callback_context.state`` and return ``(artifact_type,
  artifact_id)``.
* **Failures never raise** — if a critic emits malformed JSON, the
  callback logs at WARNING and skips just that critic; the rest of
  the squad is recorded normally.  A store failure is also swallowed
  so critique mirroring cannot take down the pipeline.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from critique.adapters import critic_payload_to_critique
from critique.record import ArtifactType
from critique.store import ArtifactCritiqueStore, get_critique_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Critic specs
# ---------------------------------------------------------------------------

@dataclass
class CriticSpec:
    """Declarative description of a single critic in a squad.

    ``output_key`` defaults to ``f"critique_{name}"``; callers who
    already have an ``output_key`` convention can override.

    ``voter_model`` (falling back to ``model``) is recorded on the
    :class:`Critique` so downstream consumers can see which model
    produced which perspective.
    """

    name: str
    model: Any
    instruction: str
    description: str = ""
    output_key: str = ""
    voter_model: str = ""
    critic_source: str = ""

    def resolved_output_key(self) -> str:
        return self.output_key or f"critique_{self.name}"

    def resolved_voter_model(self) -> str:
        if self.voter_model:
            return self.voter_model
        return getattr(self.model, "model", "") or str(self.model) if self.model is not None else ""

    def resolved_source(self) -> str:
        return self.critic_source or self.name


# ---------------------------------------------------------------------------
# State resolvers
# ---------------------------------------------------------------------------

StateLike = Any  # duck-typed ADK callback_context.state

StateArtifactResolver = Callable[[StateLike], tuple[str, str]]
"""Given callback state, return ``(artifact_type, artifact_id)``.

The callback never invokes the resolver with a missing state; it just
catches ValueError/KeyError if the resolver can't produce an identity.
"""


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_payload(raw: Any) -> Optional[dict[str, Any]]:
    """Pull a JSON object out of ``raw`` (critic output text).

    Critics are instructed to emit JSON, but LLMs often wrap the
    object in prose / markdown code fences.  This helper:

    1. If ``raw`` is already a dict, return it.
    2. If ``raw`` is a string, try ``json.loads`` directly.
    3. Fall back to the first ``{...}`` block in the string.
    4. Return ``None`` if nothing parseable is found.
    """

    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    match = _JSON_OBJ_RE.search(text)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except (ValueError, json.JSONDecodeError):
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


# ---------------------------------------------------------------------------
# Callback factory
# ---------------------------------------------------------------------------

def make_critic_squad_callback(
    *,
    critics: list[CriticSpec],
    artifact_resolver: StateArtifactResolver,
    store: Optional[ArtifactCritiqueStore] = None,
    produced_by: str = "",
    iteration_resolver: Optional[Callable[[StateLike], Optional[int]]] = None,
) -> Callable[[Any], None]:
    """Return an ``after_agent_callback`` for a critic ``ParallelAgent``.

    The returned callable:

    * resolves the target ``(artifact_type, artifact_id)`` from state,
    * for each :class:`CriticSpec`, reads the critic's output_key from
      state and parses it as JSON,
    * converts to a :class:`Critique` via
      :func:`critic_payload_to_critique`, and
    * appends the critique to the artifact's record via
      :meth:`ArtifactCritiqueStore.append_critique`.

    Errors are logged at WARNING; the callback never raises.
    """

    def _callback(callback_context: Any) -> None:
        state = getattr(callback_context, "state", callback_context)
        try:
            artifact_type_raw, artifact_id = artifact_resolver(state)
        except (KeyError, ValueError, TypeError, LookupError) as exc:
            logger.warning(
                "critic squad: artifact_resolver failed, skipping critique persistence: %s",
                exc,
            )
            return
        if not artifact_type_raw or not artifact_id:
            logger.debug(
                "critic squad: empty artifact identity (%r / %r), skipping",
                artifact_type_raw,
                artifact_id,
            )
            return

        resolved_store = store
        if resolved_store is None:
            try:
                resolved_store = get_critique_store()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("critic squad: store unavailable, skipping: %s", exc)
                return

        iteration: Optional[int] = None
        if iteration_resolver is not None:
            try:
                iteration = iteration_resolver(state)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("critic squad: iteration_resolver failed: %s", exc)

        for spec in critics:
            raw = _safe_state_get(state, spec.resolved_output_key())
            payload = _extract_json_payload(raw)
            if payload is None:
                logger.warning(
                    "critic squad: critic %r produced no parseable JSON "
                    "(output_key=%r); skipping",
                    spec.name,
                    spec.resolved_output_key(),
                )
                continue
            try:
                critique = critic_payload_to_critique(
                    payload,
                    source=spec.resolved_source(),
                    voter_model=spec.resolved_voter_model(),
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "critic squad: adapter failed for critic %r: %s",
                    spec.name,
                    exc,
                )
                continue
            try:
                resolved_store.append_critique(
                    _normalise_artifact_type(artifact_type_raw),
                    artifact_id,
                    critique,
                    produced_by=produced_by or spec.resolved_source(),
                    iteration=iteration,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "critic squad: failed to persist critique for %s/%s critic=%s: %s",
                    artifact_type_raw,
                    artifact_id,
                    spec.name,
                    exc,
                )

    return _callback


def _normalise_artifact_type(value: Any) -> ArtifactType:
    """Coerce a resolver's ``artifact_type`` string to :data:`ArtifactType`.

    The store re-validates via :func:`critique.store.ArtifactCritiqueStore._path`,
    so callers who pass an unknown string still fail loudly -- this
    helper is purely for typing.
    """

    return str(value)  # type: ignore[return-value]


def _safe_state_get(state: Any, key: str) -> Any:
    """Read ``key`` from ``state`` across ADK's state variants.

    ADK's callback_context.state exposes both a dict-like mapping and
    attribute access.  We try mapping first, then getattr, then
    ``None`` on anything weird.
    """

    try:
        return state[key]
    except (KeyError, TypeError):
        pass
    try:
        return getattr(state, key)
    except AttributeError:
        return None


# ---------------------------------------------------------------------------
# ParallelAgent factory (lazy ADK import)
# ---------------------------------------------------------------------------

@dataclass
class CriticSquad:
    """Container returned by :func:`build_critic_squad`.

    Holds the ADK ``ParallelAgent`` plus the critic specs + callback so
    callers can introspect / re-attach in tests without re-importing
    ``google.adk``.
    """

    parallel_agent: Any
    critics: list[CriticSpec] = field(default_factory=list)
    callback: Optional[Callable[[Any], None]] = None
    name: str = ""


def build_critic_squad(
    *,
    name: str,
    description: str,
    critics: list[CriticSpec],
    artifact_resolver: StateArtifactResolver,
    store: Optional[ArtifactCritiqueStore] = None,
    produced_by: str = "",
    iteration_resolver: Optional[Callable[[StateLike], Optional[int]]] = None,
    before_agent_callback: Optional[Callable[[Any], Any]] = None,
) -> CriticSquad:
    """Build a critic ``ParallelAgent`` wired to the critique store.

    The ADK imports happen *inside* this function so the module can be
    imported from tests / environments without ``google.adk``.  The
    :class:`CriticSquad` wrapper carries both the ADK agent and the
    callback/spec tuple so tests can exercise the callback directly.
    """

    if not critics:
        raise ValueError("critic squad must have at least one critic")

    callback = make_critic_squad_callback(
        critics=critics,
        artifact_resolver=artifact_resolver,
        store=store,
        produced_by=produced_by,
        iteration_resolver=iteration_resolver,
    )

    # Lazy ADK import; keeps module importable without ADK.
    from google.adk.agents import Agent  # type: ignore[import-not-found]
    from google.adk.agents.parallel_agent import (  # type: ignore[import-not-found]
        ParallelAgent,
    )

    sub_agents = [
        Agent(
            name=spec.name,
            model=spec.model,
            description=spec.description or f"Critic: {spec.name}",
            instruction=spec.instruction,
            output_key=spec.resolved_output_key(),
        )
        for spec in critics
    ]

    parallel = ParallelAgent(
        name=name,
        description=description,
        sub_agents=sub_agents,
        before_agent_callback=before_agent_callback,
        after_agent_callback=callback,
    )

    return CriticSquad(
        parallel_agent=parallel,
        critics=list(critics),
        callback=callback,
        name=name,
    )


__all__ = [
    "CriticSpec",
    "CriticSquad",
    "build_critic_squad",
    "make_critic_squad_callback",
]
