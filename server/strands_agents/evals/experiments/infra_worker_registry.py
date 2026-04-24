"""Worker-registry experiment (slice 3 / 5a).

Drives :class:`strands_agents.playground.worker_registry.WorkerRegistry`
through its invariant-enforcing surface and scores every outcome
through deterministic :class:`strands_evals.Evaluator` subclasses.

The registry is pure Python with no external service, so this
experiment is offline — no VM, no Redis, no live worker. Each Case
runs a short scripted sequence of operations and asserts whether the
*last* operation's outcome matches expectation. ``success`` means "no
exception raised"; ``raises/<ExceptionName>`` means the registry
correctly rejected an invalid operation.

Cases cover:

* **register_new_tts_with_voice** — happy path, pins a voice in one call.
* **register_duplicate_worker_rejected** — same ``worker_id`` twice
  raises :class:`DuplicateWorkerError`.
* **voice_already_pinned_rejected** — the same ``voice_id`` pinned on
  a second VM raises :class:`VoiceAlreadyPinnedError` (AGENTS.md §1
  invariant: one voice per VM).
* **pin_second_voice_on_same_worker_rejected** — a worker already
  carrying a voice raises :class:`WorkerAlreadyHasVoiceError` when
  asked to carry a different one.
* **voice_on_non_tts_worker_rejected** — pinning a voice to an
  ``ltx_render`` worker raises :class:`VoiceOnNonTtsWorkerError`.
* **unregister_releases_voice** — after ``unregister_worker`` the
  voice is free for a different VM to pin.
* **unregister_unknown_rejected** — no-op on an unknown id raises
  :class:`WorkerNotFoundError`, not silent.
* **preflight_no_workers_rejected** — empty registry raises
  :class:`NoWorkersRegisteredError`.
* **preflight_vram_insufficient_lists_all** — the error lists every
  shortfall, not just the first.
* **preflight_stale_worker_excluded** — a worker whose last heartbeat
  is older than ``HEARTBEAT_STALE_SECONDS`` is excluded.
* **heartbeat_refreshes_worker** — after a bump the previously-stale
  worker becomes eligible again.
"""

from __future__ import annotations

from typing import Any

from strands_evals.case import Case
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.experiment import Experiment
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from strands_agents.playground.worker_registry import (
    HEARTBEAT_STALE_SECONDS,
    DuplicateWorkerError,
    NoWorkersRegisteredError,
    VoiceAlreadyPinnedError,
    VoiceOnNonTtsWorkerError,
    VramInsufficientError,
    Worker,
    WorkerAlreadyHasVoiceError,
    WorkerNotFoundError,
    WorkerRegistry,
    preflight_vram,
)


INFRA_WORKER_REGISTRY_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "RegistryOutcomeEvaluator": (1.0, True),
    "RegistryDetailEvaluator": (1.0, True),
}


# ── Scripted-scenario schema ─────────────────────────────────────────
#
# Each case's ``input`` is a ``{"operations": [...]}`` list. Every
# operation is ``{"op": <name>, ...args}`` and is applied in order.
# The *last* operation's outcome is scored.


def _case(
    name: str,
    *,
    operations: list[dict[str, Any]],
    expected_outcome: str,
    expected_detail: dict[str, Any] | None = None,
) -> Case[dict[str, Any], dict[str, Any]]:
    """Build one Case keyed off the final operation's outcome.

    ``expected_outcome``:
        * ``"success"`` — the final op ran cleanly.
        * ``"raises/<ExceptionName>"`` — the final op raised that exact
          exception class.

    ``expected_detail``: optional structural check against the final
    op's payload — e.g. ``shortfall_worker_ids`` for VRAM preflight
    errors.
    """
    return Case[dict[str, Any], dict[str, Any]](
        name=name,
        session_id=f"infra-worker-registry-{name}",
        input={"operations": operations},
        expected_output={"outcome": expected_outcome},
        metadata={
            "expected_outcome": expected_outcome,
            "expected_detail": expected_detail or {},
        },
    )


def _register(
    worker_id: str,
    *,
    role: str = "tts",
    endpoint_url: str = "http://10.0.0.1:8080",
    vram_gb: int = 24,
    voice_id: str | None = None,
) -> dict[str, Any]:
    op = {
        "op": "register_worker",
        "worker_id": worker_id,
        "role": role,
        "endpoint_url": endpoint_url,
        "vram_gb": vram_gb,
    }
    if voice_id is not None:
        op["voice_id"] = voice_id
    return op


