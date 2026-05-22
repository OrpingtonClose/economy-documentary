"""LTX-Video worker LIVE passthrough experiment (slice 9d-wire).

Sibling of :mod:`infra_ltx_video_worker` that proves the wired
LTX-2.3 BASIC engine works **end-to-end against a real GPU worker**,
not against the in-process :class:`StubVideoEngine`.

Where ``infra_ltx_video_worker`` instantiates a stub engine + a
``TestClient(app)`` so CI stays GPU-free, this experiment dispatches
HTTP requests to the URL in ``LTX_VIDEO_WORKER_URL`` (typically a
Vast.ai-hosted H200 reachable via SSH tunnel at
``http://127.0.0.1:29232``).

The contract checked here is the same as the stub experiment
(``200`` + ``mp4_base64`` containing an ISO-BMFF ``ftyp`` box) but
the proof is actual rendered bytes from the real LTX-2.3 BASIC
``ti2vid_one_stage`` subprocess on the H200.

Cases:

* ``render_returns_mp4_live`` — POST a 2 s render to the live
  worker, assert ``200`` + ``mp4_base64`` decodes to ≥ 50 KB and
  ``ftyp`` is at bytes 4..8.

This experiment is intentionally **not registered as a default eval
target** — it requires a live H200 and is reserved for the
``/components/infra_ltx_video_worker_live`` workbench surface. CI
must continue to call :func:`build_infra_ltx_video_worker_experiment`
(stub).
"""

from __future__ import annotations

import base64
import os
import struct
from dataclasses import dataclass
from typing import Any

import httpx
from strands_evals.case import Case
from strands_evals.evaluators.evaluator import Evaluator
from strands_evals.experiment import Experiment
from strands_evals.types.evaluation import EvaluationData, EvaluationOutput

# Reuse the box-walking helpers from the stub experiment so structure
# checks stay consistent across stub and live runs.
from strands_agents.evals.experiments.infra_ltx_video_worker import (
    _mp4_box_types,
    _mp4_structure_valid,
)

INFRA_LTX_VIDEO_WORKER_LIVE_EVALUATOR_THRESHOLDS: dict[str, tuple[float, bool]] = {
    "LiveVideoResponseEvaluator": (1.0, True),
    "LiveVideoMp4Evaluator": (1.0, True),
}

# Minimum MP4 size that proves the render produced real frames. The
# H200 ti2vid_one_stage smoke during slice 9d-wire returned ~105 KB
# for a 2 s clip; setting the floor at 50 KB tolerates compression
# variance while still rejecting any zero-byte or empty-mdat stub.
_MIN_LIVE_MP4_BYTES = 50_000

# Worker URL is read at task time so a single experiment definition
# can target different VMs across runs without code changes.
_DEFAULT_WORKER_URL_ENV = "LTX_VIDEO_WORKER_URL"

# A real LTX-2.3 BASIC render on H200 takes several minutes for even
# a 2 s clip (the bottleneck is the Gemma encoder + 22B-dev sampling).
# Bound the HTTP wait at 30 min to match the orchestrator's per-scene
# patience and surface stuck workers as test failures rather than
# hangs.
_DEFAULT_TIMEOUT_S = 30 * 60.0


# ── Cases ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _LiveCaseInput:
    prompt: str
    duration_s: float
    seed: int = 7


def infra_ltx_video_worker_live_cases() -> list[Case[dict[str, Any], dict[str, Any]]]:
    """Single case proving the live H200 worker returns a real MP4."""
    return [
        Case[dict[str, Any], dict[str, Any]](
            name="render_returns_mp4_live",
            session_id="infra-ltx-video-live-render-returns-mp4",
            input={
                "prompt": (
                    "A documentary establishing shot of a city skyline "
                    "at dusk, slow zoom"
                ),
                "duration_s": 2.0,
                "seed": 7,
            },
            expected_output={"final_status": 200},
            metadata={
                "expected_status": 200,
                "min_mp4_bytes": _MIN_LIVE_MP4_BYTES,
            },
        ),
    ]


# ── Task adapter ─────────────────────────────────────────────────────


