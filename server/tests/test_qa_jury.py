"""Tests for ``server.tools.qa_jury``.

These cover the pure-Python parts of the jury: voter selection, score-bias
correction, and majority-vote aggregation.  Network-dependent adapter code
(Gemini Files API / Dashscope / Zhipu) is exercised only by the integration
smoke test below, which is skipped by default.

Run the fast unit tests with::

    cd server && poetry run pytest tests/test_qa_jury.py -v

Run the skipped smoke test explicitly with::

    cd server && poetry run pytest tests/test_qa_jury.py -v -m integration --run-integration
"""

from __future__ import annotations

import asyncio
import os

import pytest

from tools.qa_jury import (
    AUDIO_ONLY_CHECKS,
    DashscopeQwenVoter,
    FinalCut,
    GLMVoter,
    GeminiVoter,
    VoterCapabilities,
    VoterVerdict,
    aggregate,
    assign_voters,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _StubVoter:
    """Minimal stub satisfying the :class:`Voter` protocol for selection tests."""

    def __init__(self, capabilities: VoterCapabilities) -> None:
        self.capabilities = capabilities

    async def judge(self, artifact, prompt):  # pragma: no cover - not exercised
        raise NotImplementedError


def _caps(
    family: str,
    *,
    cannot_judge: frozenset[str] = frozenset(),
    score_bias: float = 0.0,
    model_name: str = "stub",
    native_audio: bool = True,
) -> VoterCapabilities:
    return VoterCapabilities(
        native_video=True,
        native_audio=native_audio,
        family=family,
        cannot_judge=cannot_judge,
        score_bias=score_bias,
        model_name=model_name,
        provider="stub",
    )


# ---------------------------------------------------------------------------
# assign_voters
# ---------------------------------------------------------------------------
def test_assign_voters_drops_ineligible_for_audio_checks() -> None:
    gemini = _StubVoter(_caps("gemini", model_name="gemini-3-pro-preview"))
    qwen = _StubVoter(
        _caps(
            "qwen",
            cannot_judge=AUDIO_ONLY_CHECKS,
            score_bias=2.0,
            model_name="qwen3-vl-plus",
            native_audio=False,
        )
    )
    glm = _StubVoter(
        _caps(
            "glm",
            cannot_judge=AUDIO_ONLY_CHECKS,
            model_name="glm-4.5v",
            native_audio=False,
        )
    )

    # An audio-only check must drop Qwen and GLM but keep Gemini.
    for audio_check in AUDIO_ONLY_CHECKS:
        assigned = assign_voters(audio_check, [gemini, qwen, glm])
        assert assigned == [gemini], (
            f"Audio-only check {audio_check!r} should leave only Gemini; "
            f"got {[v.capabilities.family for v in assigned]}"
        )

    # A visual check keeps all three.
    visual_assigned = assign_voters("composition", [gemini, qwen, glm])
    assert [v.capabilities.family for v in visual_assigned] == ["gemini", "qwen", "glm"]


def test_assign_voters_dedupes_by_family() -> None:
    gemini_pro = _StubVoter(_caps("gemini", model_name="gemini-3-pro-preview"))
    gemini_flash = _StubVoter(_caps("gemini", model_name="gemini-3-flash-preview"))
    qwen = _StubVoter(
        _caps("qwen", cannot_judge=AUDIO_ONLY_CHECKS, score_bias=2.0, native_audio=False)
    )

    # Two Gemini voters in: exactly one Gemini voter out.
    assigned = assign_voters("composition", [gemini_pro, gemini_flash, qwen])
    assert len(assigned) == 2
    families = [v.capabilities.family for v in assigned]
    assert families == ["gemini", "qwen"]
    # The first Gemini voter in input order wins.
    assert assigned[0].capabilities.model_name == "gemini-3-pro-preview"


# ---------------------------------------------------------------------------
# aggregate -- numeric with per-voter bias correction
# ---------------------------------------------------------------------------
def test_aggregate_applies_score_bias() -> None:
    """Qwen's +2.0 bias must be subtracted before the median is computed.

    Raw scores     : 3.5, 4.0, 7.2 (Qwen), 4.0
    Bias-corrected : 3.5, 4.0, 5.2,        4.0
    Sorted         : 3.5, 4.0, 4.0, 5.2
    Median         : (4.0 + 4.0) / 2 = 4.0
    """

    verdicts = [
        VoterVerdict(
            voter_model="gemini-3-pro-preview",
            voter_family="gemini",
            voter_score_bias=0.0,
            value=3.5,
        ),
        VoterVerdict(
            voter_model="glm-4.5v",
            voter_family="glm",
            voter_score_bias=0.0,
            value=4.0,
        ),
        VoterVerdict(
            voter_model="qwen3-vl-plus",
            voter_family="qwen",
            voter_score_bias=2.0,
            value=7.2,
        ),
        VoterVerdict(
            voter_model="gemini-3-flash-preview",
            voter_family="gemini-flash",  # distinct for the test
            voter_score_bias=0.0,
            value=4.0,
        ),
    ]

    jury = aggregate(
        verdicts,
        check_type="numeric",
        check_name="style_rating",
        artifact_id="final_cut_test",
    )

    assert jury.per_check_results["style_rating"] == pytest.approx(4.0)
    assert jury.artifact_id == "final_cut_test"
    # With four close-together values the overall should pass, not escalate.
    assert jury.overall == "pass"


def test_aggregate_majority_vote_tie_fails() -> None:
    """A 2-2 tie must resolve to ``fail`` (per jury spec), NOT escalate."""

    verdicts = [
        VoterVerdict(
            voter_model="gemini-3-pro-preview",
            voter_family="gemini",
            voter_score_bias=0.0,
            value=True,
        ),
        VoterVerdict(
            voter_model="qwen3-vl-plus",
            voter_family="qwen",
            voter_score_bias=2.0,
            value=True,
        ),
        VoterVerdict(
            voter_model="glm-4.5v",
            voter_family="glm",
            voter_score_bias=0.0,
            value=False,
        ),
        VoterVerdict(
            voter_model="gemini-3-flash-preview",
            voter_family="gemini-flash",
            voter_score_bias=0.0,
            value=False,
        ),
    ]

    jury = aggregate(
        verdicts,
        check_type="binary",
        check_name="composition_ok",
        artifact_id="scene_07",
    )

    assert jury.overall == "fail"
    assert jury.per_check_results["composition_ok"] is False
    assert jury.confidence == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Integration smoke test (skipped by default)
# ---------------------------------------------------------------------------
# Marked so CI never runs it automatically.  Invoke manually with
#     poetry run pytest -m integration --run-integration
_SMOKE_VIDEO_URL = (
    "https://f004.backblazeb2.com/file/cloudberry-documentary-v2/"
    "unknown_1776445689/output/final_documentary_v2.mp4"
)


@pytest.mark.integration
def test_voters_smoke_against_real_final_cut() -> None:
    """Smoke test: each voter rates the reference final cut on style 0-10.

    This test is skipped by default.  Invoke it explicitly with
    ``--run-integration`` and the following env vars set:

        * ``GOOGLE_API_KEY``          (Gemini)
        * ``DASHSCOPE_INTL_API_KEY``  (Qwen3-VL)
        * ``GLM_API_KEY``             (GLM-4.5V)
    """

    prompt = (
        "Rate the overall cinematic style of this documentary on a scale "
        "from 0 to 10. Respond with a single number only."
    )

    artifact = FinalCut(
        artifact_id="reference_final_cut",
        path="",
        url=_SMOKE_VIDEO_URL,
        full_narration="",
    )

    async def _run() -> list[VoterVerdict]:
        voters = []
        if os.environ.get("GOOGLE_API_KEY"):
            voters.append(GeminiVoter(model_name="gemini-3-flash-preview"))
        if os.environ.get("DASHSCOPE_INTL_API_KEY"):
            voters.append(DashscopeQwenVoter())
        if os.environ.get("GLM_API_KEY"):
            voters.append(GLMVoter())

        if not voters:
            pytest.skip(
                "No provider API keys available for the smoke test "
                "(set GOOGLE_API_KEY / DASHSCOPE_INTL_API_KEY / GLM_API_KEY)."
            )

        return await asyncio.gather(*(v.judge(artifact, prompt) for v in voters))

    # GeminiVoter needs a local path for Files API upload; for the smoke
    # test we only exercise the URL-based voters.
    verdicts = asyncio.run(_run())
    assert verdicts, "At least one voter must produce a verdict."
    # All returned verdicts should be real strings or disabled-with-error.
    for verdict in verdicts:
        assert verdict.disabled or isinstance(verdict.value, str)
