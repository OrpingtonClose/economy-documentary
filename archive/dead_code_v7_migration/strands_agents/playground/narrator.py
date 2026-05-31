"""Narrator LLM loop + post-run interpreter for the Component Playground.

The event bus in :mod:`strands_agents.playground.events` emits
structured events at every step of a run — probing, dispatching,
tool-calling, evaluating. Those events are fine for a disclosure log
but they are noise for the user's primary feedback surface.

This module owns two LLM passes that sit on top of the event stream:

1. **Live narrator** — an async loop that runs while a run is in
   progress. Every ``_NARRATE_INTERVAL_SECONDS`` it looks at the
   tail of the event buffer and, if something new has happened since
   the last narration, picks the single most salient event and
   writes a terse one-sentence status line back to the bus as a
   ``narrate`` event. The UI renders whichever ``narrate`` event is
   freshest; silence between them is handled by the frontend
   staleness timer.
2. **Post-run interpreter** — once the run hits a terminal event
   (``run.ok`` / ``run.error`` / ``run.cancelled``) the interpreter
   runs once against the full event history + the run's output +
   any evaluator scores. It emits a single ``interpret`` event with
   a short paragraph describing what happened, what the output
   means, and what is notable or concerning.

Both passes fail open: if the chosen narrator model is unreachable
or the LLM call raises, we skip the narration rather than killing
the run. The raw event stream is always available; narration is
feedback polish, not a contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from typing import Any

from strands_agents.playground.events import Event, RunStream

logger = logging.getLogger(__name__)

_NARRATE_INTERVAL_SECONDS: float = 1.5
_SILENT_REPORT_SECONDS: float = 3.0
_MAX_TAIL_EVENTS: int = 12
_DEFAULT_NARRATOR_MODEL: str = "openai/gpt-4o-mini"
_NARRATOR_MODEL_ENV: str = "PLAYGROUND_NARRATOR_MODEL"
_NARRATOR_DISABLE_ENV: str = "PLAYGROUND_NARRATOR_DISABLE"
_NARRATOR_TIMEOUT_SECONDS: float = 8.0
_INTERPRETER_TIMEOUT_SECONDS: float = 20.0

LLMCompleter = Callable[..., Any]


def _is_disabled() -> bool:
    flag = os.environ.get(_NARRATOR_DISABLE_ENV, "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def _narrator_model() -> str:
    override = os.environ.get(_NARRATOR_MODEL_ENV, "").strip()
    return override or _DEFAULT_NARRATOR_MODEL


def _resolve_complete(
    complete: LLMCompleter | None,
) -> LLMCompleter | None:
    if complete is not None:
        return complete
    try:
        import litellm  # type: ignore[import-not-found]
    except ImportError:
        return None
    return litellm.completion


_RICH_DETAIL_KEYS: tuple[str, ...] = (
    "model_id",
    "provider",
    "tool",
    "step",
    "elapsed_ms",
    "latency_ms",
    "reason",
    "error",
    "error_class",
    "score",
    "rating",
    "num_scenes",
    "num_issues",
    "total_duration_sec",
    "input_keys",
    "result_shape",
    "input_digest",
    "result_digest",
)


def _tail_to_prompt(events: list[Event], *, now: float | None = None) -> str:
    """Compact the tail into an LLM-friendly bullet list.

    Each event line carries:

    * kind + summary,
    * every narrator-relevant detail key (see :data:`_RICH_DETAIL_KEYS`
      — the full tapestry, not just model_id + elapsed_ms),
    * an elapsed-age hint like ``(4.2s ago)`` when ``now`` is passed,
      so the narrator can keep talking about an in-flight step even
      when no new backend event has arrived.

    A ``CONTEXT:`` header is prepended with aggregate facts the
    per-event rendering alone would hide — total run elapsed,
    distinct kinds, and a repeat-count for the most common kind.
    Those aggregates are what lets the narrator say "still on
    generate_scenario — 6th evaluate_scenario iteration, 41s total"
    instead of "still probing gemini" for the thirtieth time.
    """
    import time as _time

    reference = now if now is not None else _time.time()
    tail = events[-_MAX_TAIL_EVENTS:]
    lines: list[str] = []
    for event in tail:
        detail = event.detail or {}
        detail_parts: list[str] = []
        for key in _RICH_DETAIL_KEYS:
            if key in detail:
                value = detail[key]
                if isinstance(value, (list, tuple)):
                    value = ",".join(str(v) for v in value)
                detail_parts.append(f"{key}={value}")
        age_s = max(0.0, reference - event.ts)
        age = f" ({age_s:.1f}s ago)"
        tail_str = (" " + " ".join(detail_parts)) if detail_parts else ""
        lines.append(f"- [{event.kind}] {event.summary}{tail_str}{age}")

    header = _context_header(events, tail, reference)
    return header + "\n".join(lines)


def _context_header(
    all_events: list[Event],
    tail: list[Event],
    reference: float,
) -> str:
    """Aggregate facts the per-event lines don't carry on their own.

    * ``total_elapsed_s`` — time since the *first* event in the run,
      so the narrator can frame "42s into the run" instead of only
      knowing per-event ages.
    * ``kinds`` — compact histogram of kinds in the tail so the
      narrator can spot a tight loop ("tool.called × 6 in 12s").
    * ``repeated_kind`` — the dominant kind and its count, surfacing
      the "same event fired N times" signal that drives the
      novel-or-admit-repetition rule in the system prompt.
    """
    if not all_events:
        return "CONTEXT: empty\n"
    first_ts = all_events[0].ts
    total_elapsed = max(0.0, reference - first_ts)
    histogram: dict[str, int] = {}
    for ev in tail:
        histogram[ev.kind] = histogram.get(ev.kind, 0) + 1
    dominant = max(histogram.items(), key=lambda kv: kv[1]) if histogram else ("-", 0)
    kinds_str = ",".join(f"{k}={v}" for k, v in sorted(histogram.items()))
    return (
        f"CONTEXT: total_elapsed={total_elapsed:.1f}s "
        f"tail_kinds={kinds_str} "
        f"dominant={dominant[0]}×{dominant[1]} "
        f"total_events={len(all_events)}\n"
    )


_NARRATE_SYSTEM = """You narrate a single in-flight software task to a developer.

