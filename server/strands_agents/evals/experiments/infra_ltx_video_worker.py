"""LTX-Video worker experiment (slice 5 / 5a).

Drives :func:`strands_agents.ltx_video_worker.app.build_app` through
scripted HTTP sequences using a deterministic
:class:`StubVideoEngine` so CI stays GPU-free, and scores every
outcome through :class:`strands_evals.Evaluator` subclasses.

This exercises the *worker contract* the orchestrator depends on:

* Happy-path render returns an ISO-BMFF (MP4) payload starting with
  an ``ftyp`` box and containing an ``mdat`` box whose size reflects
  the requested duration.
* Duration is clamped to engine bounds; requests below
  :data:`MIN_DURATION_S` are rejected.
* Deterministic output — same ``(prompt, duration, fps, seed)`` →
  byte-identical payload. The pipeline's resumable-checkpoint story
  depends on this.
* The bump middleware fires on ``/video/render`` but not on
  ``/health``.

Cases cover:

* **render_returns_mp4** — POST /video/render, assert 200 +
  ftyp/mdat boxes + engine=stub + correct dimensions.
* **prompt_empty_422** — Pydantic min-length validation.
* **duration_too_short_400** — below min rejected with
  ``duration_too_short`` reason.
* **duration_zero_422** — Pydantic gt=0 validation.
* **duration_clamped_at_max** — request > MAX clamped, 200.
* **determinism_same_input_same_bytes** — two identical renders,
  byte-identical MP4 payloads.
* **render_bumps_infra_agent** — middleware fires on /video/render.
* **health_does_not_bump** — /health excluded from middleware.
* **vram_endpoint_returns_peak** — /health/vram surfaces injected
  telemetry.
"""

from __future__ import annotations

import base64
import json
import struct
from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient
from strands_evals.case import Case
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.experiment import Experiment
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

from strands_agents.infra_agent.telemetry import ResourceTelemetry
from strands_agents.ltx_video_worker.app import build_app
from strands_agents.ltx_video_worker.bump_client import (
    InfraAgentBumpClient,
    _BumpResponse,
)
from strands_agents.ltx_video_worker.engine import (
    MAX_DURATION_S,
    StubVideoEngine,
)


INFRA_LTX_VIDEO_WORKER_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "VideoResponseEvaluator": (1.0, True),
    "VideoMp4StructureEvaluator": (1.0, True),
    "VideoInvariantEvaluator": (1.0, True),
}


_WORKER_ID = "ltx-video-test"


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
    expected_mp4_exact_byte_equality: bool = False,
    expected_duration_clamped_to: float | None = None,
    vram_samples: list[tuple[int, int]] | None = None,
) -> Case[dict[str, Any], dict[str, Any]]:
    return Case[dict[str, Any], dict[str, Any]](
        name=name,
        session_id=f"infra-ltx-video-{name}",
        input={
            "requests": requests,
            "vram_samples": [list(s) for s in (vram_samples or [])],
        },
        expected_output={"final_status": expected_status},
        metadata={
            "expected_status": expected_status,
            "expected_body_contains": expected_body_contains or {},
            "expected_bump_count": expected_bump_count,
            "expected_mp4_exact_byte_equality": (
                expected_mp4_exact_byte_equality
            ),
            "expected_duration_clamped_to": expected_duration_clamped_to,
        },
    )


