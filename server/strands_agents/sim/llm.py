"""Scripted LLM fake.

:class:`FakeLLM` stands in for every LLM-backed helper the pipeline
injects:

* scenario generator (component 01) — ``(topic, num_scenes, style, language)``
* scenario refiner (component 03) — ``(scenes, feedback)``
* voice-text rewriter (component 03) — ``(text, direction, delta_sec)``
* phrase extractor (component 06) — ``(scene, whisperx_segment, max_phrases)``
* concept proposer (component 07) — ``(phrase, style_lock, visual_style)``
* coherence scorer (component 08) — ``(visual_concepts, style_lock, content_analysis)``

Scripting model
---------------

Each test builds a :class:`LLMScript` that declares what every
``generate_*`` / ``refine_*`` / ``score_*`` call on the fake should
return. Matching is in declaration order: the first rule whose
``match`` function returns ``True`` is used. A rule may fire once
(default) or be marked ``reusable=True`` to match every call that
satisfies it.

If no rule matches a call, :class:`NoScriptedResponse` is raised.
Silent fallbacks are forbidden — the whole point of the fake is to
force the test author to describe the scenario exhaustively.

Scripts can also be built with just canned payloads when the matcher
doesn't matter (e.g. "return one scenario, then refine it once"). For
that common case use :meth:`LLMScript.always` and :meth:`LLMScript.next_of`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from strands_agents.sim.recorder import CallRecord, Recorder


class NoScriptedResponse(RuntimeError):
    """Raised when :class:`FakeLLM` is called for an unscripted scenario."""


@dataclass
class _Rule:
    op: str
    match: Callable[[dict[str, Any]], bool]
    response: Any
    reusable: bool = False
    used: int = 0


@dataclass
class LLMScript:
    """Ordered list of scripted LLM responses.

    Use the ``.when_*`` helpers to build up rules for each helper the
    pipeline will call. Rules fire in declaration order; the first
    match wins. Each rule fires once unless ``reusable=True``.
    """

    rules: list[_Rule] = field(default_factory=list)

    # ------------------------------------------------------------------
    # One rule per LLM helper. Each helper gets a dedicated ``when_*``
    # method so test authors don't have to remember op names.
    # ------------------------------------------------------------------

    def when_generate_scenario(
        self,
        *,
        response: dict[str, Any],
        match: Callable[[dict[str, Any]], bool] = lambda _: True,
        reusable: bool = False,
    ) -> LLMScript:
        """Register a rule for the scenario-generator call."""
        self.rules.append(
            _Rule(
                op="generate_scenario",
                match=match,
                response=response,
                reusable=reusable,
            )
        )
        return self

    def when_refine_scenario(
        self,
        *,
        response: dict[str, Any],
        match: Callable[[dict[str, Any]], bool] = lambda _: True,
        reusable: bool = False,
    ) -> LLMScript:
        self.rules.append(
            _Rule(
                op="refine_scenario",
                match=match,
                response=response,
                reusable=reusable,
            )
        )
        return self

    def when_rewrite_voice_text(
        self,
        *,
        response: str,
        match: Callable[[dict[str, Any]], bool] = lambda _: True,
        reusable: bool = False,
    ) -> LLMScript:
        self.rules.append(
            _Rule(
                op="rewrite_voice_text",
                match=match,
                response=response,
                reusable=reusable,
            )
        )
        return self

    def when_extract_phrases(
        self,
        *,
        response: list[dict[str, Any]],
        match: Callable[[dict[str, Any]], bool] = lambda _: True,
        reusable: bool = False,
    ) -> LLMScript:
        self.rules.append(
            _Rule(
                op="extract_phrases",
                match=match,
                response=response,
                reusable=reusable,
            )
        )
        return self

    def when_propose_concept(
        self,
        *,
        response: dict[str, Any],
        match: Callable[[dict[str, Any]], bool] = lambda _: True,
        reusable: bool = False,
    ) -> LLMScript:
        self.rules.append(
            _Rule(
                op="propose_concept",
                match=match,
                response=response,
                reusable=reusable,
            )
        )
        return self

    def when_score_coherence(
        self,
        *,
        response: dict[str, Any],
        match: Callable[[dict[str, Any]], bool] = lambda _: True,
        reusable: bool = False,
    ) -> LLMScript:
        self.rules.append(
            _Rule(
                op="score_coherence",
                match=match,
                response=response,
                reusable=reusable,
            )
        )
        return self


class FakeLLM:
    """Scripted responder for every LLM-backed helper in the pipeline.

    Each public method matches a helper's signature exactly so it can
    be passed straight into the corresponding ``set_*_helpers(...)``.
    """

    def __init__(
        self,
        script: LLMScript | None = None,
        *,
        recorder: Recorder | None = None,
    ) -> None:
        """Create a fake LLM.

        Args:
            script: Optional script. A fake with no script raises
                :class:`NoScriptedResponse` on the first call —
                that's the default so "I forgot to wire this" fails
                loudly.
            recorder: Optional :class:`Recorder` for trajectory capture.
        """
        self._script = script or LLMScript()
        self._recorder = recorder
        # Parallelism in the pipeline (e.g. ``content_analyst`` extracting
        # phrases for multiple scenes concurrently) means ``_dispatch``
        # can be entered from many threads at once. Without a lock, two
        # threads could both see ``rule.used == 0`` and double-fire a
        # one-shot rule. Every other fake in this package uses the same
        # pattern — keep it uniform.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public helper surfaces — each matches a ``set_*_helpers`` slot.
    # ------------------------------------------------------------------

    def generate_scenario(
        self, topic: str, num_scenes: int, style: str, language: str
    ) -> dict[str, Any]:
        return self._dispatch(
            op="generate_scenario",
            payload={
                "topic": topic,
                "num_scenes": num_scenes,
                "style": style,
                "language": language,
            },
        )

    def refine_scenario(
        self, scenes: list[dict[str, Any]], feedback: dict[str, Any]
    ) -> dict[str, Any]:
        return self._dispatch(
            op="refine_scenario",
            payload={"scenes": scenes, "feedback": feedback},
        )

    def rewrite_voice_text(self, text: str, direction: str, delta_sec: float) -> str:
        return self._dispatch(
            op="rewrite_voice_text",
            payload={"text": text, "direction": direction, "delta_sec": delta_sec},
        )

    def extract_phrases(
        self,
        scene: dict[str, Any],
        whisperx_segment: dict[str, Any],
        max_phrases: int,
    ) -> list[dict[str, Any]]:
        return self._dispatch(
            op="extract_phrases",
            payload={
                "scene": scene,
                "whisperx_segment": whisperx_segment,
                "max_phrases": max_phrases,
            },
        )

    def propose_concept(
        self,
        phrase: dict[str, Any],
        style_lock: dict[str, Any],
        visual_style: dict[str, Any],
    ) -> dict[str, Any]:
        return self._dispatch(
            op="propose_concept",
            payload={
                "phrase": phrase,
                "style_lock": style_lock,
                "visual_style": visual_style,
            },
        )

    def score_coherence(
        self,
        visual_concepts: list[dict[str, Any]],
        style_lock: dict[str, Any],
        content_analysis: dict[str, Any],
    ) -> dict[str, Any]:
        return self._dispatch(
            op="score_coherence",
            payload={
                "visual_concepts": visual_concepts,
                "style_lock": style_lock,
                "content_analysis": content_analysis,
            },
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _dispatch(self, *, op: str, payload: dict[str, Any]) -> Any:
        with self._lock:
            for rule in self._script.rules:
                if rule.op != op:
                    continue
                if not rule.reusable and rule.used > 0:
                    continue
                try:
                    if not rule.match(payload):
                        continue
                except Exception as exc:  # noqa: BLE001 — script author bug
                    msg = f"match() for op={op} rule raised {exc!r}"
                    raise NoScriptedResponse(msg) from exc
                rule.used += 1
                response = rule.response
                if self._recorder is not None:
                    self._recorder.record(
                        CallRecord(
                            channel="llm",
                            op=op,
                            kwargs=payload,
                            result_summary=_summarise(response),
                        )
                    )
                return response
        msg = (
            f"no scripted response for FakeLLM op={op!r}; "
            f"payload keys={sorted(payload)}; "
            f"add a rule via LLMScript.when_{op}(...)"
        )
        raise NoScriptedResponse(msg)


def _summarise(response: Any) -> str:
    if isinstance(response, dict):
        return f"dict keys={sorted(response)[:5]}"
    if isinstance(response, list):
        return f"list len={len(response)}"
    if isinstance(response, str):
        truncated = response if len(response) <= 40 else response[:37] + "..."
        return f"str={truncated!r}"
    return type(response).__name__