Ground rules:
- Write one sentence. <= 90 characters. No preamble.
- Terse, technical, no marketing voice. The reader is not a layperson.
- Draw from the full tapestry in the event list: CONTEXT header
  (total_elapsed, tail_kinds histogram, dominant kind × count),
  every detail key on each line (model_id, tool, step, elapsed_ms,
  num_scenes, rating, num_issues, result_shape, input_digest,
  error_class, score, ...). Use whatever is most salient right
  now — not just model_id and elapsed.
- Never reassure ("we're working on it"); state the step.
- Never invent steps or facts that aren't in the event list.
- If the last event is an error, repeat its error class and short
  message verbatim.
- If the latest event is old (the age in parentheses is several
  seconds), keep talking about that same step but frame it with
  the elapsed time — the user needs to know the task is still
  on the same thing, not silent.

Try hard to say something novel and pertinent every time:
- Prefer a concrete new fact from the latest event (new tool,
  new step number, new rating, new num_scenes, new elapsed).
- If no new fact exists, be ever more specific about what IS
  happening (which tool, which step number, which model) or
  hypothesise the likely outcome using only known facts
  ("gemini 3 Pro preview cold-starts ~30s on first call —
  still within budget at 18s").
- Never repeat the previous narration verbatim. Advance it:
  higher elapsed, different framing, a detail you hadn't named.

If you absolutely cannot say something novel and pertinent,
acknowledge the repetition explicitly instead of paraphrasing.
Format: "no new signal, still on <step> — <Ns>". That honest
line is preferred over an invented "different framing" that
adds no information.

Only return the word NONE if the event list is empty.

Examples of good lines:
- probing gemini/gemini-3-pro-preview (3.8s elapsed)
- still probing gemini — 12s, preview models often cold-start this slow
- scenario_agent picked openai/gpt-4o, on tool.called step 3 (evaluate_scenario)
- evaluate_scenario returned rating=FAIR with 2 issues — expecting refine next
- refine_scenario step 5 returned 7 scenes, 318s total — converging on target
- no new signal, still on evaluate_scenario — 8s
- task raised Timeout after 41.2s
"""


_INTERPRET_SYSTEM = """You write a short post-run interpretation for a developer inspecting a single component run.

Output a single paragraph, 2-4 sentences. No bullet points, no headings.

Cover, in order:
1. What the component actually did on this input (name the model it ran against if known).
2. Whether the output looks contract-honest for this case.
3. Whether the declared evaluators are likely to pass or fail, and why. If evaluators already ran, cite the scores verbatim.
4. Anything notable, unusual, or concerning in the trajectory or timing.

Terse, technical, no marketing voice. The reader is not a layperson.
If the run errored, state the error class + message and what layer it came from.
Do not invent information. If evaluators did not run, say so.
"""


async def narrator_loop(
    stream: RunStream,
    *,
    interval_seconds: float = _NARRATE_INTERVAL_SECONDS,
    silent_report_seconds: float = _SILENT_REPORT_SECONDS,
    complete: LLMCompleter | None = None,
) -> None:
    """Pump narration events into ``stream`` until it closes.

    Emits a fresh ``narrate`` event on two triggers:

    * **New backend activity** — whenever the tail grows beyond the
      highest ``seq`` we've already narrated.
    * **Silence** — if no new events land but the last narration is
      more than ``silent_report_seconds`` old, re-narrate anyway so
      the UI isn't frozen staring at "still probing gemini" from
      eight seconds ago. The prompt carries each event's elapsed age
      so the LLM can advance the framing (e.g. "still probing — 12s").

    Safe to cancel — cancellation unwinds with a single CancelledError
    and does not emit a terminal event (the run endpoint owns the
    terminal event).
    """
    import time as _time

    if _is_disabled():
        return
    completer = _resolve_complete(complete)
    if completer is None:
        return
    model = _narrator_model()
    last_reported_seq = 0
    last_line: str | None = None
    last_emit_ts: float = 0.0
    try:
        while not stream.closed:
            await asyncio.sleep(interval_seconds)
            tail = stream.snapshot()
            if not tail:
                continue
            newest_seq = tail[-1].seq
            now = _time.time()
            has_new = newest_seq > last_reported_seq
            silent_for = now - last_emit_ts if last_emit_ts else float("inf")
            if not has_new and silent_for < silent_report_seconds:
                continue
            line = await _narrate_once(
                completer=completer,
                model=model,
                tail=tail,
                previous=last_line,
                now=now,
            )
            if line is None:
                continue
            stripped = line.strip()
            if not stripped or stripped.upper() == "NONE":
                continue
            if stripped == (last_line or "").strip():
                # LLM parroted the previous line; don't re-emit — the
                # frontend keeps the previous narration visible and
                # updating the stall counter is handled client-side.
                continue
            last_reported_seq = newest_seq
            last_line = line
            last_emit_ts = now
            await stream.emit(
                "narrate",
                line,
                detail={"model_id": model, "upto_seq": newest_seq},
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — narrator must never crash run
        logger.warning("narrator loop failed for %s: %s", stream.run_id, exc)


async def _narrate_once(
    *,
    completer: LLMCompleter,
    model: str,
    tail: list[Event],
    previous: str | None,
    now: float | None = None,
) -> str | None:
    user_prompt = _tail_to_prompt(tail, now=now)
    if previous:
        user_prompt += f"\n\nLast narration: {previous}"

    def _call() -> Any:
        return completer(
            model=model,
            messages=[
                {"role": "system", "content": _NARRATE_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=60,
            temperature=0.2,
            timeout=_NARRATOR_TIMEOUT_SECONDS,
        )

    try:
        result = await asyncio.to_thread(_call)
    except Exception as exc:  # noqa: BLE001 — silent fail is fine
        logger.debug("narrator call failed: %s", exc)
        return None
    return _extract_text(result)


async def interpret_run(
    stream: RunStream,
    *,
    output: Any,
    evaluator_scores: list[dict[str, Any]] | None = None,
    complete: LLMCompleter | None = None,
) -> str | None:
    """Run the post-hoc interpreter and emit an ``interpret`` event.

    Returns the interpretation string for the caller to include in
    the run's terminal payload as well. Returns ``None`` if the
    interpreter is disabled or the LLM call failed.
    """
    if _is_disabled():
        return None
    completer = _resolve_complete(complete)
    if completer is None:
        return None
    model = _narrator_model()
    tail = stream.snapshot()
    try:
        output_blob = json.dumps(output, default=str)[:4000]
    except Exception:  # noqa: BLE001
        output_blob = str(output)[:4000]
    evaluator_blob = json.dumps(evaluator_scores or [], default=str)[:2000]
    user_prompt = (
        f"Component: {stream.component_id}\n"
        f"Case: {stream.case_name or '(custom)'}\n\n"
        f"Events:\n{_tail_to_prompt(tail)}\n\n"
        f"Output:\n{output_blob}\n\n"
        f"Evaluator scores:\n{evaluator_blob}"
    )

    def _call() -> Any:
        return completer(
            model=model,
            messages=[
                {"role": "system", "content": _INTERPRET_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=220,
            temperature=0.2,
            timeout=_INTERPRETER_TIMEOUT_SECONDS,
        )

    try:
        result = await asyncio.to_thread(_call)
    except Exception as exc:  # noqa: BLE001
        logger.warning("interpreter call failed for %s: %s", stream.run_id, exc)
        return None
    text = _extract_text(result)
    if not text:
        return None
    await stream.emit(
        "interpret", text, detail={"model_id": model, "post_run": True}
    )
    return text


def _extract_text(result: Any) -> str | None:
    """Extract the narrator / interpreter's message text from a completion.

    Accepts both the litellm ``ModelResponse`` shape and a plain
    dict-shaped stub (used by tests).
    """
    if result is None:
        return None
    try:
        choices = result.choices if hasattr(result, "choices") else result["choices"]
        first = choices[0]
        message = (
            first.message if hasattr(first, "message") else first["message"]
        )
        content = (
            message.content if hasattr(message, "content") else message["content"]
        )
    except (AttributeError, KeyError, IndexError, TypeError):
        return None
    if not isinstance(content, str):
        return None
    text = content.strip()
    return text or None


__all__ = [
    "interpret_run",
    "narrator_loop",
]
