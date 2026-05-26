"""RevisionTagger — tag produced artifacts with the preference-ledger revision.

Ports the ADK ``after_agent_callback`` from
``server/callbacks/artifact_revision_tag.py`` to a Strands
:class:`HookProvider`. The callback snapshot pattern is unchanged: on
:class:`AfterInvocationEvent`, read the current ledger revision, attach
it to the artifact the agent produced, and persist the tag under
``_artifact_revision_tags`` in state.

The Strands agent's ``agent.state`` is the equivalent of ADK's
``callback_context.state`` — a mutable mapping — so the underlying
:func:`tag_artifact` helper works unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

from strands.hooks import AfterInvocationEvent, HookProvider, HookRegistry

from callbacks.artifact_revision_tag import (
    ArtifactAlreadyTaggedError,
    MissingArtifactError,
    MissingLedgerStateError,
    tag_artifact,
)

logger = logging.getLogger(__name__)


class RevisionTagger(HookProvider):
    """Tag ``state[output_key]`` with the current ledger revision post-run.

    Attributes:
        output_key: The state key whose value was produced by the agent
            (e.g. ``"scenes"``, ``"visual_concepts"``). Matches ADK's
            ``output_key`` semantics.
        stage: Stage identifier stamped into the tag. Defaults to
            ``output_key`` when not provided.
        require_artifact: When True, missing / placeholder artifacts
            raise :class:`MissingArtifactError`. When False, missing
            artifacts are logged and the hook is a no-op.
        retag_on_reproduce: When True and an existing tag is present for
            this key, the old tag is cleared before re-tagging. Matches
            the ARCH-B3 re-manifestation idiom.
    """

    def __init__(
        self,
        output_key: str,
        *,
        stage: str | None = None,
        require_artifact: bool = True,
        retag_on_reproduce: bool = False,
    ) -> None:
        if not output_key:
            raise ValueError("output_key must be a non-empty string")
        self.output_key = output_key
        self.stage = stage or output_key
        self.require_artifact = require_artifact
        self.retag_on_reproduce = retag_on_reproduce

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        """Subscribe to ``AfterInvocationEvent``."""
        registry.add_callback(AfterInvocationEvent, self._on_after)

    def _on_after(self, event: AfterInvocationEvent) -> None:
        state_obj = event.agent.state
        state = state_obj.get() if hasattr(state_obj, "get") else state_obj
        if not isinstance(state, dict):
            logger.warning(
                "output_key=<%s> | agent.state is not a dict (type=<%s>); skipping",
                self.output_key,
                type(state).__name__,
            )
            return

        artifact = state.get(self.output_key)
        if not artifact:
            msg = (
                f"output_key=<{self.output_key}> | agent produced no artifact"
            )
            if self.require_artifact:
                raise MissingArtifactError(msg)
            logger.debug("%s | skipping tag", msg)
            return

        from callbacks.artifact_revision_tag import (
            ARTIFACT_REVISION_TAGS_KEY,
            _load_raw,
            clear_tag,
        )

        existing = _load_raw(state)
        if self.output_key in existing:
            if not self.retag_on_reproduce:
                logger.debug(
                    "output_key=<%s> | tag already present; skipping",
                    self.output_key,
                )
                return
            clear_tag(state, self.output_key)

        try:
            tag = tag_artifact(state, self.output_key, stage=self.stage)
        except (MissingLedgerStateError, ArtifactAlreadyTaggedError) as exc:
            logger.warning(
                "output_key=<%s>, error=<%s> | tag_artifact raised; propagating",
                self.output_key,
                exc,
            )
            raise

        # Surface the tag on state so downstream tooling can observe it without
        # parsing the JSON blob under ARTIFACT_REVISION_TAGS_KEY again.
        logger.debug(
            "output_key=<%s>, revision=<%d>, storage_key=<%s> | tagged artifact",
            self.output_key,
            tag.ledger_revision,
            ARTIFACT_REVISION_TAGS_KEY,
        )
