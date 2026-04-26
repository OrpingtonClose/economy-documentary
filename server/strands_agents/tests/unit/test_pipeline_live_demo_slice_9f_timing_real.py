"""Unit tests for slice 9f-timing-real — wire real ``evaluate_timing``.

Slice 9f-timing-real removes the placeholder ``evaluate_timing`` from
the pipeline tool list and binds the real
:func:`strands_agents.timing_tool.evaluate_timing` instead, so the
orchestrator's timing loop computes ``timing_passed`` against actual
WhisperX-aligned narration durations.

The slice spans two surfaces:

* The real-worker audio dispatcher
  (:func:`strands_agents.playground.pipeline_live_real_workers
  .build_real_worker_tools`) must surface the TTS engine's reported
  ``duration_s`` as a per-scene ``alignment`` envelope field, so the
  orchestrator can aggregate per-scene durations into the
  ``whisperx_alignment`` payload the timing tool consumes.
* The scripted demo orchestrator
  (:func:`strands_agents.playground.pipeline_live_demo._demo_chat_script`)
  must call the real ``evaluate_timing`` tool with the
  ``scenes`` / ``whisperx_alignment`` shape — not the legacy
  placeholder shape (``timeline`` / ``alignment``).

These tests pin down both surfaces and add anti-drift assertions so a
future placeholder swap is caught at CI time, not at render time.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx
import pytest

from strands_agents import _placeholders
from strands_agents.playground import pipeline_live_demo as demo
from strands_agents.playground.pipeline_live_real_workers import (
    build_real_worker_tools,
)
from strands_agents.timing_tool import (
    compute_timing_report,
    evaluate_timing as real_evaluate_timing,
)


# ---------------------------------------------------------------------------
# Demo binds the REAL evaluate_timing (anti-drift on placeholder usage)
# ---------------------------------------------------------------------------


class TestDemoBindsRealEvaluateTiming:
    """The demo's tool list must use ``timing_tool.evaluate_timing``."""

    @staticmethod
    def _tool_name(tool: Any) -> str | None:
        """Resolve a name from either LangChain or Strands tool wrappers.

        LangChain ``StructuredTool`` exposes ``.name``; Strands
        ``DecoratedFunctionTool`` exposes ``.tool_name``.
        """
        return getattr(tool, "name", None) or getattr(tool, "tool_name", None)

    def test_real_evaluate_timing_is_in_demo_tools(self) -> None:
        tools = demo._demo_tools()
        names = [self._tool_name(t) for t in tools]
        assert names.count("evaluate_timing") == 1
        # Identity check: bound tool must be the import from ``timing_tool``,
        # not the placeholder echo from ``_placeholders``.
        assert real_evaluate_timing in tools
        assert _placeholders.evaluate_timing not in tools

    def test_placeholder_evaluate_timing_is_not_in_demo_tools(self) -> None:
        """Anti-drift: a future revert to placeholder must fail CI."""
        tools = demo._demo_tools()
        for t in tools:
            assert t is not _placeholders.evaluate_timing, (
                f"placeholder evaluate_timing leaked into demo tool list: {t}"
            )

    def test_real_tool_signature_matches_timing_tool(self) -> None:
        """The bound tool must accept the timing_tool argument keys."""
        # ``DecoratedFunctionTool._tool_spec`` carries the JSON schema.
        spec = real_evaluate_timing._tool_spec
        schema = spec["inputSchema"]["json"]
        required = set(schema.get("required", []))
        # The placeholder takes ``timeline`` / ``alignment``; the real
        # tool takes ``scenes`` / ``whisperx_alignment``. This pins the
        # expected shape.
        assert "scenes" in required
        assert "whisperx_alignment" in required
        assert "target_duration_sec" in required
        assert "timeline" not in schema.get("properties", {})
        assert "alignment" not in schema.get("properties", {})


# ---------------------------------------------------------------------------
# Demo script wires the new shape
# ---------------------------------------------------------------------------


def _tool_calls(msg: Any) -> list[dict[str, Any]]:
    return list(getattr(msg, "tool_calls", []) or [])


