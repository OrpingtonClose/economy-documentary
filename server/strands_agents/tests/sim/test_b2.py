"""Direct-proof tests for :class:`FakeB2`."""

from __future__ import annotations

import os

import pytest

from strands_agents.sim.b2 import FakeB2
from strands_agents.sim.recorder import Recorder


class TestFakeB2Upload:
    def test_upload_returns_fake_b2_url(self, tmp_path) -> None:
        blob_path = tmp_path / "clip.wav"
        blob_path.write_bytes(b"hello")
        b2 = FakeB2()
        url = b2.upload(str(blob_path))
        assert url.startswith("fake-b2://")
        assert url.endswith("/clip.wav")

    def test_identical_bytes_collapse_to_same_url(self, tmp_path) -> None:
        a = tmp_path / "a.wav"
        b = tmp_path / "b.wav"
        a.write_bytes(b"same-content")
        b.write_bytes(b"same-content")
        b2 = FakeB2()
        # Content-addressed: bytes match → hash matches → URL prefix
        # matches. (Basenames differ so the full URLs differ, but the
        # blob store keeps both entries pointing at the same bytes.)
        url_a = b2.upload(str(a))
        url_b = b2.upload(str(b))
        assert url_a.rsplit("/", 1)[0] == url_b.rsplit("/", 1)[0]
        assert b2.get(url_a) == b2.get(url_b) == b"same-content"

    def test_get_returns_uploaded_bytes(self, tmp_path) -> None:
        b2 = FakeB2()
        src = tmp_path / "x.bin"
        src.write_bytes(b"payload")
        url = b2.upload(str(src))
        assert b2.get(url) == b"payload"

    def test_upload_missing_file_raises(self, tmp_path) -> None:
        b2 = FakeB2()
        missing = tmp_path / "does-not-exist.wav"
        with pytest.raises(FileNotFoundError):
            b2.upload(str(missing))

    def test_contains_and_len(self, tmp_path) -> None:
        b2 = FakeB2()
        src = tmp_path / "x.bin"
        src.write_bytes(b"a")
        url = b2.upload(str(src))
        assert url in b2
        assert len(b2) == 1

    def test_upload_bytes_shortcut(self) -> None:
        b2 = FakeB2()
        url = b2.upload_bytes(b"inline", basename="inline.bin")
        assert b2.get(url) == b"inline"
        assert url.endswith("/inline.bin")

    def test_urls_snapshot(self, tmp_path) -> None:
        b2 = FakeB2()
        p1 = tmp_path / "1.bin"
        p2 = tmp_path / "2.bin"
        p1.write_bytes(b"one")
        p2.write_bytes(b"two")
        u1 = b2.upload(str(p1))
        u2 = b2.upload(str(p2))
        assert set(b2.urls()) == {u1, u2}


class TestFakeB2Recording:
    def test_records_uploads(self, tmp_path) -> None:
        r = Recorder()
        b2 = FakeB2(recorder=r)
        src = tmp_path / "s.bin"
        src.write_bytes(b"x")
        b2.upload(str(src))
        b2.upload_bytes(b"y", basename="y.bin")
        ops = r.ops(channel="b2")
        assert ops == ["upload", "upload_bytes"]

    def test_basename_respects_filename(self, tmp_path) -> None:
        b2 = FakeB2()
        src = tmp_path / "narration_s1.wav"
        src.write_bytes(b"audio")
        url = b2.upload(str(src))
        # Basename survives hashing — test authors can grep URLs by
        # filename when debugging a trajectory.
        assert os.path.basename(url) == "narration_s1.wav"
