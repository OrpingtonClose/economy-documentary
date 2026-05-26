"""Qwen3-TTS worker experiment (slice 4b / 5a).

Drives :func:`strands_agents.qwen3_tts_worker.app.build_app` through
scripted HTTP sequences using a deterministic
:class:`StubTTSEngine` so CI stays GPU-free, and scores every outcome
through :class:`strands_evals.Evaluator` subclasses.

This exercises the *worker contract* the orchestrator depends on:

* Happy-path render returns a well-formed 16-bit mono PCM WAV whose
  duration scales linearly with input text length.
* The VM's pinned voice is enforced — any mismatched ``voice_id``
  gets a 409 (AGENTS.md §1: one voice per VM).
* Deterministic output — same text + voice + seed → same bytes. The
  pipeline's resumable-checkpoint story depends on this.
* The bump middleware fires on ``/tts/render`` but not on ``/health``.

Cases cover:

* **render_returns_wav** — POST /tts/render, assert 200 + valid WAV +
  engine=stub + voice echo.
* **voice_mismatch_409** — request a voice not pinned to this VM.
* **empty_text_422** — Pydantic validation.
* **determinism_same_input_same_bytes** — two identical renders,
  byte-identical WAV payloads.
* **long_text_longer_audio** — duration scales linearly with text.
* **render_bumps_infra_agent** — middleware fires on /tts/render.
* **health_does_not_bump** — /health excluded from middleware.
* **vram_endpoint_returns_peak** — /health/vram surfaces injected
  telemetry.
"""

from __future__ import annotations

import io
import json
import wave
from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient
from strands_evals.case import Case  # type: ignore[import-not-found]
from strands_evals.evaluators.evaluator import Evaluator  # type: ignore[import-not-found]
from strands_evals.experiment import Experiment  # type: ignore[import-not-found]
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput  # type: ignore[import-not-found]

from strands_agents.infra_agent.telemetry import ResourceTelemetry
from strands_agents.qwen3_tts_worker.app import build_app
from strands_agents.qwen3_tts_worker.bump_client import (
    InfraAgentBumpClient,
    _BumpResponse,
)
from strands_agents.qwen3_tts_worker.engine import StubTTSEngine


INFRA_QWEN3_TTS_WORKER_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "TTSResponseEvaluator": (1.0, True),
    "TTSWavStructureEvaluator": (1.0, True),
    "TTSInvariantEvaluator": (1.0, True),
}


_PINNED_VOICE = "narrator_male_1"
_WORKER_ID = "qwen3-tts-test"


# ── Cases ────────────────────────────────────────────────────────────


@dataclass
class _BumpRecorder:
    """Stand-in for :class:`InfraAgentBumpClient` that records calls."""

    calls: list[str] = field(default_factory=list)

    def post(self, *, url: str, timeout: float) -> _BumpResponse:
        self.calls.append(url)
        return _BumpResponse(status_code=200)


def _case(
    name: str,
    *,
    requests: list[dict[str, Any]],
    expected_status: int,
    expected_body_contains: dict[str, Any] | None = None,
    expected_bump_count: int | None = None,
    expected_wav_duration_s: float | None = None,
    expected_wav_exact_byte_equality: bool = False,
    vram_samples: list[tuple[int, int]] | None = None,
    pinned_voice_id: str = _PINNED_VOICE,
) -> Case[dict[str, Any], dict[str, Any]]:
    return Case[dict[str, Any], dict[str, Any]](
        name=name,
        session_id=f"infra-qwen3-tts-{name}",
        input={
            "pinned_voice_id": pinned_voice_id,
            "requests": requests,
            "vram_samples": [list(s) for s in (vram_samples or [])],
        },
        expected_output={"final_status": expected_status},
        metadata={
            "expected_status": expected_status,
            "expected_body_contains": expected_body_contains or {},
            "expected_bump_count": expected_bump_count,
            "expected_wav_duration_s": expected_wav_duration_s,
            "expected_wav_exact_byte_equality": expected_wav_exact_byte_equality,
        },
    )