class TestDemoScriptShapesEvaluateTimingArgs:
    """The scripted demo's ``evaluate_timing`` AIMessage carries the
    real tool's expected input shape."""

    def _timing_call(self, num_scenes: int = 3) -> dict[str, Any]:
        script = demo._demo_chat_script(
            topic="The Federal Reserve",
            target_duration_sec=60,
            language="en",
            num_scenes=num_scenes,
        )
        return next(
            call
            for msg in script
            for call in _tool_calls(msg)
            if call["name"] == "evaluate_timing"
        )

    def test_scenes_arg_is_list_of_dicts_with_scene_id_and_duration(self) -> None:
        call = self._timing_call(num_scenes=4)
        scenes = call["args"]["scenes"]
        assert isinstance(scenes, list)
        assert len(scenes) == 4
        for s in scenes:
            assert "scene_id" in s
            assert "duration_sec" in s
            assert isinstance(s["duration_sec"], int | float)
            assert s["duration_sec"] > 0

    def test_scenes_arg_carries_voices_list(self) -> None:
        """``timing_tool._gap_overhead_sec`` keys off ``voices``."""
        call = self._timing_call(num_scenes=2)
        for s in call["args"]["scenes"]:
            assert "voices" in s
            assert isinstance(s["voices"], list)

    def test_whisperx_alignment_carries_total_duration_sec(self) -> None:
        call = self._timing_call(num_scenes=3)
        align = call["args"]["whisperx_alignment"]
        assert "total_duration_sec" in align
        assert isinstance(align["total_duration_sec"], int | float)
        assert align["total_duration_sec"] > 0

    def test_whisperx_alignment_carries_per_scene_list(self) -> None:
        call = self._timing_call(num_scenes=3)
        align = call["args"]["whisperx_alignment"]
        assert "per_scene" in align
        assert isinstance(align["per_scene"], list)
        assert len(align["per_scene"]) == 3
        for entry in align["per_scene"]:
            assert "scene_id" in entry
            assert "duration_sec" in entry
            assert entry["duration_sec"] > 0

    def test_whisperx_total_equals_sum_of_per_scene_durations(self) -> None:
        call = self._timing_call(num_scenes=4)
        align = call["args"]["whisperx_alignment"]
        per_scene_sum = sum(e["duration_sec"] for e in align["per_scene"])
        assert align["total_duration_sec"] == pytest.approx(per_scene_sum, rel=1e-6)

    def test_target_duration_sec_passed_through(self) -> None:
        call = self._timing_call(num_scenes=3)
        assert call["args"]["target_duration_sec"] == pytest.approx(60.0, rel=1e-6)

    def test_no_legacy_timeline_or_alignment_keys(self) -> None:
        """Anti-drift: legacy keys must not regress."""
        call = self._timing_call(num_scenes=3)
        assert "timeline" not in call["args"]
        assert "alignment" not in call["args"]


# ---------------------------------------------------------------------------
# Real-tool invocation against the demo's payload
# ---------------------------------------------------------------------------


class TestDemoPayloadRoundTripsThroughRealTool:
    """Calling the real ``evaluate_timing`` with the scripted demo's
    args succeeds (no schema errors) and returns a well-formed report."""

    def test_real_tool_accepts_scripted_demo_args(self) -> None:
        script = demo._demo_chat_script(
            topic="The Federal Reserve",
            target_duration_sec=60,
            language="en",
            num_scenes=3,
        )
        timing_call = next(
            call
            for msg in script
            for call in _tool_calls(msg)
            if call["name"] == "evaluate_timing"
        )
        report = compute_timing_report(**timing_call["args"])
        assert isinstance(report, dict)
        assert "timing_passed" in report
        # Scripted seeding uses target / num_scenes per scene, so the
        # narration sum equals target — legacy mode passes by design.
        assert report["timing_passed"] is True

    def test_real_tool_report_carries_legacy_metrics(self) -> None:
        """The real timing tool nests ``timing_report`` under the
        envelope and surfaces the metrics the orchestrator forwards to
        ``refine_scenario`` (mode, deviation, tolerance)."""
        script = demo._demo_chat_script(
            topic="topic", target_duration_sec=60, language="en", num_scenes=3
        )
        timing_call = next(
            call
            for msg in script
            for call in _tool_calls(msg)
            if call["name"] == "evaluate_timing"
        )
        report = compute_timing_report(**timing_call["args"])
        assert "timing_report" in report
        timing_report = report["timing_report"]
        # Pin the keys the orchestrator must read to decide whether
        # to refine. Drift on these breaks the timing-loop contract.
        for key in ("mode", "actual_duration_sec", "deviation_sec"):
            assert key in timing_report, (
                f"timing_report missing {key!r}: {timing_report}"
            )
        # Scripted seeding hits the legacy path (no intent target).
        assert timing_report["mode"] == "legacy"