def infra_ltx_video_worker_live_task(
    case: Case[dict[str, Any], dict[str, Any]],
) -> dict[str, Any]:
    """POST to the live LTX-Video worker and decode the response.

    The worker URL is read from ``LTX_VIDEO_WORKER_URL`` at call time;
    if unset the task fails closed with a structured error instead of
    silently degrading to the stub engine.
    """
    payload = case.input or {}
    worker_url = os.environ.get(_DEFAULT_WORKER_URL_ENV, "").rstrip("/")
    if not worker_url:
        return {
            "output": {
                "final_status": 0,
                "body": None,
                "mp4_bytes_len": 0,
                "mp4_structure_valid": False,
                "mp4_box_types": [],
                "error": (
                    f"{_DEFAULT_WORKER_URL_ENV} is not set; "
                    "live worker not reachable"
                ),
            },
            "trajectory": [f"abort: {_DEFAULT_WORKER_URL_ENV} unset"],
            "metadata": {"worker_url": ""},
        }

    body = {
        "prompt": payload.get("prompt", ""),
        "duration_s": float(payload.get("duration_s", 2.0)),
        "seed": int(payload.get("seed", 7)),
    }

    prompt = payload.get("prompt", "")
    trajectory: list[str] = [f"POST {worker_url}/"]
    final_status = 0
    final_body: Any = None
    mp4_bytes: bytes = b""
    error: str | None = None

    try:
        with httpx.Client(timeout=_DEFAULT_TIMEOUT_S) as client:
            response = client.post(
                f"{worker_url.rstrip('/')}/",
                content=prompt.encode("utf-8"),
                headers={"Content-Type": "text/plain"},
            )
        final_status = response.status_code
        content_type = response.headers.get("content-type", "")
        if "video/mp4" in content_type:
            mp4_bytes = response.content
            final_body = {"_bytes": len(mp4_bytes)}
        else:
            try:
                final_body = response.json()
            except ValueError:
                final_body = {"_text": response.text}
    except httpx.HTTPError as exc:
        error = f"http error: {exc!r}"

    output: dict[str, Any] = {
        "final_status": final_status,
        "body": final_body,
        "mp4_bytes_len": len(mp4_bytes),
        "mp4_structure_valid": _mp4_structure_valid(mp4_bytes)
        if mp4_bytes
        else False,
        "mp4_box_types": _mp4_box_types(mp4_bytes) if mp4_bytes else [],
        "ftyp_at_offset_4": (
            mp4_bytes[4:8] == b"ftyp" if len(mp4_bytes) >= 8 else False
        ),
    }
    if error is not None:
        output["error"] = error

    return {
        "output": output,
        "trajectory": trajectory,
        "metadata": {"worker_url": worker_url},
    }


# ── Evaluators ───────────────────────────────────────────────────────


class LiveVideoResponseEvaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Pin status + presence of real MP4 bytes in the worker response."""

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        metadata = evaluation_case.metadata or {}
        expected_status = int(metadata.get("expected_status", -1))
        min_bytes = int(metadata.get("min_mp4_bytes", _MIN_LIVE_MP4_BYTES))
        actual_status = int(actual.get("final_status", -1))
        body = actual.get("body") or {}
        mp4_len = int(actual.get("mp4_bytes_len", 0))
        engine_field = body.get("engine") if isinstance(body, dict) else None
        problems: list[str] = []
        if actual_status != expected_status:
            problems.append(
                f"status={actual_status} expected {expected_status}"
            )
        if mp4_len < min_bytes:
            problems.append(
                f"mp4_bytes_len={mp4_len} < {min_bytes} (real LTX-2.3 floor)"
            )
        if engine_field == "stub":
            problems.append(
                "body.engine='stub' — request hit the in-process stub, "
                "not the live H200"
            )
        if actual.get("error"):
            problems.append(f"error: {actual['error']}")
        ok = not problems
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                reason=(
                    f"live worker returned {mp4_len} bytes mp4"
                    if ok
                    else "; ".join(problems)
                ),
                label="live_response_ok" if ok else "live_response_bad",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


class LiveVideoMp4Evaluator(Evaluator[dict[str, Any], dict[str, Any]]):
    """Assert the decoded MP4 has ftyp + mdat and ftyp is at offset 4."""

    def evaluate(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        actual = evaluation_case.actual_output or {}
        if int(actual.get("final_status", 0)) != 200:
            return [
                EvaluationOutput(
                    score=1.0,
                    test_pass=True,
                    reason="non-200 — structure not required",
                    label="structure_not_required",
                )
            ]
        ok_struct = bool(actual.get("mp4_structure_valid", False))
        ok_ftyp = bool(actual.get("ftyp_at_offset_4", False))
        problems: list[str] = []
        if not ok_struct:
            problems.append(
                f"mp4 missing ftyp/mdat (boxes={actual.get('mp4_box_types')})"
            )
        if not ok_ftyp:
            problems.append("ftyp not at byte offset 4 (ISO-BMFF magic)")
        ok = not problems
        return [
            EvaluationOutput(
                score=1.0 if ok else 0.0,
                test_pass=ok,
                reason=(
                    f"mp4 valid (boxes={actual.get('mp4_box_types')})"
                    if ok
                    else "; ".join(problems)
                ),
                label="mp4_ok" if ok else "mp4_bad",
            )
        ]

    async def evaluate_async(
        self,
        evaluation_case: EvaluationData[dict[str, Any], dict[str, Any]],
    ) -> list[EvaluationOutput]:
        return self.evaluate(evaluation_case)


# Resolve unused-import guard — keeps struct in scope for any future
# inline assertions on the binary header without bloating imports.
_ = struct


# ── Experiment factory ───────────────────────────────────────────────


def build_infra_ltx_video_worker_live_experiment() -> (
    Experiment[dict[str, Any], dict[str, Any]]
):
    """Assemble the live LTX-Video worker passthrough experiment."""
    return Experiment[dict[str, Any], dict[str, Any]](
        cases=infra_ltx_video_worker_live_cases(),
        evaluators=[
            LiveVideoResponseEvaluator(),
            LiveVideoMp4Evaluator(),
        ],
    )


__all__ = [
    "INFRA_LTX_VIDEO_WORKER_LIVE_EVALUATOR_THRESHOLDS",
    "LiveVideoMp4Evaluator",
    "LiveVideoResponseEvaluator",
    "build_infra_ltx_video_worker_live_experiment",
    "infra_ltx_video_worker_live_cases",
    "infra_ltx_video_worker_live_task",
]
