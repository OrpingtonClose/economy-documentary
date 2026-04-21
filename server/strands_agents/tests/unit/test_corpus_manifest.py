"""Unit tests for the corpus manifest types and loader.

Hermetic: no network, no B2, no GPU.  Validates schema parsing,
lookup tables, dedup, and error paths.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strands_agents.corpus.manifest import (
    CorpusArtifact,
    CorpusManifest,
    load_manifest,
)

VALID_SHA = "a" * 64


def _artifact(**overrides: object) -> CorpusArtifact:
    base: dict[str, object] = {
        "key": "scenario.golden.example",
        "component": "01-scenario-agent",
        "role": "golden",
        "content_type": "scenario_json",
        "storage": "seed",
        "sha256": VALID_SHA,
        "size_bytes": 42,
        "seed_path": "example.json",
    }
    base.update(overrides)
    return CorpusArtifact(**base)  # type: ignore[arg-type]


class TestCorpusArtifactValidation:
    def test_seed_without_seed_path_raises(self) -> None:
        with pytest.raises(ValueError, match="no seed_path"):
            _artifact(seed_path=None)

    def test_b2_without_b2_key_raises(self) -> None:
        with pytest.raises(ValueError, match="no b2_key"):
            _artifact(storage="b2", seed_path=None, b2_key=None)

    def test_b2_with_b2_key_ok(self) -> None:
        art = _artifact(storage="b2", seed_path=None, b2_key="corpus/foo.bin")
        assert art.b2_key == "corpus/foo.bin"

    def test_invalid_sha_length_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid sha256"):
            _artifact(sha256="abc")

    def test_zero_size_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid size_bytes"):
            _artifact(size_bytes=0)

    def test_negative_size_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid size_bytes"):
            _artifact(size_bytes=-1)


class TestCorpusManifestLookups:
    def test_by_key(self) -> None:
        a = _artifact(key="scenario.golden.a")
        b = _artifact(
            key="scenario.adversarial.b",
            role="adversarial",
            seed_path="b.json",
        )
        manifest = CorpusManifest(version=1, artifacts=(a, b))
        assert manifest.get("scenario.golden.a") is a
        assert manifest.get("scenario.adversarial.b") is b

    def test_missing_key_raises(self) -> None:
        manifest = CorpusManifest(version=1, artifacts=(_artifact(),))
        with pytest.raises(KeyError, match="no artifact with key"):
            manifest.get("does-not-exist")

    def test_duplicate_key_raises(self) -> None:
        a = _artifact(key="dup")
        b = _artifact(key="dup", seed_path="b.json")
        with pytest.raises(ValueError, match="duplicate key"):
            CorpusManifest(version=1, artifacts=(a, b))

    def test_for_component(self) -> None:
        a = _artifact(key="x.a", component="01-scenario-agent")
        b = _artifact(
            key="x.b",
            component="02-timing-evaluator",
            content_type="timing_report_json",
            seed_path="b.json",
        )
        manifest = CorpusManifest(version=1, artifacts=(a, b))
        assert manifest.for_component("01-scenario-agent") == (a,)
        assert manifest.for_component("02-timing-evaluator") == (b,)
        assert manifest.for_component("14-pipeline-graph") == ()

    def test_by_role(self) -> None:
        a = _artifact(key="x.a", role="golden")
        b = _artifact(key="x.b", role="adversarial", seed_path="b.json")
        c = _artifact(key="x.c", role="ambiguous", seed_path="c.json")
        manifest = CorpusManifest(version=1, artifacts=(a, b, c))
        assert manifest.by_role("01-scenario-agent", "golden") == (a,)
        assert manifest.by_role("01-scenario-agent", "adversarial") == (b,)
        assert manifest.by_role("01-scenario-agent", "ambiguous") == (c,)


class TestLoadManifest:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            load_manifest(tmp_path / "nope.json")

    def test_missing_version_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "m.json"
        p.write_text(json.dumps({"artifacts": []}))
        with pytest.raises(ValueError, match="missing integer <version>"):
            load_manifest(p)

    def test_missing_artifacts_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "m.json"
        p.write_text(json.dumps({"version": 1}))
        with pytest.raises(ValueError, match="missing list <artifacts>"):
            load_manifest(p)

    def test_non_object_artifact_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "m.json"
        p.write_text(json.dumps({"version": 1, "artifacts": ["not-a-dict"]}))
        with pytest.raises(ValueError, match="not an object"):
            load_manifest(p)

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "m.json"
        p.write_text(json.dumps({
            "version": 1,
            "artifacts": [{"key": "partial"}],
        }))
        with pytest.raises(ValueError, match="missing fields"):
            load_manifest(p)

    def test_roundtrip(self, tmp_path: Path) -> None:
        p = tmp_path / "m.json"
        p.write_text(json.dumps({
            "version": 1,
            "artifacts": [
                {
                    "key": "x",
                    "component": "01-scenario-agent",
                    "role": "golden",
                    "content_type": "scenario_json",
                    "storage": "seed",
                    "seed_path": "x.json",
                    "sha256": VALID_SHA,
                    "size_bytes": 10,
                    "expected_verdict": "accept",
                },
            ],
        }))
        manifest = load_manifest(p)
        assert manifest.version == 1
        artifact = manifest.get("x")
        assert artifact.expected_verdict == "accept"


class TestDefaultManifest:
    """Smoke-test the committed default manifest loads and is well-formed."""

    def test_default_manifest_loads(self) -> None:
        from strands_agents.corpus import load_default_manifest

        manifest = load_default_manifest()
        assert manifest.version >= 1
        assert len(manifest.artifacts) > 0

    def test_default_manifest_keys_unique(self) -> None:
        from strands_agents.corpus import load_default_manifest

        manifest = load_default_manifest()
        keys = [a.key for a in manifest.artifacts]
        assert len(keys) == len(set(keys))

    def test_default_manifest_covers_multiple_components(self) -> None:
        from strands_agents.corpus import load_default_manifest

        manifest = load_default_manifest()
        components = {a.component for a in manifest.artifacts}
        # Corpus v1 seeds at least three components so per-component
        # eval suites have something to chew on from day one.
        assert len(components) >= 3

    def test_default_manifest_covers_all_roles(self) -> None:
        from strands_agents.corpus import load_default_manifest

        manifest = load_default_manifest()
        roles = {a.role for a in manifest.artifacts}
        # Every role class should have at least one seed — otherwise
        # the eval suites don't exercise the judgment dimension that
        # role measures.
        assert {"golden", "adversarial", "ambiguous"} <= roles