# ---------------------------------------------------------------------------
# Audio dispatcher surfaces duration_s as alignment envelope
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, *, status_code: int, json_payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = json_payload
        self.content = b"non-empty"
        self.text = "ok"

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeHTTPXClient:
    def __init__(self, *, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def __enter__(self) -> "_FakeHTTPXClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def post(self, url: str, *, json: dict[str, Any]) -> _FakeResponse:
        self.calls.append({"url": url, "json": json})
        return self.response


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "run"


def _wav_payload(duration_s: float | int | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "engine": "qwen3-tts",
        "wav_base64": base64.b64encode(b"FAKEWAVDATA" * 32).decode("ascii"),
    }
    if duration_s is not None:
        payload["duration_s"] = duration_s
    return payload


class TestAudioDispatcherSurfacesAlignment:
    """``launch_audio_render`` must echo the TTS engine's reported
    ``duration_s`` as a per-scene ``alignment`` envelope field so the
    orchestrator can aggregate it into ``whisperx_alignment``."""

    def _invoke(
        self,
        *,
        run_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        duration_s: float | int | None,
        scene_id: str = "scene_001",
    ) -> dict[str, Any]:
        fake = _FakeHTTPXClient(
            response=_FakeResponse(
                status_code=200, json_payload=_wav_payload(duration_s)
            )
        )
        monkeypatch.setattr(httpx, "Client", lambda **_: fake)
        overrides = build_real_worker_tools(
            run_dir,
            audio_worker_url="http://audio.invalid:8000",
            video_worker_url=None,
        )
        audio_tool = overrides["launch_audio_render"]
        result = audio_tool.invoke(
            {
                "scene_id": scene_id,
                "voice_id": "Ryan",
                "text": "Real narration about the Federal Reserve.",
            }
        )
        return result

    def test_envelope_carries_alignment_when_duration_present(
        self,
        run_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._invoke(run_dir=run_dir, monkeypatch=monkeypatch, duration_s=12.5)
        alignment = result["args"]["alignment"]
        assert alignment is not None
        assert alignment["scene_id"] == "scene_001"
        assert alignment["duration_sec"] == pytest.approx(12.5, rel=1e-6)
        assert alignment["source"] == "qwen3-tts-engine-duration"

    def test_envelope_alignment_accepts_int_duration(
        self,
        run_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._invoke(run_dir=run_dir, monkeypatch=monkeypatch, duration_s=8)
        alignment = result["args"]["alignment"]
        assert alignment is not None
        assert alignment["duration_sec"] == pytest.approx(8.0, rel=1e-6)

    def test_envelope_alignment_none_when_duration_missing(
        self,
        run_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._invoke(run_dir=run_dir, monkeypatch=monkeypatch, duration_s=None)
        assert result["args"]["alignment"] is None

    def test_envelope_alignment_none_when_duration_zero(
        self,
        run_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._invoke(run_dir=run_dir, monkeypatch=monkeypatch, duration_s=0)
        assert result["args"]["alignment"] is None

    def test_envelope_alignment_none_when_duration_negative(
        self,
        run_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._invoke(run_dir=run_dir, monkeypatch=monkeypatch, duration_s=-1.5)
        assert result["args"]["alignment"] is None

    def test_envelope_alignment_carries_supplied_scene_id(
        self,
        run_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._invoke(
            run_dir=run_dir,
            monkeypatch=monkeypatch,
            duration_s=20.0,
            scene_id="scene_007",
        )
        assert result["args"]["alignment"]["scene_id"] == "scene_007"

    def test_envelope_keeps_existing_engine_and_wav_fields(
        self,
        run_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Adding ``alignment`` must not displace prior fields."""
        result = self._invoke(run_dir=run_dir, monkeypatch=monkeypatch, duration_s=5.0)
        args = result["args"]
        assert args["engine"] == "qwen3-tts"
        assert args["wav_bytes_len"] > 0
        assert "alignment" in args


# ---------------------------------------------------------------------------
# Aggregation: dispatcher envelope → whisperx_alignment payload
# ---------------------------------------------------------------------------


class TestEnvelopeAggregatesIntoWhisperxAlignment:
    """The orchestrator aggregates per-scene ``alignment`` envelopes
    from N ``launch_audio_render`` calls into the
    ``whisperx_alignment`` payload the real timing tool consumes.
    Pin the aggregation contract here so a downstream rename can't
    silently break the timing loop.
    """

    def test_aggregation_produces_real_tool_compatible_payload(self) -> None:
        # Simulate three dispatched scenes with realistic durations.
        envelopes = [
            {
                "scene_id": "scene_001",
                "duration_sec": 18.4,
                "source": "qwen3-tts-engine-duration",
            },
            {
                "scene_id": "scene_002",
                "duration_sec": 21.1,
                "source": "qwen3-tts-engine-duration",
            },
            {
                "scene_id": "scene_003",
                "duration_sec": 19.7,
                "source": "qwen3-tts-engine-duration",
            },
        ]
        whisperx_alignment = {
            "total_duration_sec": float(sum(e["duration_sec"] for e in envelopes)),
            "per_scene": [
                {"scene_id": e["scene_id"], "duration_sec": e["duration_sec"]}
                for e in envelopes
            ],
        }
        scenes = [
            {"scene_id": e["scene_id"], "duration_sec": e["duration_sec"], "voices": []}
            for e in envelopes
        ]
        # The real timing tool must accept this payload without error.
        report = compute_timing_report(
            scenes=scenes,
            whisperx_alignment=whisperx_alignment,
            target_duration_sec=whisperx_alignment["total_duration_sec"],
        )
        assert report["timing_passed"] is True