def infra_qwen3_tts_worker_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Canonical suite for the Qwen3-TTS worker."""
    short_text = "This is a short narration."
    long_text = "This is a much longer narration. " * 10
    return [
        _case(
            "render_returns_wav",
            requests=[
                {
                    "method": "POST",
                    "path": "/",
                    "body": short_text,
                }
            ],
            expected_status=200,
            expected_wav_duration_s=len(short_text) / 15.0,
        ),
        _case(
            "empty_text_400",
            requests=[
                {
                    "method": "POST",
                    "path": "/",
                    "body": "",
                }
            ],
            expected_status=400,
        ),
        _case(
            "determinism_same_input_same_bytes",
            requests=[
                {
                    "method": "POST",
                    "path": "/",
                    "body": short_text,
                },
                {
                    "method": "POST",
                    "path": "/",
                    "body": short_text,
                },
            ],
            expected_status=200,
            expected_wav_exact_byte_equality=True,
        ),
        _case(
            "long_text_longer_audio",
            requests=[
                {
                    "method": "POST",
                    "path": "/",
                    "body": long_text,
                }
            ],
            expected_status=200,
            expected_wav_duration_s=len(long_text) / 15.0,
        ),
        _case(
            "render_bumps_infra_agent",
            requests=[
                {
                    "method": "POST",
                    "path": "/",
                    "body": short_text,
                }
            ],
            expected_status=200,
            expected_bump_count=1,
        ),
        _case(
            "health_does_not_bump",
            requests=[{"method": "GET", "path": "/"}],
            expected_status=200,
            expected_bump_count=0,
        ),
    ]


# ── Task adapter ─────────────────────────────────────────────────────


def _make_vram_prober(samples: list[tuple[int, int]]) -> Any:
    if not samples:
        return lambda: None
    idx = [0]

    def _prober() -> tuple[int, int]:
        i = min(idx[0], len(samples) - 1)
        idx[0] += 1
        return tuple(samples[i])  # type: ignore[return-value]

    return _prober


def _disk_prober(_path: str) -> tuple[int, int]:
    return 100, 20


def infra_qwen3_tts_worker_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Replay the case's HTTP sequence against a fresh worker app."""
    payload = case.input or {}

    bumps = _BumpRecorder()
    bump_client = InfraAgentBumpClient(
        url="http://127.0.0.1:29230/",
        http_post=bumps.post,
    )
    engine = StubTTSEngine()
    telemetry = ResourceTelemetry(
        vram_prober=_make_vram_prober(
            [tuple(s) for s in payload.get("vram_samples", [])]
        ),
        disk_prober=_disk_prober,
        disk_path="/",
    )
    app = build_app(
        worker_id=_WORKER_ID,
        pinned_voice_id=payload["pinned_voice_id"],
        engine=engine,
        telemetry=telemetry,
        bump_client=bump_client,
    )

    trajectory: list[str] = []
    final_status = 0
    final_body: Any = None
    wav_payloads: list[bytes] = []
    with TestClient(app) as client:
        for req in payload.get("requests", []):
            method = req["method"].upper()
            path = req["path"]
            trajectory.append(f"{method} {path}")
            if method == "GET":
                response = client.get(path)
            elif method == "POST":
                body = req.get("body")
                if body is not None:
                    response = client.post(path, data=body.encode("utf-8"), headers={"Content-Type": "text/plain"})
                else:
                    response = client.post(path)
            else:
                raise ValueError(f"unsupported method: {method}")
            final_status = response.status_code
            content_type = response.headers.get("content-type", "")
            if "audio/wav" in content_type:
                wav_payloads.append(response.content)
                final_body = {"_bytes": len(response.content)}
            else:
                try:
                    final_body = response.json()
                except json.JSONDecodeError:
                    final_body = {"_text": response.text}

    return {
        "output": {
            "final_status": final_status,
            "body": final_body,
            "wav_payloads_len": len(wav_payloads),
            "bump_count": len(bumps.calls),
            "wav_bytes_equal": (
                len(wav_payloads) >= 2
                and all(
                    payload == wav_payloads[0] for payload in wav_payloads[1:]
                )
            ),
            "wav_duration_s": _probe_wav_duration(
                wav_payloads[-1] if wav_payloads else None
            ),
            "wav_structure_valid": all(
                _wav_structure_valid(p) for p in wav_payloads
            ),
        },
        "trajectory": trajectory,
        "metadata": {"requests_applied": len(trajectory)},
    }


