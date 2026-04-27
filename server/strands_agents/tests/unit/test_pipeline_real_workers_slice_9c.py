"""Unit tests for slice 9c — real LLM narration + visual prompts on dispatch.

Slice 9c extends ``launch_audio_render`` with an optional ``text``
argument and ``launch_visual_production`` with an optional ``prompt``
argument so the orchestrator can pass the actual scenario narration
and a fully-formed visual prompt down to the real Qwen3-TTS / LTX-2.3
workers, instead of a hard-coded "Documentary narration for scene X"
caption.

These tests pin down:

* The placeholders accept the new optional kwargs and echo them in
  their envelopes (backward-compatible default ``None``).
* :func:`_resolve_audio_text` prefers the orchestrator-supplied
  ``text`` and falls back to the legacy placeholder line on empty /
  missing input.
* :func:`_resolve_visual_prompt` prefers ``prompt``, synthesises a
  rich string from the structured ``visual_concept`` when ``prompt``
  is missing, and falls back to a generic establishing-shot line only
  when both are empty.
* The real-worker dispatchers send the resolved strings to the
  workers and surface them in the returned envelope so the SSE
  envelope-lift (slice 9d-wire follow-up) carries real content.
* The orchestrator system prompt instructs the LLM to pass the new
  arguments through, so a future LLM swap doesn't silently regress
  to caption-only renders.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx
import pytest

from strands_agents import _placeholders
from strands_agents.pipeline import ORCHESTRATOR_PROMPT
from strands_agents.playground.pipeline_live_real_workers import (
    _resolve_audio_text,
    _resolve_visual_prompt,
    build_real_worker_tools,
)


# ---------------------------------------------------------------------------
# Placeholder signature
# ---------------------------------------------------------------------------


class TestPlaceholderSignatures:
    def test_audio_placeholder_accepts_text_kwarg(self) -> None:
        result = _placeholders.launch_audio_render.invoke(
            {
                "scene_id": "scene_001",
                "voice_id": "Ryan",
                "text": "Real narration about the Federal Reserve.",
            }
        )

        assert result["status"] == "placeholder"
        assert result["tool"] == "launch_audio_render"
        assert result["args"]["text"] == ("Real narration about the Federal Reserve.")

    def test_audio_placeholder_text_defaults_to_none(self) -> None:
        result = _placeholders.launch_audio_render.invoke(
            {"scene_id": "scene_001", "voice_id": "Ryan"}
        )

        assert result["args"]["text"] is None

    def test_visual_placeholder_accepts_prompt_kwarg(self) -> None:
        result = _placeholders.launch_visual_production.invoke(
            {
                "scene_id": "scene_001",
                "visual_concept": {"shot_type": "wide"},
                "prompt": "Cinematic wide shot, golden hour, static frame.",
            }
        )

        assert result["status"] == "placeholder"
        assert result["tool"] == "launch_visual_production"
        assert result["args"]["prompt"] == (
            "Cinematic wide shot, golden hour, static frame."
        )

    def test_visual_placeholder_prompt_defaults_to_none(self) -> None:
        result = _placeholders.launch_visual_production.invoke(
            {"scene_id": "scene_001", "visual_concept": {}}
        )

        assert result["args"]["prompt"] is None


# ---------------------------------------------------------------------------
# _resolve_audio_text
# ---------------------------------------------------------------------------


class TestResolveAudioText:
    def test_uses_supplied_text_when_present(self) -> None:
        text = "The Federal Reserve sets monetary policy in the United States."

        assert _resolve_audio_text("scene_001", text) == text

    def test_strips_whitespace_around_text(self) -> None:
        assert _resolve_audio_text("scene_001", "  hello world  ") == ("hello world")

    def test_falls_back_to_placeholder_on_none(self) -> None:
        out = _resolve_audio_text("scene_001", None)

        assert "scene_001" in out
        assert "Documentary narration" in out

    def test_falls_back_to_placeholder_on_empty(self) -> None:
        out = _resolve_audio_text("scene_001", "   ")

        assert "Documentary narration" in out

    def test_falls_back_to_placeholder_on_non_string(self) -> None:
        # Defensive: orchestrator might leak a non-string from a buggy
        # tool call. Resolver must not crash.
        out = _resolve_audio_text("scene_001", 12345)  # type: ignore[arg-type]

        assert "Documentary narration" in out


# ---------------------------------------------------------------------------
# _resolve_visual_prompt
# ---------------------------------------------------------------------------


class TestResolveVisualPrompt:
    def test_prompt_kwarg_wins_over_concept(self) -> None:
        prompt = "Cinematic dolly in on a bustling trading floor, dusk light."
        concept = {"phrases": ["different concept text"], "mood": "frenetic"}

        assert _resolve_visual_prompt(concept, prompt) == prompt

    def test_synthesises_from_concept_phrases(self) -> None:
        concept = {
            "phrases": ["Wide shot of a bank vault", "soft fluorescent light"],
        }

        out = _resolve_visual_prompt(concept, None)

        assert "Wide shot of a bank vault" in out
        assert "soft fluorescent light" in out

    def test_synthesises_full_concept_fields(self) -> None:
        concept = {
            "phrases": ["Aerial pan over a wind farm"],
            "shot_type": "establishing wide",
            "camera_movement": "slow pan left",
            "mood": "hopeful, contemplative",
            "palette": "cool blues",
            "style": "documentary",
        }

        out = _resolve_visual_prompt(concept, None)

        assert "Aerial pan over a wind farm" in out
        assert "shot type: establishing wide" in out
        assert "camera movement: slow pan left" in out
        assert "mood: hopeful, contemplative" in out
        assert "palette: cool blues" in out

    def test_handles_concept_list_fields(self) -> None:
        concept = {"palette": ["amber", "charcoal", "sepia"]}

        out = _resolve_visual_prompt(concept, None)

        assert "palette: amber, charcoal, sepia" in out

    def test_falls_back_when_concept_empty_and_prompt_empty(self) -> None:
        out = _resolve_visual_prompt({}, None)

        assert "establishing shot" in out

    def test_falls_back_when_concept_not_dict(self) -> None:
        out = _resolve_visual_prompt(None, None)

        assert "establishing shot" in out


# ---------------------------------------------------------------------------
# Real-worker dispatcher integration (httpx mocked)
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
    """Captures ``httpx.Client.post`` calls for assertion."""

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


def _wav_payload() -> dict[str, Any]:
    return {
        "engine": "qwen3-tts",
        "wav_base64": base64.b64encode(b"FAKEWAVDATA" * 32).decode("ascii"),
    }


def _mp4_payload() -> dict[str, Any]:
    return {
        "engine": "ltx-video",
        "mp4_base64": base64.b64encode(b"FAKEMP4DATA" * 32).decode("ascii"),
    }


class TestRealAudioDispatcher:
    def test_sends_resolved_text_to_worker(
        self,
        run_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake = _FakeHTTPXClient(
            response=_FakeResponse(status_code=200, json_payload=_wav_payload())
        )
        monkeypatch.setattr(httpx, "Client", lambda **_: fake)

        overrides = build_real_worker_tools(
            run_dir,
            audio_worker_url="http://audio.invalid:8000",
            video_worker_url=None,
        )
        audio_tool = overrides["launch_audio_render"]

        narration = (
            "Inflation is the rate at which prices rise across an economy. "
            "When central banks raise rates, borrowing slows."
        )
        result = audio_tool.invoke(
            {
                "scene_id": "scene_001",
                "voice_id": "Ryan",
                "text": narration,
            }
        )

        assert len(fake.calls) == 1
        assert fake.calls[0]["url"] == "http://audio.invalid:8000/tts/render"
        assert fake.calls[0]["json"]["text"] == narration
        # Envelope round-trips the resolved text so the SSE
        # envelope-lift can surface it on the wire.
        assert result["args"]["text"] == narration
        assert result["args"]["wav_bytes_len"] > 0
        assert result["args"]["engine"] == "qwen3-tts"

    def test_falls_back_to_placeholder_when_text_missing(
        self,
        run_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake = _FakeHTTPXClient(
            response=_FakeResponse(status_code=200, json_payload=_wav_payload())
        )
        monkeypatch.setattr(httpx, "Client", lambda **_: fake)

        overrides = build_real_worker_tools(
            run_dir,
            audio_worker_url="http://audio.invalid:8000",
        )
        audio_tool = overrides["launch_audio_render"]

        audio_tool.invoke({"scene_id": "scene_001", "voice_id": "Ryan"})

        body_text = fake.calls[0]["json"]["text"]
        assert "scene_001" in body_text
        assert "Documentary narration" in body_text


class TestRealVisualDispatcher:
    def test_sends_resolved_prompt_to_worker(
        self,
        run_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake = _FakeHTTPXClient(
            response=_FakeResponse(status_code=200, json_payload=_mp4_payload())
        )
        monkeypatch.setattr(httpx, "Client", lambda **_: fake)

        overrides = build_real_worker_tools(
            run_dir,
            video_worker_url="http://video.invalid:9000",
        )
        video_tool = overrides["launch_visual_production"]

        prompt = (
            "Cinematic establishing shot of a Federal Reserve marble lobby, "
            "slow dolly in, soft natural daylight, muted earth tones."
        )
        result = video_tool.invoke(
            {
                "scene_id": "scene_001",
                "visual_concept": {"phrases": ["should be ignored when prompt set"]},
                "prompt": prompt,
            }
        )

        assert fake.calls[0]["url"] == "http://video.invalid:9000/video/render"
        assert fake.calls[0]["json"]["prompt"] == prompt
        assert result["args"]["prompt"] == prompt
        assert result["args"]["mp4_bytes_len"] > 0
        assert result["args"]["engine"] == "ltx-video"

    def test_synthesises_prompt_from_concept_when_prompt_missing(
        self,
        run_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake = _FakeHTTPXClient(
            response=_FakeResponse(status_code=200, json_payload=_mp4_payload())
        )
        monkeypatch.setattr(httpx, "Client", lambda **_: fake)

        overrides = build_real_worker_tools(
            run_dir,
            video_worker_url="http://video.invalid:9000",
        )
        video_tool = overrides["launch_visual_production"]

        concept = {
            "phrases": ["Aerial of a wind farm at dawn"],
            "mood": "hopeful",
            "palette": "cool blues",
        }
        video_tool.invoke(
            {
                "scene_id": "scene_001",
                "visual_concept": concept,
            }
        )

        sent_prompt = fake.calls[0]["json"]["prompt"]
        assert "Aerial of a wind farm at dawn" in sent_prompt
        assert "mood: hopeful" in sent_prompt
        assert "palette: cool blues" in sent_prompt


# ---------------------------------------------------------------------------
# GPU-lock serialisation
# ---------------------------------------------------------------------------


class TestVisualDispatchSerialisation:
    """AGENTS.md hard invariant: a single GPU worker must never receive
    parallel ``/video/render`` requests. ``_video_dispatch_lock`` keeps
    the queue at depth=1 even when LangGraph fires the @tool callable
    concurrently across multiple scene dispatches.
    """

    def test_concurrent_invocations_are_serialised(
        self,
        run_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import threading
        import time as _time

        in_flight = 0
        max_in_flight = 0
        lock = threading.Lock()

        class _SlowFakeHTTPXClient:
            def __enter__(self) -> "_SlowFakeHTTPXClient":
                return self

            def __exit__(self, *exc: Any) -> None:
                return None

            def post(self, url: str, *, json: dict[str, Any]) -> _FakeResponse:
                nonlocal in_flight, max_in_flight
                with lock:
                    in_flight += 1
                    max_in_flight = max(max_in_flight, in_flight)
                _time.sleep(0.05)
                with lock:
                    in_flight -= 1
                return _FakeResponse(status_code=200, json_payload=_mp4_payload())

        monkeypatch.setattr(httpx, "Client", lambda **_: _SlowFakeHTTPXClient())

        overrides = build_real_worker_tools(
            run_dir,
            video_worker_url="http://video.invalid:9000",
        )
        video_tool = overrides["launch_visual_production"]

        results: list[dict[str, Any]] = []

        def _invoke(scene_id: str) -> None:
            results.append(
                video_tool.invoke(
                    {
                        "scene_id": scene_id,
                        "visual_concept": {"phrases": ["test"]},
                        "prompt": f"prompt for {scene_id}",
                    }
                )
            )

        threads = [
            threading.Thread(target=_invoke, args=(f"scene_{i:03d}",)) for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 5
        # Every dispatch must succeed end-to-end \u2014 the original bug had
        # 4/5 returning 400 because the worker was hammered concurrently.
        for r in results:
            assert r["args"]["mp4_bytes_len"] > 0
            assert r["args"]["engine"] == "ltx-video"
        # The lock keeps in-flight count at \u22641 across the whole call.
        assert max_in_flight == 1, (
            f"expected serialised dispatch (max_in_flight=1), saw {max_in_flight}"
        )


# ---------------------------------------------------------------------------
# Orchestrator prompt guidance — anti-drift assertion
# ---------------------------------------------------------------------------


class TestOrchestratorPromptGuidance:
    def test_prompt_directs_llm_to_pass_narration_text(self) -> None:
        # If a future LLM rewrites the orchestrator prompt and drops
        # the narration-text guidance, this assertion fails so the
        # regression is caught in CI before live workers go quiet.
        assert "text`` argument to" in ORCHESTRATOR_PROMPT
        assert "launch_audio_render" in ORCHESTRATOR_PROMPT

    def test_prompt_directs_llm_to_pass_visual_prompt(self) -> None:
        assert "prompt`` string" in ORCHESTRATOR_PROMPT
        assert "launch_visual_production" in ORCHESTRATOR_PROMPT
