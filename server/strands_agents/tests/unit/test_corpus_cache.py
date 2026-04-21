"""Unit tests for the content-addressed corpus cache.

Hermetic: all paths go through ``tmp_path``.  No network, no B2.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from strands_agents.corpus import cache


class TestResolveCacheRoot:
    def test_override_takes_precedence(self, tmp_path: Path) -> None:
        override = tmp_path / "explicit"
        root = cache.resolve_cache_root(override)
        assert root == override.resolve()
        assert root.is_dir()

    def test_env_var_when_no_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("STRANDS_CORPUS_CACHE", str(tmp_path / "envvar"))
        root = cache.resolve_cache_root()
        assert root == (tmp_path / "envvar").resolve()


class TestPathFor:
    def test_sharded_layout(self, tmp_path: Path) -> None:
        sha = "a" * 64
        path = cache.path_for(tmp_path, sha)
        assert path.parent.name == "aa"
        assert path.name == sha

    def test_invalid_sha_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="invalid sha256"):
            cache.path_for(tmp_path, "abc")


class TestComputeSha256:
    def test_matches_stdlib(self, tmp_path: Path) -> None:
        data = b"hello world" * 100
        p = tmp_path / "file.bin"
        p.write_bytes(data)
        assert cache.compute_sha256(p) == hashlib.sha256(data).hexdigest()

    def test_large_file(self, tmp_path: Path) -> None:
        # Confirm chunked read gives the same digest as one-shot.
        data = b"x" * (1 << 17)  # 128 KiB, > chunk_size
        p = tmp_path / "big.bin"
        p.write_bytes(data)
        assert cache.compute_sha256(p) == hashlib.sha256(data).hexdigest()


class TestVerifyBytes:
    def test_ok(self, tmp_path: Path) -> None:
        p = tmp_path / "f.bin"
        p.write_bytes(b"payload")
        expected = hashlib.sha256(b"payload").hexdigest()
        assert cache.verify_bytes(p, expected)

    def test_mismatch_returns_false(self, tmp_path: Path) -> None:
        p = tmp_path / "f.bin"
        p.write_bytes(b"payload")
        assert not cache.verify_bytes(p, "0" * 64)

    def test_missing_returns_false(self, tmp_path: Path) -> None:
        assert not cache.verify_bytes(tmp_path / "missing.bin", "0" * 64)


class TestStore:
    def test_happy_path(self, tmp_path: Path) -> None:
        root = tmp_path / "cache"
        root.mkdir()
        data = b"some-bytes"
        sha = hashlib.sha256(data).hexdigest()
        source = tmp_path / "src.bin"
        source.write_bytes(data)

        dest = cache.store(root, sha, source)
        assert dest == cache.path_for(root, sha)
        assert dest.read_bytes() == data
        # Source preserved by default (copy=True).
        assert source.exists()

    def test_move_semantics(self, tmp_path: Path) -> None:
        root = tmp_path / "cache"
        root.mkdir()
        data = b"mv-bytes"
        sha = hashlib.sha256(data).hexdigest()
        source = tmp_path / "src.bin"
        source.write_bytes(data)

        cache.store(root, sha, source, copy=False)
        assert not source.exists()

    def test_hash_mismatch_raises(self, tmp_path: Path) -> None:
        root = tmp_path / "cache"
        root.mkdir()
        source = tmp_path / "src.bin"
        source.write_bytes(b"actual")
        wrong_sha = "0" * 64
        with pytest.raises(ValueError, match="mismatch"):
            cache.store(root, wrong_sha, source)

    def test_second_store_is_noop(self, tmp_path: Path) -> None:
        root = tmp_path / "cache"
        root.mkdir()
        data = b"repeat"
        sha = hashlib.sha256(data).hexdigest()
        source = tmp_path / "src.bin"
        source.write_bytes(data)

        first = cache.store(root, sha, source)
        first_mtime = first.stat().st_mtime_ns
        # Second call with fresh source should short-circuit.
        source2 = tmp_path / "src2.bin"
        source2.write_bytes(data)
        second = cache.store(root, sha, source2)
        assert second == first
        assert second.stat().st_mtime_ns == first_mtime

    def test_corrupted_target_is_rewritten(self, tmp_path: Path) -> None:
        # Regression: a previously-cached entry that went corrupt (disk
        # error, external tampering) must be re-written by store() so the
        # resolve() → re-fetch → store() path can self-heal.  Earlier
        # short-circuited on target.exists() alone, permanently poisoning
        # the cache entry.
        root = tmp_path / "cache"
        root.mkdir()
        good = b"good-bytes"
        sha = hashlib.sha256(good).hexdigest()
        source = tmp_path / "src.bin"
        source.write_bytes(good)

        first = cache.store(root, sha, source)
        first.write_bytes(b"corrupted!")  # Simulate on-disk corruption.
        assert not cache.verify_bytes(first, sha)

        # Fresh source, correct hash — store must overwrite the bad entry.
        source2 = tmp_path / "src2.bin"
        source2.write_bytes(good)
        second = cache.store(root, sha, source2)
        assert second == first
        assert second.read_bytes() == good
        assert cache.verify_bytes(second, sha)
