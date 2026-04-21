"""Unit tests for the corpus fetcher + backends.

Hermetic: seed backend reads committed files, mock backend serves
in-memory bytes, B2 backend is never instantiated.  No network, no GPU.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from strands_agents.corpus.fetcher import (
    CorpusFetcher,
    MockBackend,
    SeedBackend,
    build_default_fetcher,
)
from strands_agents.corpus.manifest import CorpusArtifact


def _mkartifact(
    *,
    storage: str = "seed",
    data: bytes = b"hello",
    key: str = "t.artifact",
    seed_path: str | None = "f.bin",
    b2_key: str | None = None,
) -> CorpusArtifact:
    sha = hashlib.sha256(data).hexdigest()
    return CorpusArtifact(
        key=key,
        component="01-scenario-agent",
        role="golden",
        content_type="scenario_json",
        storage=storage,  # type: ignore[arg-type]
        sha256=sha,
        size_bytes=len(data),
        seed_path=seed_path,
        b2_key=b2_key,
    )


class TestSeedBackend:
    def test_reads_committed_seed(self, tmp_path: Path) -> None:
        (tmp_path / "x.json").write_bytes(b'{"ok": true}')
        backend = SeedBackend(seed_root=tmp_path)
        artifact = _mkartifact(data=b'{"ok": true}', seed_path="x.json")
        dest = tmp_path / "dest.bin"
        backend.fetch(artifact, dest)
        assert dest.read_bytes() == b'{"ok": true}'

    def test_missing_seed_path_raises(self, tmp_path: Path) -> None:
        backend = SeedBackend(seed_root=tmp_path)
        artifact = CorpusArtifact(
            key="bad", component="01-scenario-agent", role="golden",
            content_type="scenario_json", storage="seed",
            sha256="a" * 64, size_bytes=1,
            seed_path="definitely-not-there.bin",
        )
        with pytest.raises(FileNotFoundError, match="does not exist"):
            backend.fetch(artifact, tmp_path / "dest.bin")

    def test_directory_traversal_blocked(self, tmp_path: Path) -> None:
        # Attacker-controlled manifest entry tries to escape seed root.
        outside = tmp_path.parent / "escape.bin"
        outside.write_bytes(b"secret")
        seed_root = tmp_path / "seeds"
        seed_root.mkdir()
        backend = SeedBackend(seed_root=seed_root)
        artifact = CorpusArtifact(
            key="evil", component="01-scenario-agent", role="adversarial",
            content_type="scenario_json", storage="seed",
            sha256="a" * 64, size_bytes=1,
            seed_path="../escape.bin",
        )
        with pytest.raises(ValueError, match="escapes root"):
            backend.fetch(artifact, tmp_path / "dest.bin")


class TestMockBackend:
    def test_roundtrip(self, tmp_path: Path) -> None:
        backend = MockBackend()
        data = b"mock-bytes"
        sha = hashlib.sha256(data).hexdigest()
        backend.put(sha, data)
        artifact = _mkartifact(data=data)
        dest = tmp_path / "dest.bin"
        backend.fetch(artifact, dest)
        assert dest.read_bytes() == data
        assert backend.fetch_calls == [artifact.key]

    def test_missing_blob_raises(self, tmp_path: Path) -> None:
        backend = MockBackend()
        artifact = _mkartifact(data=b"unregistered")
        with pytest.raises(KeyError, match="no bytes for sha256"):
            backend.fetch(artifact, tmp_path / "dest.bin")


class TestCorpusFetcherResolve:
    def test_cache_miss_then_hit(self, tmp_path: Path) -> None:
        data = b"payload"
        seeds = tmp_path / "seeds"
        seeds.mkdir()
        (seeds / "f.bin").write_bytes(data)
        cache_root = tmp_path / "cache"

        fetcher = CorpusFetcher(
            cache_root=cache_root,
            seed_backend=SeedBackend(seed_root=seeds),
        )
        artifact = _mkartifact(data=data)

        path1 = fetcher.resolve(artifact)
        assert path1.read_bytes() == data
        mtime1 = path1.stat().st_mtime_ns

        # Second call should hit cache without re-invoking backend.
        # We prove it by deleting the seed — a miss would ENOENT.
        (seeds / "f.bin").unlink()
        path2 = fetcher.resolve(artifact)
        assert path2 == path1
        assert path2.stat().st_mtime_ns == mtime1

    def test_mock_precedence_over_seed(self, tmp_path: Path) -> None:
        # Even if storage="seed", a pre-registered mock blob for the
        # artifact's sha should be used first.  This lets unit tests
        # swap in controlled bytes without touching the seed dir.
        data = b"mock-override"
        sha = hashlib.sha256(data).hexdigest()
        mock = MockBackend({sha: data})
        fetcher = CorpusFetcher(cache_root=tmp_path / "c", mock_backend=mock)
        artifact = _mkartifact(data=data, seed_path=None, storage="b2", b2_key="x")
        resolved = fetcher.resolve(artifact)
        assert resolved.read_bytes() == data
        assert mock.fetch_calls == [artifact.key]

    def test_b2_storage_without_backend_raises(self, tmp_path: Path) -> None:
        fetcher = CorpusFetcher(cache_root=tmp_path / "c")
        artifact = _mkartifact(
            data=b"remote", storage="b2", seed_path=None, b2_key="corpus/x.bin",
        )
        with pytest.raises(RuntimeError, match="no b2 backend|requires b2 backend"):
            fetcher.resolve(artifact)

    def test_hash_mismatch_from_backend_raises(self, tmp_path: Path) -> None:
        # Seed file content doesn't match the artifact's sha — fetcher
        # must refuse to cache it.
        seeds = tmp_path / "seeds"
        seeds.mkdir()
        (seeds / "f.bin").write_bytes(b"wrong content")
        artifact = _mkartifact(data=b"right content")  # sha expects "right"
        fetcher = CorpusFetcher(
            cache_root=tmp_path / "c",
            seed_backend=SeedBackend(seed_root=seeds),
        )
        with pytest.raises(ValueError, match="mismatch"):
            fetcher.resolve(artifact)

    def test_default_manifest_fetches_end_to_end(self) -> None:
        # Wires up the repo's default manifest with the committed seeds
        # and confirms every artifact resolves + verifies.
        from strands_agents.corpus import load_default_manifest

        manifest = load_default_manifest()
        fetcher = build_default_fetcher(
            cache_root=Path("/tmp/test-corpus-cache-default"),
            enable_b2=False,
        )
        for artifact in manifest.artifacts:
            if artifact.storage != "seed":
                continue
            resolved = fetcher.resolve(artifact)
            assert resolved.exists()
            assert resolved.stat().st_size == artifact.size_bytes


class TestBuildDefaultFetcher:
    def test_no_b2_without_creds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("B2_KEY_ID", raising=False)
        monkeypatch.delenv("B2_APPLICATION_KEY", raising=False)
        fetcher = build_default_fetcher(cache_root=tmp_path)
        assert not fetcher.has_b2()

    def test_b2_enabled_when_creds_present(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("B2_KEY_ID", "dummy")
        monkeypatch.setenv("B2_APPLICATION_KEY", "dummy")
        fetcher = build_default_fetcher(cache_root=tmp_path)
        assert fetcher.has_b2()

    def test_explicit_disable_wins(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("B2_KEY_ID", "dummy")
        monkeypatch.setenv("B2_APPLICATION_KEY", "dummy")
        fetcher = build_default_fetcher(cache_root=tmp_path, enable_b2=False)
        assert not fetcher.has_b2()


class TestFixtureHelpers:
    def test_load_artifact_json_ok(self, tmp_path: Path) -> None:
        from strands_agents.corpus import load_artifact_json, load_default_manifest

        manifest = load_default_manifest()
        fetcher = build_default_fetcher(cache_root=tmp_path, enable_b2=False)
        # Pick a known-JSON artifact from the default manifest.
        for artifact in manifest.artifacts:
            if artifact.content_type == "scenario_json":
                loaded = load_artifact_json(fetcher, artifact)
                assert isinstance(loaded, dict)
                assert "scenes" in loaded
                return
        pytest.skip("no scenario_json seed available")

    def test_load_artifact_json_refuses_non_json(self) -> None:
        from strands_agents.corpus import load_artifact_json

        fetcher = CorpusFetcher(cache_root=Path("/tmp/does-not-matter"))
        artifact = CorpusArtifact(
            key="x", component="04-audio-agent", role="golden",
            content_type="audio_wav", storage="seed",
            sha256="a" * 64, size_bytes=10, seed_path="x.wav",
        )
        with pytest.raises(ValueError, match="not JSON"):
            load_artifact_json(fetcher, artifact)

    def test_filter_artifacts_by_component_and_role(self) -> None:
        from strands_agents.corpus import filter_artifacts, load_default_manifest

        manifest = load_default_manifest()
        goldens_01 = filter_artifacts(
            manifest, component="01-scenario-agent", role="golden",
        )
        for a in goldens_01:
            assert a.component == "01-scenario-agent"
            assert a.role == "golden"


class TestPublicAPI:
    def test_everything_importable(self) -> None:
        from strands_agents import corpus

        # Smoke: every entry in __all__ must resolve.
        for name in corpus.__all__:
            assert hasattr(corpus, name), f"missing public export <{name}>"