def infra_worker_registry_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Return the canonical invariant-enforcement suite."""
    return [
        _case(
            "register_new_tts_with_voice",
            operations=[
                _register("tts-a", voice_id="narrator_male_1"),
            ],
            expected_outcome="success",
        ),
        _case(
            "register_duplicate_worker_rejected",
            operations=[
                _register("tts-a", voice_id="narrator_male_1"),
                _register("tts-a", voice_id="narrator_female_1"),
            ],
            expected_outcome="raises/DuplicateWorkerError",
        ),
        _case(
            "voice_already_pinned_rejected",
            operations=[
                _register("tts-a", voice_id="narrator_male_1"),
                _register("tts-b", voice_id="narrator_male_1"),
            ],
            expected_outcome="raises/VoiceAlreadyPinnedError",
            expected_detail={"voice_id": "narrator_male_1"},
        ),
        _case(
            "pin_second_voice_on_same_worker_rejected",
            operations=[
                _register("tts-a", voice_id="narrator_male_1"),
                {
                    "op": "pin_voice",
                    "worker_id": "tts-a",
                    "voice_id": "narrator_female_1",
                },
            ],
            expected_outcome="raises/WorkerAlreadyHasVoiceError",
        ),
        _case(
            "voice_on_non_tts_worker_rejected",
            operations=[
                _register("ltx-a", role="ltx_render", vram_gb=80),
                {
                    "op": "pin_voice",
                    "worker_id": "ltx-a",
                    "voice_id": "narrator_male_1",
                },
            ],
            expected_outcome="raises/VoiceOnNonTtsWorkerError",
        ),
        _case(
            "unregister_releases_voice",
            operations=[
                _register("tts-a", voice_id="narrator_male_1"),
                {"op": "unregister_worker", "worker_id": "tts-a"},
                _register("tts-b", voice_id="narrator_male_1"),
            ],
            expected_outcome="success",
        ),
        _case(
            "unregister_unknown_rejected",
            operations=[
                {"op": "unregister_worker", "worker_id": "ghost"},
            ],
            expected_outcome="raises/WorkerNotFoundError",
        ),
        _case(
            "preflight_no_workers_rejected",
            operations=[
                {
                    "op": "preflight_vram",
                    "role": "ltx_render",
                    "required_gb": 48,
                    "model": "ltx-video-2.3",
                },
            ],
            expected_outcome="raises/NoWorkersRegisteredError",
        ),
        _case(
            "preflight_vram_insufficient_lists_all",
            operations=[
                _register("ltx-a", role="ltx_render", vram_gb=40),
                _register("ltx-b", role="ltx_render", vram_gb=24),
                _register("ltx-c", role="ltx_render", vram_gb=80),
                {
                    "op": "preflight_vram",
                    "role": "ltx_render",
                    "required_gb": 48,
                    "model": "ltx-video-2.3",
                },
            ],
            expected_outcome="raises/VramInsufficientError",
            expected_detail={"shortfall_worker_ids": ["ltx-a", "ltx-b"]},
        ),
        _case(
            "preflight_stale_worker_excluded",
            operations=[
                _register("ltx-a", role="ltx_render", vram_gb=80),
                {
                    "op": "advance_time",
                    "seconds": HEARTBEAT_STALE_SECONDS + 5,
                },
                {
                    "op": "preflight_vram",
                    "role": "ltx_render",
                    "required_gb": 48,
                    "model": "ltx-video-2.3",
                },
            ],
            expected_outcome="raises/NoWorkersRegisteredError",
        ),
        _case(
            "heartbeat_refreshes_worker",
            operations=[
                _register("ltx-a", role="ltx_render", vram_gb=80),
                {
                    "op": "advance_time",
                    "seconds": HEARTBEAT_STALE_SECONDS + 5,
                },
                {"op": "heartbeat", "worker_id": "ltx-a"},
                {
                    "op": "preflight_vram",
                    "role": "ltx_render",
                    "required_gb": 48,
                    "model": "ltx-video-2.3",
                },
            ],
            expected_outcome="success",
        ),
    ]


# ── Task adapter ─────────────────────────────────────────────────────


def _apply_operation(
    registry: WorkerRegistry,
    op: dict[str, Any],
    clock: list[float],
) -> tuple[str, dict[str, Any]]:
    """Run one operation against ``registry``.

    ``clock`` is a single-element list whose value is returned by the
    registry's injected ``now`` function. Operations may advance it.

    Returns ``(outcome, detail)`` where ``outcome`` is ``"success"`` or
    ``"raises/<ExceptionName>"`` and ``detail`` is a structured payload
    for the detail evaluator.
    """
    kind = op["op"]
    try:
        if kind == "register_worker":
            args = {k: v for k, v in op.items() if k != "op"}
            worker = registry.register_worker(**args)
            return "success", {"worker_id": worker.worker_id}
        if kind == "pin_voice":
            registry.pin_voice(op["worker_id"], op["voice_id"])
            return "success", {}
        if kind == "unregister_worker":
            registry.unregister_worker(op["worker_id"])
            return "success", {}
        if kind == "heartbeat":
            registry.heartbeat(op["worker_id"])
            return "success", {}
        if kind == "preflight_vram":
            workers: tuple[Worker, ...] = preflight_vram(
                registry,
                role=op["role"],
                required_gb=int(op["required_gb"]),
                model=op["model"],
            )
            return "success", {
                "eligible_worker_ids": [w.worker_id for w in workers],
            }
        if kind == "advance_time":
            clock[0] += float(op["seconds"])
            return "success", {"now": clock[0]}
        raise ValueError(f"unknown op: {kind!r}")
    except DuplicateWorkerError as err:
        return "raises/DuplicateWorkerError", {"worker_id": err.args[0]}
    except VoiceAlreadyPinnedError as err:
        return "raises/VoiceAlreadyPinnedError", {
            "voice_id": err.voice_id,
            "owner": err.other_worker_id,
        }
    except WorkerAlreadyHasVoiceError as err:
        return "raises/WorkerAlreadyHasVoiceError", {
            "worker_id": err.worker_id,
            "existing_voice_id": err.existing_voice_id,
            "new_voice_id": err.new_voice_id,
        }
    except VoiceOnNonTtsWorkerError as err:
        return "raises/VoiceOnNonTtsWorkerError", {
            "worker_id": err.worker_id,
            "role": err.role,
        }
    except WorkerNotFoundError as err:
        return "raises/WorkerNotFoundError", {"worker_id": err.args[0]}
    except NoWorkersRegisteredError as err:
        return "raises/NoWorkersRegisteredError", {"role": err.role}
    except VramInsufficientError as err:
        return "raises/VramInsufficientError", {
            "shortfall_worker_ids": [s.worker_id for s in err.shortfalls],
            "required_gb": err.required_gb,
            "model": err.model,
        }


def infra_worker_registry_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Replay the case's operation sequence against a fresh registry.

    Returns the evaluate-friendly envelope with the *final* operation's
    ``outcome`` and ``detail`` under ``output``. Earlier operations'
    outcomes appear in ``metadata.history`` for the trajectory-style
    view in the playground workbench.
    """
    payload = case.input or {}
    operations: list[dict[str, Any]] = payload.get("operations", [])

    clock = [1_000_000.0]
    registry = WorkerRegistry(now=lambda: clock[0])

    history: list[dict[str, Any]] = []
    final_outcome = "success"
    final_detail: dict[str, Any] = {}
    trajectory: list[str] = []
    for op in operations:
        outcome, detail = _apply_operation(registry, op, clock)
        trajectory.append(op["op"])
        history.append({"op": op["op"], "outcome": outcome})
        final_outcome, final_detail = outcome, detail

    return {
        "output": {
            "outcome": final_outcome,
            "detail": final_detail,
        },
        "trajectory": trajectory,
        "metadata": {
            "history": history,
            "operations_applied": len(operations),
        },
    }