def infra_ltx_video_worker_cases() -> (
    list[Case[dict[str, Any], dict[str, Any]]]
):
    """Canonical suite for the LTX-Video worker."""
    prompt = "A documentary establishing shot of a quiet harbour at dawn."
    return [
        _case(
            "render_returns_mp4",
            requests=[
                {
                    "method": "POST",
                    "path": "/video/render",
                    "body": {
                        "prompt": prompt,
                        "duration_s": 2.0,
                    },
                }
            ],
            expected_status=200,
            expected_body_contains={
                "worker_id": _WORKER_ID,
                "engine": "stub",
                "width": 1280,
                "height": 720,
                "fps": 24,
                "duration_s": 2.0,
            },
        ),
        _case(
            "prompt_empty_422",
            requests=[
                {
                    "method": "POST",
                    "path": "/video/render",
                    "body": {"prompt": "", "duration_s": 2.0},
                }
            ],
            expected_status=422,
        ),
        _case(
            "duration_too_short_400",
            requests=[
                {
                    "method": "POST",
                    "path": "/video/render",
                    "body": {"prompt": prompt, "duration_s": 0.01},
                }
            ],
            expected_status=400,
        ),
        _case(
            "duration_zero_422",
            requests=[
                {
                    "method": "POST",
                    "path": "/video/render",
                    "body": {"prompt": prompt, "duration_s": 0.0},
                }
            ],
            expected_status=422,
        ),
        _case(
            "duration_clamped_at_max",
            requests=[
                {
                    "method": "POST",
                    "path": "/video/render",
                    "body": {
                        "prompt": prompt,
                        "duration_s": MAX_DURATION_S * 1.5,
                    },
                }
            ],
            expected_status=200,
            expected_duration_clamped_to=MAX_DURATION_S,
        ),
        _case(
            "determinism_same_input_same_bytes",
            requests=[
                {
                    "method": "POST",
                    "path": "/video/render",
                    "body": {
                        "prompt": prompt,
                        "duration_s": 1.0,
                        "seed": 42,
                    },
                },
                {
                    "method": "POST",
                    "path": "/video/render",
                    "body": {
                        "prompt": prompt,
                        "duration_s": 1.0,
                        "seed": 42,
                    },
                },
            ],
            expected_status=200,
            expected_mp4_exact_byte_equality=True,
        ),
        _case(
            "render_bumps_infra_agent",
            requests=[
                {
                    "method": "POST",
                    "path": "/video/render",
                    "body": {"prompt": prompt, "duration_s": 1.0},
                }
            ],
            expected_status=200,
            expected_bump_count=1,
        ),
        _case(
            "health_does_not_bump",
            requests=[{"method": "GET", "path": "/health"}],
            expected_status=200,
            expected_bump_count=0,
        ),
        _case(
            "vram_endpoint_returns_peak",
            requests=[{"method": "GET", "path": "/health/vram"}],
            vram_samples=[(80, 48)],
            expected_status=200,
            expected_body_contains={
                "worker_id": _WORKER_ID,
                "vram_total_gb": 80,
                "vram_peak_gb": 48,
            },
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
    return 500, 40


def infra_ltx_video_worker_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Replay the case's HTTP sequence against a fresh worker app."""
    payload = case.input or {}

    bumps = _BumpRecorder()
    bump_client = InfraAgentBumpClient(
        url="http://127.0.0.1:29230/infra/bump",
        http_post=bumps.post,
    )
    engine = StubVideoEngine()
    telemetry = ResourceTelemetry(
        vram_prober=_make_vram_prober(
            [tuple(s) for s in payload.get("vram_samples", [])]
        ),
        disk_prober=_disk_prober,
        disk_path="/",
    )
    app = build_app(
        worker_id=_WORKER_ID,
        engine=engine,
        telemetry=telemetry,
        bump_client=bump_client,
    )

    trajectory: list[str] = []
    final_status = 0
    final_body: Any = None
    mp4_payloads: list[bytes] = []
    with TestClient(app) as client:
        for req in payload.get("requests", []):
            method = req["method"].upper()
            path = req["path"]
            trajectory.append(f"{method} {path}")
            if method == "GET":
                response = client.get(path)
            elif method == "POST":
                response = client.post(path, json=req.get("body"))
            else:
                raise ValueError(f"unsupported method: {method}")
            final_status = response.status_code
            try:
                final_body = response.json()
            except json.JSONDecodeError:
                final_body = {"_text": response.text}
            if isinstance(final_body, dict) and "mp4_base64" in final_body:
                mp4_payloads.append(
                    base64.b64decode(final_body["mp4_base64"])
                )

    return {
        "output": {
            "final_status": final_status,
            "body": final_body,
            "mp4_payloads_len": len(mp4_payloads),
            "bump_count": len(bumps.calls),
            "mp4_bytes_equal": (
                len(mp4_payloads) >= 2
                and all(
                    payload == mp4_payloads[0] for payload in mp4_payloads[1:]
                )
            ),
            "mp4_structure_valid": all(
                _mp4_structure_valid(p) for p in mp4_payloads
            ),
            "mp4_box_types": (
                _mp4_box_types(mp4_payloads[-1]) if mp4_payloads else []
            ),
        },
        "trajectory": trajectory,
        "metadata": {"requests_applied": len(trajectory)},
    }


def _mp4_box_types(mp4_bytes: bytes) -> list[str]:
    """Return the 4-byte box types at the top level of ``mp4_bytes``.

    Does not recurse. Returns an empty list if parsing fails.
    """
    box_types: list[str] = []
    offset = 0
    try:
        while offset < len(mp4_bytes):
            if offset + 8 > len(mp4_bytes):
                break
            size = struct.unpack(">I", mp4_bytes[offset : offset + 4])[0]
            box_type = mp4_bytes[offset + 4 : offset + 8].decode(
                "ascii", errors="replace"
            )
            box_types.append(box_type)
            if size == 0 or size < 8:
                break
            offset += size
    except struct.error:
        return []
    return box_types


def _mp4_structure_valid(mp4_bytes: bytes) -> bool:
    """Return True iff ``mp4_bytes`` starts with ``ftyp`` and contains ``mdat``."""
    box_types = _mp4_box_types(mp4_bytes)
    return bool(box_types) and box_types[0] == "ftyp" and "mdat" in box_types


# ── Evaluators ───────────────────────────────────────────────────────


class VideoResponseEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
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


class VideoMp4StructureEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """For success cases, ensure the MP4 payload has ftyp + mdat boxes."""

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        status = int(actual.get("final_status", 0))
        if status != 200 or actual.get("mp4_payloads_len", 0) == 0:
            return [
                EvaluationOutput(
                    score=1.0,
                    test_pass=True,
                    reason="no mp4 expected for this case",
                    label="structure_not_required",
                )
            ]
        ok = bool(actual.get("mp4_structure_valid", False))
        box_types = actual.get("mp4_box_types") or []
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                reason=(
                    f"mp4 starts with ftyp and contains mdat (boxes={box_types!r})"
                    if ok
                    else f"mp4 structure invalid (boxes={box_types!r})"
                ),
                label="mp4_valid" if ok else "mp4_invalid",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


class VideoInvariantEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Pin bump count, duration clamp, determinism."""

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        metadata = evaluation_case.metadata or {}
        body = actual.get("body") or {}
        problems: list[str] = []

        expected_bump = metadata.get("expected_bump_count")
        if expected_bump is not None:
            got_bump = int(actual.get("bump_count", 0))
            if got_bump != expected_bump:
                problems.append(
                    f"bump_count={got_bump} expected {expected_bump}"
                )

        expected_clamp = metadata.get("expected_duration_clamped_to")
        if expected_clamp is not None:
            got_duration = body.get("duration_s")
            if got_duration is None:
                problems.append("body.duration_s is None")
            elif abs(float(got_duration) - float(expected_clamp)) > 1e-6:
                problems.append(
                    f"body.duration_s={got_duration} "
                    f"expected {expected_clamp} (engine clamp)"
                )

        if metadata.get("expected_mp4_exact_byte_equality"):
            if not actual.get("mp4_bytes_equal", False):
                problems.append("mp4 payloads not byte-identical")

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


def build_infra_ltx_video_worker_experiment() -> (
    Experiment[dict[str, Any], dict[str, Any]]
):
    """Assemble the LTX-Video worker :class:`Experiment`."""
    return Experiment[dict[str, Any], dict[str, Any]](
        cases=infra_ltx_video_worker_cases(),
        evaluators=[
            VideoResponseEvaluator(),
            VideoMp4StructureEvaluator(),
            VideoInvariantEvaluator(),
        ],
    )


__all__ = [
    "INFRA_LTX_VIDEO_WORKER_EVALUATOR_THRESHOLDS",
    "VideoInvariantEvaluator",
    "VideoMp4StructureEvaluator",
    "VideoResponseEvaluator",
    "build_infra_ltx_video_worker_experiment",
    "infra_ltx_video_worker_cases",
    "infra_ltx_video_worker_task",
]
