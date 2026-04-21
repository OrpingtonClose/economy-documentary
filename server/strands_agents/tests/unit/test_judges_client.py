"""Tests for the judge client wire contract."""

from __future__ import annotations

import json
from typing import Any

import pytest

from strands_agents.judges.client import (
    HttpJudgeClient,
    JudgeRequest,
    JudgeResponse,
    MockJudgeClient,
    build_judge_client,
)


# ---------------------------------------------------------------------------
# JudgeRequest
# ---------------------------------------------------------------------------


class TestJudgeRequestPayload:
    def test_text_only_payload_has_single_message_with_text_part(self) -> None:
        req = JudgeRequest(prompt="hi", system="sys")
        payload = req.to_payload()
        assert payload["temperature"] == 0.0
        assert payload["max_tokens"] == 1024
        assert payload["messages"] == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ]

    def test_no_system_prompt_skips_system_message(self) -> None:
        req = JudgeRequest(prompt="p")
        payload = req.to_payload()
        roles = [m["role"] for m in payload["messages"]]
        assert roles == ["user"]

    def test_multimodal_parts_appended_in_order(self) -> None:
        req = JudgeRequest(
            prompt="inspect",
            images=("http://img/a.png", "http://img/b.png"),
            audio_url="http://a/clip.wav",
            video_url="http://v/scene.mp4",
        )
        parts = req.to_payload()["messages"][0]["content"]
        types = [p["type"] for p in parts]
        assert types == ["text", "image_url", "image_url", "audio_url", "video_url"]
        assert parts[1]["image_url"]["url"] == "http://img/a.png"
        assert parts[3]["audio_url"]["url"] == "http://a/clip.wav"
        assert parts[4]["video_url"]["url"] == "http://v/scene.mp4"


# ---------------------------------------------------------------------------
# HttpJudgeClient
# ---------------------------------------------------------------------------


def _fake_ok_response() -> dict[str, Any]:
    return {
        "model": "judge-xyz",
        "choices": [{"message": {"content": "verdict"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }


class TestHttpJudgeClient:
    def test_constructor_rejects_empty_base_url(self) -> None:
        with pytest.raises(ValueError):
            HttpJudgeClient(base_url="", api_key="k", model="m")

    def test_constructor_rejects_empty_model(self) -> None:
        with pytest.raises(ValueError):
            HttpJudgeClient(base_url="http://x", api_key="k", model="")

    def test_complete_posts_to_chat_completions_and_parses_response(self) -> None:
        captured: dict[str, Any] = {}

        def fake_post(
            url: str,
            body: dict[str, Any],
            headers: dict[str, str],
            timeout_s: float,
        ) -> tuple[int, bytes]:
            captured["url"] = url
            captured["body"] = body
            captured["headers"] = headers
            captured["timeout_s"] = timeout_s
            return 200, json.dumps(_fake_ok_response()).encode()

        client = HttpJudgeClient(
            base_url="http://judge.local/",
            api_key="secret",
            model="judge-xyz",
            role="safety",
            request_fn=fake_post,
        )
        response = client.complete(JudgeRequest(prompt="p", system="s"))

        assert response.ok is True
        assert response.text == "verdict"
        assert response.model == "judge-xyz"
        assert response.usage["total_tokens"] == 4

        assert captured["url"] == "http://judge.local/v1/chat/completions"
        assert captured["headers"]["Authorization"] == "Bearer secret"
        assert captured["body"]["model"] == "judge-xyz"
        # System+user both forwarded
        assert len(captured["body"]["messages"]) == 2

    def test_complete_handles_non_2xx(self) -> None:
        def fake_post(*_args: Any, **_kwargs: Any) -> tuple[int, bytes]:
            return 500, b"upstream exploded"

        client = HttpJudgeClient(
            base_url="http://judge.local",
            api_key="",
            model="judge-xyz",
            request_fn=fake_post,
        )
        response = client.complete(JudgeRequest(prompt="p"))
        assert response.ok is False
        assert "http_500" in response.error

    def test_complete_handles_transport_exception(self) -> None:
        def fake_post(*_args: Any, **_kwargs: Any) -> tuple[int, bytes]:
            raise OSError("connection refused")

        client = HttpJudgeClient(
            base_url="http://judge.local",
            api_key="",
            model="judge-xyz",
            request_fn=fake_post,
        )
        response = client.complete(JudgeRequest(prompt="p"))
        assert response.ok is False
        assert "transport" in response.error

    def test_complete_handles_malformed_json(self) -> None:
        def fake_post(*_args: Any, **_kwargs: Any) -> tuple[int, bytes]:
            return 200, b"not-json"

        client = HttpJudgeClient(
            base_url="http://judge.local",
            api_key="",
            model="judge-xyz",
            request_fn=fake_post,
        )
        response = client.complete(JudgeRequest(prompt="p"))
        assert response.ok is False
        assert "json_decode" in response.error

    def test_complete_handles_empty_choices(self) -> None:
        def fake_post(*_args: Any, **_kwargs: Any) -> tuple[int, bytes]:
            return 200, json.dumps({"choices": [], "model": "judge-xyz"}).encode()

        client = HttpJudgeClient(
            base_url="http://judge.local",
            api_key="",
            model="judge-xyz",
            request_fn=fake_post,
        )
        response = client.complete(JudgeRequest(prompt="p"))
        assert response.ok is False
        assert "no_choices" in response.error

    def test_complete_concatenates_multimodal_content_parts(self) -> None:
        def fake_post(*_args: Any, **_kwargs: Any) -> tuple[int, bytes]:
            body = {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "alpha "},
                                {"type": "text", "text": "beta"},
                            ]
                        }
                    }
                ],
            }
            return 200, json.dumps(body).encode()

        client = HttpJudgeClient(
            base_url="http://judge.local",
            api_key="",
            model="m",
            request_fn=fake_post,
        )
        response = client.complete(JudgeRequest(prompt="p"))
        assert response.ok is True
        assert response.text == "alpha beta"

    def test_complete_omits_auth_header_when_api_key_empty(self) -> None:
        captured: dict[str, Any] = {}

        def fake_post(
            url: str,
            body: dict[str, Any],
            headers: dict[str, str],
            timeout_s: float,
        ) -> tuple[int, bytes]:
            captured["headers"] = headers
            return 200, json.dumps(_fake_ok_response()).encode()

        client = HttpJudgeClient(
            base_url="http://judge.local",
            api_key="",
            model="m",
            request_fn=fake_post,
        )
        client.complete(JudgeRequest(prompt="p"))
        assert "Authorization" not in captured["headers"]


