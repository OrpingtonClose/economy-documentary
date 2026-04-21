"""Tests for the judge model catalog."""

from __future__ import annotations

import pytest

from strands_agents.judges.models import (
    GEMMA4_ABLITERATED,
    JUDGE_CATALOG,
    QWEN35_OMNI,
    VIDEO_SALMONN_2_72B,
    JudgeModelSpec,
)


class TestJudgeCatalog:
    def test_catalog_keys_are_unique_and_match_specs(self) -> None:
        for key, spec in JUDGE_CATALOG.items():
            assert spec.key == key

    def test_catalog_contains_all_three_seats(self) -> None:
        assert set(JUDGE_CATALOG) == {
            "gemma4_abliterated",
            "qwen35_omni",
            "video_salmonn_2_72b",
        }

    def test_roles_cover_the_three_ensemble_seats(self) -> None:
        roles = {spec.role for spec in JUDGE_CATALOG.values()}
        assert roles == {"safety", "av_primary", "av_tiebreaker"}

    def test_abliterated_gemma_has_no_hf_source(self) -> None:
        # Abliterated weights must never be pulled from the public hub.
        assert GEMMA4_ABLITERATED.hf_source == ""
        assert GEMMA4_ABLITERATED.b2_prefix

    def test_non_abliterated_judges_have_hf_fallback(self) -> None:
        for spec in (QWEN35_OMNI, VIDEO_SALMONN_2_72B):
            assert spec.hf_source
            assert spec.b2_prefix

    def test_specs_are_frozen(self) -> None:
        with pytest.raises(Exception):
            GEMMA4_ABLITERATED.runtime_vram_gb = 0  # type: ignore[misc]

    @pytest.mark.parametrize("spec", JUDGE_CATALOG.values())
    def test_every_spec_has_plausible_hardware(self, spec: JudgeModelSpec) -> None:
        assert spec.runtime_vram_gb >= spec.weights_gb, (
            f"{spec.key}: runtime VRAM must cover weights with headroom"
        )
        assert spec.disk_gb >= spec.weights_gb * 2, (
            f"{spec.key}: disk must fit weights + scratch"
        )
        assert spec.params_billions > 0
        assert spec.dtype in {"bf16", "fp16", "fp32", "fp8", "int8", "int4"}

    @pytest.mark.parametrize("spec", JUDGE_CATALOG.values())
    def test_every_spec_lists_its_checkpoint_files(self, spec: JudgeModelSpec) -> None:
        assert spec.checkpoint_files
        assert "config.json" in spec.checkpoint_files
