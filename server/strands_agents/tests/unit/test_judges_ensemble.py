"""Unit tests for :mod:`strands_agents.judges.ensemble`.

Every test runs against :class:`MockJudgeClient`; no network calls, no
GPU, no flakiness.  The suite focuses on the four behaviours the
ensemble is responsible for:

1. Role routing: the right client is invoked for each ``query_*``.
2. Verdict parsing: JSON-in-prose, pure-prose, malformed, and
   out-of-range scores all produce sensible verdicts.
3. Aggregation: majority-vote labels, mean scores, agreement flag.
4. Fallback policy: fires only when locals fail or disagree, never
   when safety calls abstain.
"""

from __future__ import annotations

import pytest

from strands_agents.judges.client import (
    JudgeClient,
    JudgeRequest,
    JudgeResponse,
    MockJudgeClient,
)
from strands_agents.judges.ensemble import (
    EnsembleVerdict,
    JudgeEnsemble,
    JudgeVerdict,
    _majority_vote,
    _mean_or_none,
    _parse_judge_text,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _req(prompt: str = "grade this") -> JudgeRequest:
    return JudgeRequest(prompt=prompt)


def _json_text(score: float, verdict: str = "pass", reasoning: str = "ok") -> str:
    return f'{{"score": {score}, "verdict": "{verdict}", "reasoning": "{reasoning}"}}'


class _RaisingClient(JudgeClient):
    """Client whose ``complete`` always raises — covers the defensive branch."""

    role = "test"

    def complete(self, request: JudgeRequest) -> JudgeResponse:
        raise RuntimeError("boom")

    def close(self) -> None:
        pass


class _ErroringClient(JudgeClient):
    """Client that returns ``ok=False`` — covers the non-2xx path."""

    role = "test"

    def __init__(self, model: str = "err-judge") -> None:
        self._model = model

    def complete(self, request: JudgeRequest) -> JudgeResponse:
        return JudgeResponse(ok=False, error="upstream_500", model=self._model)

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class TestParseJudgeText:
    def test_pure_json(self) -> None:
        score, verdict, reasoning = _parse_judge_text(_json_text(0.82))
        assert score == pytest.approx(0.82)
        assert verdict == "pass"
        assert reasoning == "ok"

    def test_json_embedded_in_prose(self) -> None:
        text = 'The scene is fine. {"score": 0.5, "verdict": "fail"} End.'
        score, verdict, reasoning = _parse_judge_text(text)
        assert score == 0.5
        assert verdict == "fail"

    def test_prose_only_returns_none_score(self) -> None:
        score, verdict, reasoning = _parse_judge_text("looks good to me")
        assert score is None
        assert verdict == ""
        assert reasoning == "looks good to me"

    def test_score_clamped_low(self) -> None:
        score, _, _ = _parse_judge_text('{"score": -0.7}')
        assert score == 0.0

    def test_score_clamped_high(self) -> None:
        score, _, _ = _parse_judge_text('{"score": 5.0}')
        assert score == 1.0

    def test_score_non_numeric(self) -> None:
        score, verdict, _ = _parse_judge_text('{"score": "high", "verdict": "pass"}')
        assert score is None
        assert verdict == "pass"

    def test_score_bool_not_treated_as_number(self) -> None:
        # Python's True/False are int subclasses — explicitly excluded.
        score, _, _ = _parse_judge_text('{"score": true}')
        assert score is None

    def test_verdict_normalised_to_lowercase(self) -> None:
        _, verdict, _ = _parse_judge_text('{"verdict": "PASS"}')
        assert verdict == "pass"

    def test_malformed_json_returns_none(self) -> None:
        score, verdict, reasoning = _parse_judge_text("{not json")
        assert score is None
        assert verdict == ""

    def test_missing_verdict_defaults_empty(self) -> None:
        _, verdict, reasoning = _parse_judge_text('{"score": 0.5}')
        assert verdict == ""
        assert reasoning == ""

    def test_nested_dict_parses_top_level_only(self) -> None:
        # The regex matches the first non-nested block; with nesting it
        # falls back to parsing the full stripped text which does succeed.
        text = '{"score": 0.7, "verdict": "pass", "meta": {"x": 1}}'
        score, verdict, _ = _parse_judge_text(text)
        assert score == pytest.approx(0.7)
        assert verdict == "pass"


# ---------------------------------------------------------------------------
# Voting helpers
# ---------------------------------------------------------------------------


class TestVotingHelpers:
    def test_majority_vote_unique_winner(self) -> None:
        assert _majority_vote(["pass", "pass", "fail"]) == "pass"

    def test_majority_vote_tie_returns_empty(self) -> None:
        assert _majority_vote(["pass", "fail"]) == ""

    def test_majority_vote_empty_input(self) -> None:
        assert _majority_vote([]) == ""

    def test_majority_vote_all_unique(self) -> None:
        assert _majority_vote(["a", "b", "c"]) == ""

    def test_mean_or_none_empty(self) -> None:
        assert _mean_or_none([]) is None

    def test_mean_or_none_basic(self) -> None:
        assert _mean_or_none([0.2, 0.4, 0.9]) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Ensemble — construction & introspection
# ---------------------------------------------------------------------------


class TestJudgeEnsembleInit:
    def test_rejects_unknown_role(self) -> None:
        with pytest.raises(ValueError, match="unknown judge role"):
            JudgeEnsemble({"nonsense": MockJudgeClient()})  # type: ignore[dict-item]

    def test_rejects_bad_threshold(self) -> None:
        with pytest.raises(ValueError, match="disagreement_threshold"):
            JudgeEnsemble({}, disagreement_threshold=1.5)

    def test_rejects_negative_threshold(self) -> None:
        with pytest.raises(ValueError, match="disagreement_threshold"):
            JudgeEnsemble({}, disagreement_threshold=-0.1)

    def test_has_role(self) -> None:
        ensemble = JudgeEnsemble({"safety": MockJudgeClient()})
        assert ensemble.has_role("safety")
        assert not ensemble.has_role("av_primary")

    def test_fallback_flag(self) -> None:
        ensemble_no_fb = JudgeEnsemble({})
        ensemble_fb = JudgeEnsemble({}, fallback=MockJudgeClient())
        assert not ensemble_no_fb.fallback_available
        assert ensemble_fb.fallback_available

    def test_close_idempotent(self) -> None:
        safety = MockJudgeClient()
        fallback = MockJudgeClient()
        ensemble = JudgeEnsemble({"safety": safety}, fallback=fallback)
        ensemble.close()
        ensemble.close()  # second call must not raise


# ---------------------------------------------------------------------------
# query_safety
# ---------------------------------------------------------------------------


class TestQuerySafety:
    def test_invokes_safety_client(self) -> None:
        safety = MockJudgeClient(responses={"grade this": _json_text(0.9, "pass")})
        ensemble = JudgeEnsemble({"safety": safety})

        result = ensemble.query_safety(_req())

        assert result.score == pytest.approx(0.9)
        assert result.verdict == "pass"
        assert result.used_fallback is False
        assert len(safety.calls) == 1

    def test_missing_safety_returns_abstention(self) -> None:
        ensemble = JudgeEnsemble({})
        result = ensemble.query_safety(_req())
        assert result.score is None
        assert result.ok is False
        assert result.verdicts == ()
        assert result.used_fallback is False

    def test_safety_does_not_fallback_even_when_fallback_registered(self) -> None:
        # Critical policy: safety evals must NOT silently route to a
        # refusal-trained proprietary judge.  Missing safety = abstain.
        fallback = MockJudgeClient(responses={"grade this": _json_text(1.0, "pass")})
        ensemble = JudgeEnsemble({}, fallback=fallback)
        result = ensemble.query_safety(_req())
        assert result.used_fallback is False
        assert result.score is None
        assert len(fallback.calls) == 0

    def test_safety_error_propagates_as_unok(self) -> None:
        ensemble = JudgeEnsemble({"safety": _ErroringClient("gemma4-test")})
        result = ensemble.query_safety(_req())
        assert result.ok is False
        assert result.verdicts[0].error == "upstream_500"
        assert result.verdicts[0].model == "gemma4-test"
        assert result.used_fallback is False


# ---------------------------------------------------------------------------
# query_av — both local judges called together
# ---------------------------------------------------------------------------


class TestQueryAV:
    def test_both_judges_called_on_agreement(self) -> None:
        primary = MockJudgeClient(
            responses={"grade this": _json_text(0.8, "pass")},
            model="qwen",
        )
        tiebreaker = MockJudgeClient(
            responses={"grade this": _json_text(0.82, "pass")},
            model="salmonn",
        )
        ensemble = JudgeEnsemble(
            {"av_primary": primary, "av_tiebreaker": tiebreaker}
        )

        result = ensemble.query_av(_req())

        assert len(primary.calls) == 1
        assert len(tiebreaker.calls) == 1
        assert result.score == pytest.approx(0.81)
        assert result.verdict == "pass"
        assert result.agreed is True
        assert result.used_fallback is False

    def test_primary_only_when_tiebreaker_absent(self) -> None:
        primary = MockJudgeClient(responses={"grade this": _json_text(0.5, "fail")})
        ensemble = JudgeEnsemble({"av_primary": primary})
        result = ensemble.query_av(_req())
        assert result.score == 0.5
        assert result.verdict == "fail"
        assert len(result.verdicts) == 1

    def test_empty_ensemble_returns_abstention(self) -> None:
        ensemble = JudgeEnsemble({})
        result = ensemble.query_av(_req())
        assert result.score is None
        assert result.verdicts == ()
        assert result.used_fallback is False

    def test_fallback_fires_when_all_locals_fail(self) -> None:
        primary = _ErroringClient("qwen")
        tiebreaker = _ErroringClient("salmonn")
        fallback = MockJudgeClient(
            responses={"grade this": _json_text(0.6, "pass")},
            model="claude",
        )
        ensemble = JudgeEnsemble(
            {"av_primary": primary, "av_tiebreaker": tiebreaker},
            fallback=fallback,
        )

        result = ensemble.query_av(_req())

        assert result.used_fallback is True
        assert result.score == pytest.approx(0.6)
        assert len(fallback.calls) == 1
        assert len(result.verdicts) == 3
        assert result.verdicts[-1].model == "claude"
        assert result.verdicts[-1].role == "fallback"

    def test_fallback_fires_on_disagreement(self) -> None:
        primary = MockJudgeClient(
            responses={"grade this": _json_text(0.2, "fail")},
            model="qwen",
        )
        tiebreaker = MockJudgeClient(
            responses={"grade this": _json_text(0.9, "pass")},
            model="salmonn",
        )
        fallback = MockJudgeClient(
            responses={"grade this": _json_text(0.5, "pass")},
            model="claude",
        )
        ensemble = JudgeEnsemble(
            {"av_primary": primary, "av_tiebreaker": tiebreaker},
            fallback=fallback,
        )

        result = ensemble.query_av(_req())

        assert result.used_fallback is True
        # 0.2 + 0.9 + 0.5 / 3 ≈ 0.533
        assert result.score == pytest.approx((0.2 + 0.9 + 0.5) / 3)
        assert result.agreed is False

    def test_no_fallback_fires_when_disagreement_below_threshold(self) -> None:
        primary = MockJudgeClient(
            responses={"grade this": _json_text(0.4, "pass")},
            model="qwen",
        )
        tiebreaker = MockJudgeClient(
            responses={"grade this": _json_text(0.6, "pass")},
            model="salmonn",
        )
        fallback = MockJudgeClient(
            responses={"grade this": _json_text(0.99, "pass")},
            model="claude",
        )
        # Spread is 0.2; default threshold 0.3 — no fallback.
        ensemble = JudgeEnsemble(
            {"av_primary": primary, "av_tiebreaker": tiebreaker},
            fallback=fallback,
        )

        result = ensemble.query_av(_req())

        assert result.used_fallback is False
        assert len(fallback.calls) == 0
        assert result.score == pytest.approx(0.5)

    def test_custom_disagreement_threshold_tightens_policy(self) -> None:
        primary = MockJudgeClient(responses={"grade this": _json_text(0.4)})
        tiebreaker = MockJudgeClient(responses={"grade this": _json_text(0.6)})
        fallback = MockJudgeClient(responses={"grade this": _json_text(0.5)})
        # Tight threshold — 0.2 spread now exceeds 0.1 tolerance.
        ensemble = JudgeEnsemble(
            {"av_primary": primary, "av_tiebreaker": tiebreaker},
            fallback=fallback,
            disagreement_threshold=0.1,
        )
        result = ensemble.query_av(_req())
        assert result.used_fallback is True

    def test_fallback_fires_when_no_parseable_score(self) -> None:
        primary = MockJudgeClient(
            responses={"grade this": "just prose"},
            model="qwen",
        )
        fallback = MockJudgeClient(
            responses={"grade this": _json_text(0.7, "pass")},
            model="gemini",
        )
        ensemble = JudgeEnsemble({"av_primary": primary}, fallback=fallback)
        result = ensemble.query_av(_req())
        assert result.used_fallback is True
        assert result.score == 0.7

    def test_disagreement_with_no_fallback_registered_is_tolerated(self) -> None:
        primary = MockJudgeClient(responses={"grade this": _json_text(0.1, "fail")})
        tiebreaker = MockJudgeClient(responses={"grade this": _json_text(0.9, "pass")})
        ensemble = JudgeEnsemble(
            {"av_primary": primary, "av_tiebreaker": tiebreaker}
        )
        result = ensemble.query_av(_req())
        assert result.used_fallback is False
        assert result.agreed is False
        assert result.score == pytest.approx(0.5)
        # No majority — verdict strings disagree.
        assert result.verdict == ""


# ---------------------------------------------------------------------------
# query_av_primary / query_tiebreaker
# ---------------------------------------------------------------------------


class TestQueryAvPrimary:
    def test_only_primary_called(self) -> None:
        primary = MockJudgeClient(responses={"grade this": _json_text(0.7)})
        tiebreaker = MockJudgeClient(responses={"grade this": _json_text(0.1)})
        ensemble = JudgeEnsemble(
            {"av_primary": primary, "av_tiebreaker": tiebreaker}
        )
        result = ensemble.query_av_primary(_req())
        assert len(primary.calls) == 1
        assert len(tiebreaker.calls) == 0
        assert result.score == pytest.approx(0.7)

    def test_fallback_when_primary_errors(self) -> None:
        fallback = MockJudgeClient(responses={"grade this": _json_text(0.3)})
        ensemble = JudgeEnsemble(
            {"av_primary": _ErroringClient()},
            fallback=fallback,
        )
        result = ensemble.query_av_primary(_req())
        assert result.used_fallback is True
        assert result.score == pytest.approx(0.3)


class TestQueryTiebreaker:
    def test_invokes_tiebreaker_only(self) -> None:
        primary = MockJudgeClient(responses={"grade this": _json_text(0.1)})
        tiebreaker = MockJudgeClient(responses={"grade this": _json_text(0.8)})
        ensemble = JudgeEnsemble(
            {"av_primary": primary, "av_tiebreaker": tiebreaker}
        )
        result = ensemble.query_tiebreaker(_req())
        assert len(primary.calls) == 0
        assert len(tiebreaker.calls) == 1
        assert result.score == pytest.approx(0.8)
        assert result.used_fallback is False

    def test_missing_tiebreaker_returns_abstention(self) -> None:
        ensemble = JudgeEnsemble({"av_primary": MockJudgeClient()})
        result = ensemble.query_tiebreaker(_req())
        assert result.score is None
        assert result.verdicts == ()


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


class TestRobustness:
    def test_exception_from_client_becomes_unok_verdict(self) -> None:
        ensemble = JudgeEnsemble({"av_primary": _RaisingClient()})
        result = ensemble.query_av_primary(_req())
        assert result.ok is False
        assert result.verdicts[0].ok is False
        assert "boom" in result.verdicts[0].error

    def test_ensemble_verdict_ok_property(self) -> None:
        v = EnsembleVerdict(score=0.5, verdict="pass", agreed=True, used_fallback=False)
        assert v.ok is True
        unok = EnsembleVerdict(score=None, verdict="", agreed=True, used_fallback=False)
        assert unok.ok is False

    def test_judge_verdict_defaults(self) -> None:
        v = JudgeVerdict(model="m", role="av_primary", ok=True)
        assert v.score is None
        assert v.verdict == ""
        assert v.reasoning == ""

    def test_agreement_flag_with_single_judge(self) -> None:
        # Single judge trivially agrees with itself.
        ensemble = JudgeEnsemble(
            {"av_primary": MockJudgeClient(responses={"grade this": _json_text(0.5)})}
        )
        result = ensemble.query_av(_req())
        assert result.agreed is True


# ---------------------------------------------------------------------------
# Public-API smoke — ensure ensemble module is reachable via package
# ---------------------------------------------------------------------------


def test_ensemble_exposed_from_judges_package() -> None:
    from strands_agents.judges import EnsembleVerdict as ExportedEnsembleVerdict
    from strands_agents.judges import JudgeEnsemble as ExportedEnsemble
    from strands_agents.judges import JudgeVerdict as ExportedJudgeVerdict

    assert ExportedEnsemble is JudgeEnsemble
    assert ExportedEnsembleVerdict is EnsembleVerdict
    assert ExportedJudgeVerdict is JudgeVerdict