# ---------------------------------------------------------------------------
# MockJudgeClient
# ---------------------------------------------------------------------------


class TestMockJudgeClient:
    def test_default_returns_stub_response(self) -> None:
        client = MockJudgeClient()
        response = client.complete(JudgeRequest(prompt="anything"))
        assert response.ok is True
        assert "stub" in response.text

    def test_exact_match_response(self) -> None:
        client = MockJudgeClient({"hello": "world"})
        response = client.complete(JudgeRequest(prompt="hello"))
        assert response.text == "world"

    def test_longest_prefix_match(self) -> None:
        client = MockJudgeClient(
            {"safety:": "SHORT_RULE", "safety:gemma4:": "LONG_RULE"}
        )
        response = client.complete(JudgeRequest(prompt="safety:gemma4:please judge"))
        assert response.text == "LONG_RULE"

    def test_callable_receives_request(self) -> None:
        observed: list[JudgeRequest] = []

        def judge(req: JudgeRequest) -> str:
            observed.append(req)
            return f"graded:{req.prompt}"

        client = MockJudgeClient(callable=judge)
        response = client.complete(JudgeRequest(prompt="scene-1"))
        assert response.text == "graded:scene-1"
        assert len(observed) == 1

    def test_callable_can_return_full_response(self) -> None:
        def judge(_req: JudgeRequest) -> JudgeResponse:
            return JudgeResponse(ok=False, error="rate_limited")

        client = MockJudgeClient(callable=judge)
        response = client.complete(JudgeRequest(prompt="p"))
        assert response.ok is False
        assert response.error == "rate_limited"

    def test_records_calls_for_test_assertions(self) -> None:
        client = MockJudgeClient()
        client.complete(JudgeRequest(prompt="a"))
        client.complete(JudgeRequest(prompt="b"))
        calls = client.calls
        assert [r.prompt for r in calls] == ["a", "b"]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestBuildJudgeClient:
    def test_returns_mock_when_mock_responses_provided(self) -> None:
        client = build_judge_client(mock_responses={"a": "b"}, base_url="http://x", model="m")
        assert isinstance(client, MockJudgeClient)

    def test_returns_mock_when_base_url_empty(self) -> None:
        client = build_judge_client(base_url="", model="m")
        assert isinstance(client, MockJudgeClient)

    def test_returns_http_when_live_config_provided(self) -> None:
        client = build_judge_client(base_url="http://judge.local", api_key="k", model="m", role="safety")
        assert isinstance(client, HttpJudgeClient)
        assert client.role == "safety"


# ---------------------------------------------------------------------------
# JudgeResponse
# ---------------------------------------------------------------------------


class TestJudgeResponse:
    def test_to_dict_round_trips(self) -> None:
        r = JudgeResponse(ok=True, text="t", model="m", latency_ms=1.5, usage={"total_tokens": 3})
        d = r.to_dict()
        assert d["ok"] is True
        assert d["usage"]["total_tokens"] == 3