# ── Evaluators ───────────────────────────────────────────────────────


class RegistryOutcomeEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Pin the final operation's ``outcome`` to the expected string.

    Success here means "the registry correctly accepted or correctly
    rejected the operation" — the sign of the outcome matters, not the
    exception message.
    """

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = (evaluation_case.actual_output or {}).get("outcome")
        expected = (evaluation_case.metadata or {}).get("expected_outcome")
        match = actual == expected
        return [
            EvaluationOutput(
                score=1.0 if match else 0.0,
                test_pass=match,
                reason=(
                    f"outcome={actual!r} "
                    f"{'matches' if match else 'does not match'} "
                    f"expected={expected!r}"
                ),
                label="outcome_match" if match else "outcome_mismatch",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


class RegistryDetailEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Structural check on the error/success detail payload.

    For cases with ``expected_detail.shortfall_worker_ids``, pins the
    list as an unordered set. For cases with ``expected_detail.voice_id``,
    pins the voice id. Cases without ``expected_detail`` pass the
    evaluator trivially (score 1.0, pass True, reason notes "no detail
    requested").
    """

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        metadata = evaluation_case.metadata or {}
        expected = metadata.get("expected_detail") or {}
        actual = (evaluation_case.actual_output or {}).get("detail") or {}

        if not expected:
            return [
                EvaluationOutput(
                    score=1.0,
                    test_pass=True,
                    reason="no detail requested for this case",
                    label="detail_not_required",
                )
            ]

        mismatches: list[str] = []
        for key, expected_value in expected.items():
            actual_value = actual.get(key)
            if key.endswith("_ids") and isinstance(expected_value, list):
                if sorted(actual_value or []) != sorted(expected_value):
                    mismatches.append(
                        f"{key}={actual_value!r} expected (unordered) "
                        f"{expected_value!r}"
                    )
            elif actual_value != expected_value:
                mismatches.append(
                    f"{key}={actual_value!r} expected {expected_value!r}"
                )
        ok = not mismatches
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                reason=(
                    "detail matches" if ok else "; ".join(mismatches)
                ),
                label="detail_match" if ok else "detail_mismatch",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


# ── Experiment factory ───────────────────────────────────────────────


def build_infra_worker_registry_experiment() -> (
    Experiment[dict[str, Any], dict[str, Any]]
):
    """Assemble the worker-registry :class:`Experiment`."""
    return Experiment[dict[str, Any], dict[str, Any]](
        cases=infra_worker_registry_cases(),
        evaluators=[
            RegistryOutcomeEvaluator(),
            RegistryDetailEvaluator(),
        ],
    )


__all__ = [
    "INFRA_WORKER_REGISTRY_EVALUATOR_THRESHOLDS",
    "RegistryDetailEvaluator",
    "RegistryOutcomeEvaluator",
    "build_infra_worker_registry_experiment",
    "infra_worker_registry_cases",
    "infra_worker_registry_task",
]
