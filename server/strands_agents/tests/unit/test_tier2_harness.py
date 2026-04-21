"""Unit tests for the Tier-2 harness helpers.

These are pure-Python checks of the harness's structural assertions and
verdict normalisation.  The per-component suites under ``tier2/`` cover
the integration with the real corpus.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from strands_agents.corpus.fetcher import CorpusFetcher, SeedBackend
from strands_agents.corpus.manifest import CorpusArtifact, CorpusManifest
from strands_agents.tier2 import harness
from strands_agents.tier2.rubrics import RUBRICS, get_rubric


def _make_artifact(
    seeds_dir: Path,
    *,
    key: str = "x.golden.sample",
    component: str = "01-scenario-agent",
    role: str = "golden",
    content_type: str = "scenario_json",
    body: bytes = b'{"ok": true}',
    expected_verdict: str = "accept",
) -> CorpusArtifact:
    seed_path = seeds_dir / f"{key}.bin"
    seed_path.write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    return CorpusArtifact(
        key=key,
        component=component,  # type: ignore[arg-type]
        role=role,  # type: ignore[arg-type]
        content_type=content_type,  # type: ignore[arg-type]
        storage="seed",
        sha256=sha,
        size_bytes=len(body),
        seed_path=seed_path.name,
        expected_verdict=expected_verdict,
    )


def _fetcher(seeds_dir: Path, cache_dir: Path) -> CorpusFetcher:
    return CorpusFetcher(
        cache_root=cache_dir,
        seed_backend=SeedBackend(seed_root=seeds_dir),
    )


class TestAssertHermetic:
    def test_happy_path(self, tmp_path: Path) -> None:
        seeds = tmp_path / "seeds"
        seeds.mkdir()
        cache = tmp_path / "cache"
        artifact = _make_artifact(seeds)
        harness.assert_hermetic(artifact, _fetcher(seeds, cache))

    def test_missing_expected_verdict_fails(self, tmp_path: Path) -> None:
        seeds = tmp_path / "seeds"
        seeds.mkdir()
        artifact = _make_artifact(seeds, expected_verdict="")
        with pytest.raises(AssertionError, match="no expected_verdict"):
            harness.assert_hermetic(artifact, _fetcher(seeds, tmp_path / "cache"))


class TestAssertComponentSeeded:
    def test_pair_present(self, tmp_path: Path) -> None:
        seeds = tmp_path / "seeds"
        seeds.mkdir()
        manifest = CorpusManifest(
            version=1,
            artifacts=(
                _make_artifact(seeds, key="x.golden", role="golden"),
                _make_artifact(seeds, key="x.adv", role="adversarial"),
            ),
        )
        harness.assert_component_seeded(manifest, "01-scenario-agent")

    def test_missing_role_fails(self, tmp_path: Path) -> None:
        seeds = tmp_path / "seeds"
        seeds.mkdir()
        manifest = CorpusManifest(
            version=1,
            artifacts=(_make_artifact(seeds, role="golden"),),
        )
        with pytest.raises(AssertionError, match="adversarial"):
            harness.assert_component_seeded(manifest, "01-scenario-agent")


class TestVerdictNormalisation:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("pass", "accept"),
            ("PASS", "accept"),
            ("  approve  ", "accept"),
            ("deny", "reject"),
            ("refine_needed", "refine"),
            ("borderline", "ambiguous"),
            ("custom_label", "custom_label"),
        ],
    )
    def test_aliases_collapsed(self, raw: str, expected: str) -> None:
        assert harness.normalise_verdict(raw) == expected

    def test_matches_with_aliases(self) -> None:
        assert harness.verdict_matches("PASS", "accept")
        assert harness.verdict_matches("rejected", "reject")
        assert not harness.verdict_matches("accept", "reject")


class TestAssertLiveVerdict:
    def test_match(self, tmp_path: Path) -> None:
        seeds = tmp_path / "seeds"
        seeds.mkdir()
        artifact = _make_artifact(seeds, expected_verdict="accept")
        harness.assert_live_verdict(artifact, "pass")  # alias matches

    def test_abstain_allowed(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        seeds = tmp_path / "seeds"
        seeds.mkdir()
        artifact = _make_artifact(seeds, expected_verdict="accept")
        harness.assert_live_verdict(artifact, "abstain")
        harness.assert_live_verdict(artifact, "")

    def test_mismatch_fails(self, tmp_path: Path) -> None:
        seeds = tmp_path / "seeds"
        seeds.mkdir()
        artifact = _make_artifact(seeds, expected_verdict="accept")
        with pytest.raises(AssertionError, match="verdict mismatch"):
            harness.assert_live_verdict(artifact, "reject")


class TestRubrics:
    def test_every_component_has_rubric(self) -> None:
        components = [
            "01-scenario-agent", "02-timing-evaluator", "03-scenario-refiner",
            "04-audio-agent", "05-timing-loop", "06-content-analyst",
            "07-visual-concepter", "08-coherence-evaluator", "09-visual-loop",
            "10-production-supervisor", "11-assembly-agent", "12-recovery-agents",
            "13-escalation-supervisor", "14-pipeline-graph", "15-approval-gates",
        ]
        for c in components:
            rubric = get_rubric(c)
            assert rubric.component == c
            assert rubric.prompt
            assert rubric.allowed_verdicts
            assert rubric.judge_role in {"safety", "av_primary", "av_tiebreaker"}

    def test_rubric_not_found_raises(self) -> None:
        with pytest.raises(KeyError):
            get_rubric("99-does-not-exist")

    def test_rubrics_are_frozen(self) -> None:
        # Dataclass frozen=True enforces immutability; confirm the catalog is a Mapping.
        assert len(RUBRICS) == 15
        with pytest.raises(AttributeError):
            RUBRICS["01-scenario-agent"].revision = 99  # type: ignore[misc]


class TestLoadTier2Cases:
    def test_returns_cases_for_component(self, tmp_path: Path) -> None:
        seeds = tmp_path / "seeds"
        seeds.mkdir()
        manifest = CorpusManifest(
            version=1,
            artifacts=(
                _make_artifact(seeds, key="a", component="01-scenario-agent"),
                _make_artifact(seeds, key="b", component="02-timing-evaluator"),
            ),
        )
        cases = harness.load_tier2_cases(manifest, "01-scenario-agent")
        assert len(cases) == 1
        assert cases[0].artifact.key == "a"
        assert cases[0].mode is harness.Tier2Mode.HERMETIC

    def test_empty_when_component_absent(self, tmp_path: Path) -> None:
        manifest = CorpusManifest(version=1, artifacts=())
        cases = harness.load_tier2_cases(manifest, "14-pipeline-graph")
        assert cases == ()
