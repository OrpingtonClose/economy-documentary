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

from strands_agents.playground.pipeline_adapter import translate_pipeline_event
from strands_agents.playground.pipeline_live_runner import _extract_envelope_fields


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
    output = {"status": "ok", "args": {"scene_id": "scene_001"}}
    assert _extract_envelope_fields(output) == {}


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
