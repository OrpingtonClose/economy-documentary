"""ARCH-G3 — Preview critic agent (thin ADK wrapper).

Wraps :func:`server.previews.consumers.evaluate_preview` as an ADK
``Agent`` whose sole tool is the deterministic evaluator.  The agent
subclass exists so meta #122's "subclass ADK ``Agent`` where
applicable" DoD is met; the actual evaluation remains deterministic
and LLM-free — the agent merely decides **when** to call the tool
and how to summarise its findings to the content ladder.

Instantiation is deferred to :func:`build_preview_critic_agent` so
that tests can exercise the underlying callable without booting a
live model provider.  The ADK ``Agent`` class is imported lazily
inside the factory for the same reason.

Spec references:

- Issue #155 (ARCH-G3 Consumers, agent lane)
- Parent #129, meta #122
"""

from __future__ import annotations

import logging

from previews.consumers import evaluate_preview

logger = logging.getLogger(__name__)


_AGENT_NAME = "preview_critic"
_AGENT_INSTRUCTION = """\
You are the Preview Critic. A deterministic preview assembly has just
been produced for the current pipeline state, covering the current
OTIO with honest placeholders for missing / failed / in-progress
slots.

Your job:

1. Call ``evaluate_preview`` exactly once to fetch the structured
   findings dict.  Do NOT call it more than once per invocation — the
   evaluator is deterministic and caches nothing.
2. Inspect the returned dict.  If ``findings`` is empty, report
   "nothing to escalate" and stop.
3. If ``findings`` is non-empty, the evaluator has already submitted
   a content-ladder escalation for them.  Your job is to summarise
   the escalation in one short paragraph: which scenes are affected,
   what the dominant failure modes are (missing vs in-progress vs
   failed), and which placeholder cards the reviewer will see on the
   preview.

You must NOT mutate the OTIO timeline, advance the pipeline phase,
or clear any artifact tag.  The preview is a QA artifact, not a
deliverable — escalation is the only sanctioned action.
"""


def build_preview_critic_agent():
    """Construct the ADK ``Agent`` for the preview critic.

    Deferred so importers that only want the underlying
    :func:`evaluate_preview` callable (tests, human-lane handlers) do
    not pay the cost of booting a model provider.
    """
    from google.adk.agents import Agent  # type: ignore

    try:
        from agents.model_config import build_model
    except ImportError:
        build_model = None  # type: ignore[assignment]

    return Agent(
        name=_AGENT_NAME,
        model=build_model() if build_model else None,
        instruction=_AGENT_INSTRUCTION,
        tools=[evaluate_preview],
        output_key="_preview_critic_findings",
    )


__all__ = [
    "build_preview_critic_agent",
    "evaluate_preview",
]
