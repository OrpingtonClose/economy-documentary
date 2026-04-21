"""Tests for the B2-backed judge-weight fetcher."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from strands_agents.judges.fetcher import (
    _bucket_file_size,
    cache_root_from_env,
    ensure_parent_dir,
    fetch_model_from_b2,
)
from strands_agents.judges.models import GEMMA4_ABLITERATED, JudgeModelSpec


class _FakeStream:
    """Stub returned by ``download_file_by_name`` — mimics b2sdk ``DownloadedFile``."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def save_to(self, path: str) -> None:
        Path(path).write_bytes(self._payload)


class _FakeFileInfo:
    def __init__(self, size: int) -> None:
        self.size = size


class _FakeBucket:
    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files
        self.downloaded: list[str] = []

    def get_file_info_by_name(self, key: str) -> _FakeFileInfo:
        if key not in self._files:
            raise FileNotFoundError(key)
        return _FakeFileInfo(len(self._files[key]))

    def download_file_by_name(self, key: str) -> _FakeStream:
        self.downloaded.append(key)
        if key not in self._files:
            raise FileNotFoundError(key)
        return _FakeStream(self._files[key])


@pytest.fixture
def tiny_spec() -> JudgeModelSpec:
    # Use a reduced spec so tests stay cheap and deterministic.
    return JudgeModelSpec(
        key="tiny_judge",
        display_name="Tiny",
        role="safety",
        hf_source="",
        b2_prefix="models/judges/tiny",
        params_billions=0.1,
        dtype="bf16",
        weights_gb=1.0,
        runtime_vram_gb=4,
        disk_gb=8,
        min_torch="2.7.0",
        min_cuda="12.6",
        checkpoint_files=("config.json", "model.safetensors"),
    )


class TestFetchModelFromB2:
    def test_downloads_every_listed_shard(self, tmp_path: Path, tiny_spec: JudgeModelSpec) -> None:
        bucket = _FakeBucket(
            {
                "models/judges/tiny/config.json": b"{}",
                "models/judges/tiny/model.safetensors": b"\x00\x01\x02",
            }
        )
        paths = fetch_model_from_b2(tiny_spec, tmp_path, bucket_factory=lambda: bucket)
        assert [p.name for p in paths] == ["config.json", "model.safetensors"]
        assert paths[0].read_bytes() == b"{}"
        assert paths[1].read_bytes() == b"\x00\x01\x02"

    def test_raises_when_spec_has_no_b2_mirror(self, tmp_path: Path) -> None:
        spec = JudgeModelSpec(
            key="no_mirror",
            display_name="x",
            role="safety",
            hf_source="org/model",
            b2_prefix="",
            params_billions=1,
            dtype="bf16",
            weights_gb=1,
            runtime_vram_gb=4,
            disk_gb=8,
            min_torch="2.7.0",
            min_cuda="12.6",
            checkpoint_files=("config.json",),
        )
        with pytest.raises(RuntimeError, match="no B2 mirror"):
            fetch_model_from_b2(spec, tmp_path, bucket_factory=lambda: _FakeBucket({}))

    def test_skips_already_present_files(self, tmp_path: Path, tiny_spec: JudgeModelSpec) -> None:
        bucket = _FakeBucket(
            {
                "models/judges/tiny/config.json": b"{}",
                "models/judges/tiny/model.safetensors": b"xxx",
            }
        )
        dest = tmp_path / tiny_spec.key
        dest.mkdir()
        (dest / "config.json").write_bytes(b"{}")  # matches remote size

        fetch_model_from_b2(tiny_spec, tmp_path, bucket_factory=lambda: bucket)
        # Only the second shard should have been fetched.
        assert bucket.downloaded == ["models/judges/tiny/model.safetensors"]

    def test_redownloads_when_local_size_differs(self, tmp_path: Path, tiny_spec: JudgeModelSpec) -> None:
        bucket = _FakeBucket(
            {
                "models/judges/tiny/config.json": b"{}",
                "models/judges/tiny/model.safetensors": b"abcdef",
            }
        )
        dest = tmp_path / tiny_spec.key
        dest.mkdir()
        (dest / "model.safetensors").write_bytes(b"old")  # shorter than remote

        fetch_model_from_b2(
            tiny_spec,
            tmp_path,
            bucket_factory=lambda: bucket,
            files=("model.safetensors",),
        )
        assert (dest / "model.safetensors").read_bytes() == b"abcdef"

    def test_force_flag_redownloads_even_when_matching(self, tmp_path: Path, tiny_spec: JudgeModelSpec) -> None:
        bucket = _FakeBucket(
            {
                "models/judges/tiny/config.json": b"{}",
            }
        )
        dest = tmp_path / tiny_spec.key
        dest.mkdir()
        (dest / "config.json").write_bytes(b"{}")

        fetch_model_from_b2(
            tiny_spec,
            tmp_path,
            bucket_factory=lambda: bucket,
            files=("config.json",),
            force=True,
        )
        assert bucket.downloaded == ["models/judges/tiny/config.json"]

    def test_files_parameter_filters_to_subset(self, tmp_path: Path, tiny_spec: JudgeModelSpec) -> None:
        bucket = _FakeBucket(
            {
                "models/judges/tiny/config.json": b"{}",
                "models/judges/tiny/model.safetensors": b"xxx",
            }
        )
        fetch_model_from_b2(
            tiny_spec,
            tmp_path,
            bucket_factory=lambda: bucket,
            files=("config.json",),
        )
        assert bucket.downloaded == ["models/judges/tiny/config.json"]

    def test_leaves_partial_file_when_download_fails(
        self,
        tmp_path: Path,
        tiny_spec: JudgeModelSpec,
    ) -> None:
        class ExplodingStream:
            def save_to(self, path: str) -> None:
                Path(path).write_bytes(b"partial")
                raise RuntimeError("disk full mid-download")

        class ExplodingBucket:
            downloaded: list[str] = []

            def get_file_info_by_name(self, key: str) -> _FakeFileInfo:
                return _FakeFileInfo(999)

            def download_file_by_name(self, key: str) -> Any:
                self.downloaded.append(key)
                return ExplodingStream()

        with pytest.raises(RuntimeError, match="disk full"):
            fetch_model_from_b2(
                tiny_spec,
                tmp_path,
                bucket_factory=lambda: ExplodingBucket(),
                files=("config.json",),
            )
        dest = tmp_path / tiny_spec.key / "config.json.part"
        assert dest.exists()
        assert dest.read_bytes() == b"partial"

    def test_catalog_entry_integrates(self, tmp_path: Path) -> None:
        # Spot-check the real Gemma spec flows through the fetcher path
        # without hitting the network — we only materialise one file.
        bucket = _FakeBucket(
            {f"{GEMMA4_ABLITERATED.b2_prefix}/config.json": b"{}"}
        )
        paths = fetch_model_from_b2(
            GEMMA4_ABLITERATED,
            tmp_path,
            bucket_factory=lambda: bucket,
            files=("config.json",),
        )
        assert paths[0].parent.name == GEMMA4_ABLITERATED.key


