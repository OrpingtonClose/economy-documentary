"""Unit tests for the dispatcher-envelope lift on ``pipeline.tool.X.end``.

Slice 9d-wire's real-worker tools return envelopes that carry
``engine``, ``wav_bytes_len``, ``mp4_bytes_len`` and ``status_code``
under ``args``. Phase B's B5/B6 trajectory assertions rely on those
fields making it into the SSE event detail. These tests pin down:

* :func:`_extract_envelope_fields` extracts only the whitelisted keys
  and prefers ``args``-nested values over top-level when both exist.
* :func:`translate_pipeline_event` propagates the envelope into the
  ``pipeline.tool.X.end`` detail and keeps backwards compatibility
  for events that don't carry one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from strands_agents.playground.pipeline_adapter import translate_pipeline_event
from strands_agents.playground.pipeline_live_runner import _extract_envelope_fields


@dataclass
class _FakeToolMessage:
    """Minimal stand-in for ``langchain_core.messages.ToolMessage``.

    LangChain's tool-end callback wraps a tool's return value in a
    ``ToolMessage`` whose ``content`` is the tool output, JSON-encoded
    when the tool returned a non-string. The extractor must walk through
    this wrapper to reach the underlying envelope.
    """

    content: Any
    name: str = "fake_tool"
    tool_call_id: str = "call_test"


def test_extract_envelope_fields_pulls_whitelisted_keys_from_args() -> None:
    output = {
        "status": "real-worker-dispatched",
        "tool": "launch_audio_render",
        "args": {
            "scene_id": "scene_001",
            "voice_id": "Ryan",
            "engine": "qwen3-tts",
            "wav_bytes_len": 314_159,
            "status_code": 200,
            "wav_path": "/tmp/run/artifacts/scene_001-abc.wav",
            "elapsed_ms": 18_000,
        },
    }

    extracted = _extract_envelope_fields(output)

    assert extracted == {
        "scene_id": "scene_001",
        "engine": "qwen3-tts",
        "wav_bytes_len": 314_159,
        "status_code": 200,
        "wav_path": "/tmp/run/artifacts/scene_001-abc.wav",
    }


def test_extract_envelope_fields_handles_top_level_dispatcher_shape() -> None:
    output = {
        "engine": "ltx-video",
        "mp4_bytes_len": 271_828,
        "status_code": 200,
        "mp4_path": "/tmp/run/artifacts/scene_001-def.mp4",
    }

    extracted = _extract_envelope_fields(output)

    assert extracted == {
        "engine": "ltx-video",
        "mp4_bytes_len": 271_828,
        "status_code": 200,
        "mp4_path": "/tmp/run/artifacts/scene_001-def.mp4",
    }


def test_extract_envelope_fields_returns_empty_for_non_dict() -> None:
    assert _extract_envelope_fields(None) == {}
    assert _extract_envelope_fields("not a dict") == {}
    assert _extract_envelope_fields(42) == {}


def test_extract_envelope_fields_returns_empty_when_no_whitelisted_keys() -> None:
    output = {"status": "ok", "args": {"voice_id": "Ryan"}}
    assert _extract_envelope_fields(output) == {}


def test_extract_envelope_fields_lifts_qa_gate_envelope_top_level() -> None:
    """QA gates return a top-level dict (no ``args`` nesting).

    The :file:`/pipeline` UI's per-scene metric cards depend on every
    QA verdict + measurement reaching the SSE wire. The slice 9q
    post-mortem traced empty rows back to a too-narrow whitelist that
    stripped ``scene_id`` and ``verdict`` here. This test pins the
    fields the metric cards read.
    """
    output = {
        "tool": "qa_duration_align",
        "scene_id": "scene_002",
        "verdict": "pass",
        "audio_duration_s": 12.34,
        "video_duration_s": 12.40,
        "delta_s": 0.06,
        "tolerance_s": 0.5,
    }

    assert _extract_envelope_fields(output) == {
        "scene_id": "scene_002",
        "verdict": "pass",
        "audio_duration_s": 12.34,
        "video_duration_s": 12.40,
        "delta_s": 0.06,
        "tolerance_s": 0.5,
    }


def test_extract_envelope_fields_lifts_qa_audio_completeness_envelope() -> None:
    output = {
        "tool": "qa_audio_completeness",
        "scene_id": "scene_003",
        "verdict": "pass",
        "audio_duration_s": 13.21,
        "trailing_silence_s": 0.184,
        "tail_rms_db": -47.6,
        "min_trailing_silence_s": 0.15,
        "max_tail_rms_db": -25.0,
        "silence_noise_db": -45.0,
        "tail_window_s": 0.05,
    }

    assert _extract_envelope_fields(output) == {
        "scene_id": "scene_003",
        "verdict": "pass",
        "audio_duration_s": 13.21,
        "trailing_silence_s": 0.184,
        "tail_rms_db": -47.6,
        "min_trailing_silence_s": 0.15,
        "max_tail_rms_db": -25.0,
        "silence_noise_db": -45.0,
        "tail_window_s": 0.05,
    }


def test_extract_envelope_fields_lifts_qa_stills_judge_envelope() -> None:
    output = {
        "tool": "qa_stills_judge",
        "scene_id": "scene_004",
        "verdict": "pass",
        "mean_pixel_delta": 8.7,
        "min_mean_pixel_delta": 1.5,
        "num_samples": 8,
    }

    assert _extract_envelope_fields(output) == {
        "scene_id": "scene_004",
        "verdict": "pass",
        "mean_pixel_delta": 8.7,
        "min_mean_pixel_delta": 1.5,
        "num_samples": 8,
    }


def test_extract_envelope_fields_unwraps_tool_message_with_dict_content() -> None:
    """``ToolMessage(content={...})`` shape — defensive path."""
    inner = {
        "status": "real-worker-dispatched",
        "tool": "launch_audio_render",
        "args": {"engine": "qwen3-tts", "wav_bytes_len": 1_000},
    }
    output = _FakeToolMessage(content=inner)

    assert _extract_envelope_fields(output) == {
        "engine": "qwen3-tts",
        "wav_bytes_len": 1_000,
    }


def test_extract_envelope_fields_unwraps_tool_message_with_json_string_content() -> None:
    """``ToolMessage(content=json.dumps({...}))`` — production shape.

    LangChain's ``ToolNode`` JSON-encodes any non-string return so the
    envelope arrives wrapped twice: ``ToolMessage`` outside, JSON string
    inside. The extractor must unwrap both.
    """
    inner = {
        "status": "real-worker-dispatched",
        "tool": "launch_visual_production",
        "args": {
            "engine": "ltx-video",
            "mp4_bytes_len": 279_774,
            "status_code": 200,
            "mp4_path": "/tmp/run/artifacts/scene_001.mp4",
        },
    }
    output = _FakeToolMessage(content=json.dumps(inner))

    assert _extract_envelope_fields(output) == {
        "engine": "ltx-video",
        "mp4_bytes_len": 279_774,
        "status_code": 200,
        "mp4_path": "/tmp/run/artifacts/scene_001.mp4",
    }


def test_extract_envelope_fields_unwraps_bare_json_string() -> None:
    """Plain JSON-string output (no ``ToolMessage`` wrapper)."""
    output = json.dumps(
        {
            "status": "real-worker-dispatched",
            "tool": "launch_audio_render",
            "args": {"engine": "qwen3-tts", "wav_bytes_len": 1_234},
        }
    )

    assert _extract_envelope_fields(output) == {
        "engine": "qwen3-tts",
        "wav_bytes_len": 1_234,
    }


def test_extract_envelope_fields_returns_empty_for_invalid_json_string() -> None:
    """Defensive — malformed JSON must never raise from the extractor."""
    assert _extract_envelope_fields("{not valid json") == {}
    assert _extract_envelope_fields("plain text, not even json-shaped") == {}


def test_translate_pipeline_event_lifts_envelope_into_detail() -> None:
    translated = translate_pipeline_event(
        "pipeline.tool_call_finished",
        {
            "tool": "launch_audio_render",
            "agent": "orchestrator",
            "elapsed_ms": 21_758,
            "ok": True,
            "envelope": {
                "engine": "qwen3-tts",
                "wav_bytes_len": 314_159,
                "status_code": 200,
            },
        },
    )

    assert translated.kind == "pipeline.tool.launch_audio_render.end"
    assert translated.detail == {
        "tool": "launch_audio_render",
        "agent": "orchestrator",
        "elapsed_ms": 21_758,
        "ok": True,
        "envelope": {
            "engine": "qwen3-tts",
            "wav_bytes_len": 314_159,
            "status_code": 200,
        },
    }


def test_translate_pipeline_event_omits_envelope_when_missing() -> None:
    translated = translate_pipeline_event(
        "pipeline.tool_call_finished",
        {
            "tool": "generate_scenario",
            "agent": "orchestrator",
            "elapsed_ms": 1_234,
            "ok": True,
        },
    )

    assert translated.detail == {
        "tool": "generate_scenario",
        "agent": "orchestrator",
        "elapsed_ms": 1_234,
        "ok": True,
    }


def test_translate_pipeline_event_propagates_error_fields_when_failed() -> None:
    translated = translate_pipeline_event(
        "pipeline.tool_call_finished",
        {
            "tool": "launch_visual_production",
            "agent": "orchestrator",
            "elapsed_ms": 142,
            "ok": False,
            "error_class": "HTTPError",
            "error": "connection refused",
        },
    )

    assert translated.detail["ok"] is False
    assert translated.detail["error_class"] == "HTTPError"
    assert translated.detail["error"] == "connection refused"