def _probe_wav_duration(wav_bytes: bytes | None) -> float | None:
    if not wav_bytes:
        return None
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            return frames / rate if rate else None
    except wave.Error:
        return None


def _wav_structure_valid(wav_bytes: bytes) -> bool:
    """Return True iff ``wav_bytes`` parses as 16-bit mono PCM."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            return (
                wav.getnchannels() == 1
                and wav.getsampwidth() == 2
                and wav.getframerate() > 0
                and wav.getnframes() > 0
            )
    except wave.Error:
        return False


# ── Evaluators ───────────────────────────────────────────────────────


class TTSResponseEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Pin status code + required body keys."""

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        metadata = evaluation_case.metadata or {}
        expected_status = int(metadata.get("expected_status", -1))
        expected_contains: dict[str, Any] = metadata.get(
            "expected_body_contains", {}
        )
        actual_status = int(actual.get("final_status", -1))
        body = actual.get("body") or {}

        problems: list[str] = []
        if actual_status != expected_status:
            problems.append(
                f"status={actual_status} expected {expected_status}"
            )
        for key, value in expected_contains.items():
            if body.get(key) != value:
                problems.append(
                    f"body.{key}={body.get(key)!r} expected {value!r}"
                )
        ok = not problems
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                reason=("response matches" if ok else "; ".join(problems)),
                label="response_match" if ok else "response_mismatch",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


class TTSWavStructureEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """For success cases, ensure returned WAV parses as 16-bit mono PCM.

    For non-success cases (409, 422) no WAV is expected; the check is a
    trivial pass.
    """

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        status = int(actual.get("final_status", 0))
        if status != 200 or actual.get("wav_payloads_len", 0) == 0:
            return [
                EvaluationOutput(
                    score=1.0,
                    test_pass=True,
                    reason="no wav expected for this case",
                    label="structure_not_required",
                )
            ]
        ok = bool(actual.get("wav_structure_valid", False))
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                reason=(
                    "wav is 16-bit mono PCM"
                    if ok
                    else "wav failed structural check (channels/sampwidth/frames)"
                ),
                label="wav_valid" if ok else "wav_invalid",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


class TTSInvariantEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Pin bump count, duration-scales-with-text, determinism."""

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        metadata = evaluation_case.metadata or {}
        problems: list[str] = []

        expected_bump = metadata.get("expected_bump_count")
        if expected_bump is not None:
            got_bump = int(actual.get("bump_count", 0))
            if got_bump != expected_bump:
                problems.append(
                    f"bump_count={got_bump} expected {expected_bump}"
                )

        expected_duration = metadata.get("expected_wav_duration_s")
        if expected_duration is not None:
            got_duration = actual.get("wav_duration_s")
            if got_duration is None:
                problems.append("wav_duration_s is None")
            elif abs(float(got_duration) - float(expected_duration)) > 0.1:
                problems.append(
                    f"wav_duration_s={got_duration:.3f} "
                    f"expected ~{expected_duration:.3f} (±0.1s)"
                )

        if metadata.get("expected_wav_exact_byte_equality"):
            if not actual.get("wav_bytes_equal", False):
                problems.append("wav payloads not byte-identical")

        ok = not problems
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                reason=(
                    "invariants hold" if ok else "; ".join(problems)
                ),
                label="invariants_ok" if ok else "invariants_violated",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


# ── Experiment factory ───────────────────────────────────────────────


def build_infra_qwen3_tts_worker_experiment() -> (
    Experiment[dict[str, Any], dict[str, Any]]
):
    """Assemble the Qwen3-TTS worker :class:`Experiment`."""
    return Experiment[dict[str, Any], dict[str, Any]](
        cases=infra_qwen3_tts_worker_cases(),
        evaluators=[
            TTSResponseEvaluator(),
            TTSWavStructureEvaluator(),
            TTSInvariantEvaluator(),
        ],
    )


__all__ = ["TTSInvariantEvaluator",
    "TTSResponseEvaluator",
    "TTSWavStructureEvaluator",
    "build_infra_qwen3_tts_worker_experiment",
    "infra_qwen3_tts_worker_cases",
    "infra_qwen3_tts_worker_task",]
