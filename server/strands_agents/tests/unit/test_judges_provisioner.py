"""Tests for the judge fleet provisioner glue."""

from __future__ import annotations

from strands_agents.judges.models import (
    GEMMA4_ABLITERATED,
    QWEN35_OMNI,
    VIDEO_SALMONN_2_72B,
)
from strands_agents.judges.provisioner import build_judge_worker_spec, describe_judge_fleet


class TestBuildJudgeWorkerSpec:
    def test_translates_catalog_entry_into_worker_spec(self) -> None:
        spec = build_judge_worker_spec(
            GEMMA4_ABLITERATED,
            local_port=8881,
            remote_port=8880,
            max_price=6.0,
        )
        assert spec.role == "judge_gemma4_abliterated"
        assert spec.env_var == "JUDGE_GEMMA4_ABLITERATED_URL"
        assert spec.local_port == 8881
        assert spec.remote_port == 8880
        assert spec.capability == "judge_safety"
        assert spec.min_vram_gb == GEMMA4_ABLITERATED.runtime_vram_gb
        assert spec.disk_gb == GEMMA4_ABLITERATED.disk_gb
        assert spec.max_price == 6.0
        assert spec.judge_mode == "gemma4_abliterated"
        assert spec.model_key == "gemma4_abliterated"

    def test_vram_floor_override_is_respected(self) -> None:
        spec = build_judge_worker_spec(
            QWEN35_OMNI,
            local_port=8882,
            min_vram_gb=48,
        )
        assert spec.min_vram_gb == 48

    def test_disk_floor_override_is_respected(self) -> None:
        spec = build_judge_worker_spec(
            VIDEO_SALMONN_2_72B,
            local_port=8883,
            min_disk_gb=256,
        )
        assert spec.min_disk_gb == 256

    def test_each_role_yields_distinct_env_var(self) -> None:
        env_vars = {
            build_judge_worker_spec(s, local_port=8880 + i).env_var
            for i, s in enumerate(
                [GEMMA4_ABLITERATED, QWEN35_OMNI, VIDEO_SALMONN_2_72B]
            )
        }
        assert len(env_vars) == 3


class TestDescribeJudgeFleet:
    def test_returns_ordered_summary(self) -> None:
        fleet = [GEMMA4_ABLITERATED, QWEN35_OMNI]
        summary = describe_judge_fleet(fleet)
        assert [s["key"] for s in summary] == [
            "gemma4_abliterated",
            "qwen35_omni",
        ]

    def test_summary_fields_are_json_safe(self) -> None:
        import json

        summary = describe_judge_fleet(list({GEMMA4_ABLITERATED, QWEN35_OMNI, VIDEO_SALMONN_2_72B}))
        # Would raise if any non-serialisable types slipped in.
        json.dumps(summary)

    def test_summary_carries_all_required_fields(self) -> None:
        summary = describe_judge_fleet([GEMMA4_ABLITERATED])
        entry = summary[0]
        required = {
            "key",
            "display_name",
            "role",
            "runtime_vram_gb",
            "disk_gb",
            "weights_gb",
            "hf_source",
            "b2_prefix",
        }
        assert required <= set(entry)