class TestUtilities:
    def test_ensure_parent_dir_creates_directory_from_dir_path(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b"
        result = ensure_parent_dir(target)
        assert result.exists() and result.is_dir()

    def test_ensure_parent_dir_creates_parent_when_file_path_given(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "weights.bin"
        result = ensure_parent_dir(target)
        assert result == target.parent
        assert result.exists()

    def test_cache_root_honours_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JUDGE_CACHE_ROOT", "/custom/path")
        assert str(cache_root_from_env()) == "/custom/path"

    def test_cache_root_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("JUDGE_CACHE_ROOT", raising=False)
        assert str(cache_root_from_env()) == "/tmp/judge-cache"


class TestBucketFileSize:
    """Regression coverage for the size-probe helper."""

    def test_zero_byte_size_preserved(self) -> None:
        # Regression: `size or content_length` treated 0 as falsy and
        # fell through to content_length (which is typically absent on
        # b2sdk FileVersion objects), returning None. That made the
        # fetcher skip the size-match check and re-download a file that
        # was actually complete.
        class ZeroSizeBucket:
            def get_file_info_by_name(self, key: str) -> Any:
                class Info:
                    size = 0

                return Info()

        assert _bucket_file_size(ZeroSizeBucket(), "k") == 0

    def test_size_attr_missing_falls_back_to_content_length(self) -> None:
        class Bucket:
            def get_file_info_by_name(self, key: str) -> Any:
                class Info:
                    content_length = 123

                return Info()

        assert _bucket_file_size(Bucket(), "k") == 123

    def test_returns_none_when_bucket_lacks_method(self) -> None:
        class Bucket:
            pass

        assert _bucket_file_size(Bucket(), "k") is None

    def test_returns_none_when_info_has_no_size_fields(self) -> None:
        class Bucket:
            def get_file_info_by_name(self, key: str) -> Any:
                class Info:
                    pass

                return Info()

        assert _bucket_file_size(Bucket(), "k") is None
