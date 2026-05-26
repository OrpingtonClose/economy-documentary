"""AudioWorkerInvariantEvaluator — trajectory-level audio-worker check.

Enforces AGENTS.md hard invariant #1 on the orchestrator's tool-call
trajectory:

    One TTS voice per VM. The TTS worker is stateful. A single VM
    generates audio for exactly one character voice. Launching two
    ``launch_audio_render`` tasks against the same worker pool with
    different character voices is a race.

Two failure modes are detectable from a trajectory alone:

1. **Cross-voice parallel batch.** When the orchestrator fans out
   ``launch_audio_render`` in a single turn (``at_turn`` shared), every
   call in that turn must share a single ``voice_id``. Emitting voice
   ``V1`` and voice ``V2`` in one parallel batch *is* the race —
   regardless of whether a ``worker_pool`` argument is present, two
   launches on one turn are not guaranteed to land on distinct VMs.

2. **Worker-pool rebind.** When calls carry an explicit
   ``worker_pool`` argument, any pool that is ever asked to render
   more than one distinct ``voice_id`` (across the whole trajectory)
   violates the invariant. The pool is stateful — binding it to a
   different voice later in the run is still a rebind.

Clean runs that need multiple voices serialise them across batches
(separate turns) or route them to distinct ``worker_pool`` values.

Input shape
-----------
``EvaluationData`` with:

* ``actual_trajectory``: ``list[dict]`` of tool-call records. Each
  record must carry a ``"name"`` key; this evaluator inspects
  ``launch_audio_render`` entries only. Each entry's ``"args"`` is
  read for ``voice_id`` (or ``voices[0].voice_id``) and optional
  ``worker_pool``. Entries without a ``voice_id`` are ignored —
  the evaluator cannot grade what the orchestrator did not bind.
* ``metadata[`expect_cross_voice_race`]`` (optional, default
  ``False``): set to ``True`` when a test deliberately scripts the
  violation and wants the gate to fail.
* ``metadata[`expect_pool_rebind`]`` (optional, default ``False``):
  same, for the pool-rebind gate.

Output
------
Up to three :class:`EvaluationOutput` entries (all hard gates):

* ``audio_worker.voice_id_present`` — every ``launch_audio_render``
  call carried an extractable ``voice_id``.
* ``audio_worker.no_cross_voice_in_batch`` — no parallel batch mixed
  voice_ids.
* ``audio_worker.no_pool_rebind`` — no ``worker_pool`` was ever
  asked to render more than one distinct voice. Emitted only when at
  least one call declared a ``worker_pool``.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from strands_evals.evaluators.evaluator import Evaluator  # type: ignore[import-not-found]
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput  # type: ignore[import-not-found]

_AUDIO_TOOL = "launch_audio_render"


def _extract_voice_id(args: dict[str, Any]) -> str | None:
    """Pull a single voice_id out of the call args.

    Accepts either the flat ``voice_id`` form used by the placeholder
    tool (``launch_audio_render(scene_id, voice_id)``) or the nested
    ``voice_map``/``voices`` forms the real orchestrator may emit.
    Returns ``None`` when no single voice can be identified — the
    caller treats that as ungradable (counted by the
    ``voice_id_present`` gate, not the race gates).
    """
    direct = args.get("voice_id")
    if isinstance(direct, str) and direct:
        return direct

    voice_map = args.get("voice_map")
    if isinstance(voice_map, dict) and voice_map:
        unique = {v for v in voice_map.values() if isinstance(v, str) and v}
        if len(unique) == 1:
            return next(iter(unique))
        # Multiple distinct voice_ids inside one call's voice_map is
        # itself a scene-level race — surface it to the batch gate by
        # returning the sentinel "*mixed*" token. The caller bucketises
        # it like any other voice_id; a batch containing "*mixed*"
        # alongside anything else (or even alone) trips the cross-voice
        # gate because no real VM can render two voices for one scene.
        if len(unique) > 1:
            return "*mixed*"

    voices = args.get("voices")
    if isinstance(voices, list):
        collected = {
            v.get("voice_id")
            for v in voices
            if isinstance(v, dict) and isinstance(v.get("voice_id"), str) and v.get("voice_id")
        }
        if len(collected) == 1:
            return next(iter(collected))
        if len(collected) > 1:
            return "*mixed*"

    return None


class AudioWorkerInvariantEvaluator(Evaluator[Any, Any]):
    """Check audio-worker scheduling invariants on a tool-call trajectory."""

    def __init__(self) -> None:
        super().__init__()

    def evaluate(
        self,
        evaluation_case: EvaluationData[Any, Any],
    ) -> list[EvaluationOutput]:
        metadata = evaluation_case.metadata or {}
        expect_cross_voice = bool(metadata.get("expect_cross_voice_race", False))
        expect_pool_rebind = bool(metadata.get("expect_pool_rebind", False))

        trajectory = evaluation_case.actual_trajectory
        if not isinstance(trajectory, list):
            return [
                EvaluationOutput(
                    score=0.0,
                    test_pass=False,
                    reason="actual_trajectory must be list[dict] of tool calls",
                    label="audio_worker.missing_actual",
                )
            ]

        audio_calls: list[dict[str, Any]] = [
            call
            for call in trajectory
            if isinstance(call, dict) and call.get("name") == _AUDIO_TOOL
        ]

        outputs: list[EvaluationOutput] = []

        per_call_voice: list[tuple[dict[str, Any], str | None]] = [
            (call, _extract_voice_id(call.get("args") or {})) for call in audio_calls
        ]

        missing_voice = [call for call, voice in per_call_voice if voice is None]
        voice_ok = audio_calls and not missing_voice
        if not audio_calls:
            voice_reason = "FAIL launch_audio_render was never dispatched"
        elif missing_voice:
            voice_reason = (
                f"FAIL {len(missing_voice)}/{len(audio_calls)} launch_audio_render "
                f"calls had no extractable voice_id"
            )
        else:
            voice_reason = (
                f"PASS every launch_audio_render call ({len(audio_calls)}) carried a voice_id"
            )
        outputs.append(
            EvaluationOutput(
                score=1.0 if voice_ok else 0.0,
                test_pass=bool(voice_ok),
                reason=voice_reason,
                label="audio_worker.voice_id_present",
            )
        )

        batches_by_turn: dict[Any, list[str]] = defaultdict(list)
        for call, voice in per_call_voice:
            if voice is None:
                continue
            at = call.get("at_turn")
            if at is None:
                # No turn marker — skip from batch analysis. The
                # voice_id_present gate already passed; the pool gate
                # below still applies when worker_pool is present.
                continue
            batches_by_turn[at].append(voice)

        offending_turns = {
            turn: sorted(set(voices))
            for turn, voices in batches_by_turn.items()
            if len(set(voices)) > 1 or "*mixed*" in voices
        }
        batch_race_detected = bool(offending_turns)
        batch_ok = batch_race_detected == expect_cross_voice
        if expect_cross_voice and batch_race_detected:
            offenders = ", ".join(
                f"turn {turn}={voices}"
                for turn, voices in sorted(
                    offending_turns.items(), key=lambda kv: str(kv[0])
                )
            )
            batch_reason = f"PASS cross-voice race detected as expected — {offenders}"
        elif expect_cross_voice:
            batch_reason = "FAIL cross-voice race expected but every batch was single-voice"
        elif batch_race_detected:
            offenders = ", ".join(
                f"turn {turn}={voices}"
                for turn, voices in sorted(
                    offending_turns.items(), key=lambda kv: str(kv[0])
                )
            )
            batch_reason = f"FAIL cross-voice race in batch — {offenders}"
        else:
            batch_reason = (
                f"PASS no cross-voice race across {len(batches_by_turn)} batch(es)"
            )
        outputs.append(
            EvaluationOutput(
                score=1.0 if batch_ok else 0.0,
                test_pass=batch_ok,
                reason=batch_reason,
                label="audio_worker.no_cross_voice_in_batch",
            )
        )

        pool_voices: dict[str, set[str]] = defaultdict(set)
        any_pool_declared = False
        for call, voice in per_call_voice:
            args = call.get("args") or {}
            pool = args.get("worker_pool")
            if not isinstance(pool, str) or not pool:
                continue
            any_pool_declared = True
            if voice is not None and voice != "*mixed*":
                pool_voices[pool].add(voice)

        if any_pool_declared or expect_pool_rebind:
            if not any_pool_declared:
                pool_ok = False
                pool_reason = (
                    "FAIL pool rebind expected but no worker_pool was ever declared"
                )
            else:
                rebinds = {
                    pool: sorted(voices)
                    for pool, voices in pool_voices.items()
                    if len(voices) > 1
                }
                rebind_detected = bool(rebinds)
                pool_ok = rebind_detected == expect_pool_rebind
                if expect_pool_rebind and rebind_detected:
                    offenders = ", ".join(
                        f"{pool}={voices}" for pool, voices in sorted(rebinds.items())
                    )
                    pool_reason = (
                        f"PASS worker-pool rebind detected as expected — {offenders}"
                    )
                elif expect_pool_rebind:
                    pool_reason = (
                        "FAIL pool rebind expected but every pool was single-voice"
                    )
                elif rebind_detected:
                    offenders = ", ".join(
                        f"{pool}={voices}" for pool, voices in sorted(rebinds.items())
                    )
                    pool_reason = f"FAIL worker-pool rebind — {offenders}"
                else:
                    pool_reason = (
                        f"PASS every worker_pool bound to a single voice "
                        f"across {len(pool_voices)} pool(s)"
                    )
            outputs.append(
                EvaluationOutput(
                    score=1.0 if pool_ok else 0.0,
                    test_pass=pool_ok,
                    reason=pool_reason,
                    label="audio_worker.no_pool_rebind",
                )
            )

        return outputs


__all__ = ["AudioWorkerInvariantEvaluator"]
