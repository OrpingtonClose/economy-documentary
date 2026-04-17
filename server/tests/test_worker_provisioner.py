"""Tests for server/worker_provisioner.py (issues #63, #65, #62).

These tests exercise the provisioning logic without calling Vast.ai.
They cover:

1. ``--mode`` alias normalisation — ``video`` must map to ``ltx`` so
   gpu_worker.py accepts it (issue #63).
2. Env-var honouring — ``TTS_WORKER_URL`` / ``GPU_WORKER_URL`` /
   ``VIDEO_WORKER_URLS`` must short-circuit provisioning (issue #65).
3. Health-wait timeout clamp — callers can't opt out of the 15-minute
   hard ceiling (issue #62).

We import the module lazily inside each test so pytest collection
doesn't pay the cost of the heavy provisioner imports if the module
isn't available (e.g. minimal CI runner without b2sdk/vastai).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))


# ---------------------------------------------------------------------------
# #63: --mode alias normalisation
# ---------------------------------------------------------------------------


def test_normalize_worker_mode_video_maps_to_ltx():
    from worker_provisioner import normalize_worker_mode

    assert normalize_worker_mode("video") == "ltx"
    assert normalize_worker_mode("Video") == "ltx"
    assert normalize_worker_mode("LTX2") == "ltx"


def test_normalize_worker_mode_passthrough():
    from worker_provisioner import normalize_worker_mode

    assert normalize_worker_mode("tts") == "tts"
    assert normalize_worker_mode("ltx") == "ltx"
    assert normalize_worker_mode("both") == "both"


def test_normalize_worker_mode_invalid_raises():
    from worker_provisioner import normalize_worker_mode

    with pytest.raises(ValueError):
        normalize_worker_mode("sdxl")
    with pytest.raises(ValueError):
        normalize_worker_mode("")


def test_video_spec_uses_ltx_mode():
    """Regression guard: the default VIDEO_SPEC must NOT send --mode video."""
    from worker_provisioner import VIDEO_SPEC

    assert VIDEO_SPEC.worker_mode == "ltx"


# ---------------------------------------------------------------------------
# #65: env-var honouring short-circuits provisioning
# ---------------------------------------------------------------------------


class _FakeProvisionerContext:
    """Isolate start_provisioning state across tests."""

    def __init__(self):
        import worker_provisioner as wp

        self.wp = wp
        self.saved_env = {
            k: os.environ.get(k, "")
            for k in (
                "TTS_WORKER_URL",
                "GPU_WORKER_URL",
                "VIDEO_WORKER_URLS",
            )
        }

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        for k, v in self.saved_env.items():
            if v:
                os.environ[k] = v
            elif k in os.environ:
                del os.environ[k]


def _fresh_provisioner():
    """Build a provisioner instance without any module-level singletons."""
    import importlib

    import worker_provisioner

    importlib.reload(worker_provisioner)
    return worker_provisioner


def test_start_provisioning_honors_preset_tts_url(monkeypatch):
    """Pre-set ``TTS_WORKER_URL`` should mark the worker ready WITHOUT
    calling Vast.ai (issue #65).
    """
    with _FakeProvisionerContext() as ctx:
        wp = ctx.wp
        monkeypatch.setenv("TTS_WORKER_URL", "http://tts.example.com:8880")
        monkeypatch.delenv("GPU_WORKER_URL", raising=False)
        monkeypatch.delenv("VIDEO_WORKER_URLS", raising=False)
        # Simulate "worker unreachable" — we should STILL honour the env var.
        monkeypatch.setattr(wp, "check_worker_health", lambda *a, **k: False)

        # Block every Vast.ai entry point so a regression would raise.
        def _boom(*a, **k):
            raise AssertionError("Vast.ai must NOT be called when env var is pre-set")

        monkeypatch.setattr(wp, "_provision_worker", _boom, raising=False)
        monkeypatch.setattr(wp, "provision_and_tunnel", _boom, raising=False)

        provisioner = wp.WorkerProvisioner()
        provisioner.start_provisioning(require_tts=True, require_video=False)

        # The tts spec should now be marked ready.
        specs = provisioner.get_specs() if hasattr(provisioner, "get_specs") else provisioner._specs
        tts = next(s for s in specs if s.role == "tts")
        assert tts.worker_url == "http://tts.example.com:8880"
        assert tts.status in ("externally_managed", "healthy")
        assert tts.ready_event.is_set()


def test_start_provisioning_honors_video_worker_urls(monkeypatch):
    """``VIDEO_WORKER_URLS`` should short-circuit video provisioning too."""
    with _FakeProvisionerContext() as ctx:
        wp = ctx.wp
        monkeypatch.delenv("TTS_WORKER_URL", raising=False)
        monkeypatch.delenv("GPU_WORKER_URL", raising=False)
        monkeypatch.setenv(
            "VIDEO_WORKER_URLS",
            "http://v1.example.com:8880,http://v2.example.com:8880",
        )
        monkeypatch.setattr(wp, "check_worker_health", lambda *a, **k: True)

        def _boom(*a, **k):
            raise AssertionError("Vast.ai must NOT be called when VIDEO_WORKER_URLS is set")

        monkeypatch.setattr(wp, "_provision_worker", _boom, raising=False)
        monkeypatch.setattr(wp, "provision_and_tunnel", _boom, raising=False)

        provisioner = wp.WorkerProvisioner()
        provisioner.start_provisioning(require_tts=False, require_video=True)

        specs = provisioner.get_specs() if hasattr(provisioner, "get_specs") else provisioner._specs
        video = next(s for s in specs if s.role == "video")
        assert video.worker_url == "http://v1.example.com:8880"
        assert video.ready_event.is_set()


# ---------------------------------------------------------------------------
# #62: wait-for-worker timeout clamp + heartbeat
# ---------------------------------------------------------------------------


def test_wait_for_worker_healthy_clamps_to_15min(monkeypatch):
    """Callers can't raise the timeout above 15 minutes (issue #62)."""
    import worker_provisioner as wp

    calls: list[int] = []

    def fake_get_detail(url, timeout=10):
        calls.append(int(time.time()))
        return None  # never becomes healthy

    monkeypatch.setattr(wp, "_get_worker_health_detail", fake_get_detail)
    monkeypatch.setattr(wp.time, "sleep", lambda *_: None)

    # Freeze time with a monotonically advancing fake clock.
    now = {"t": 1_000_000.0}

    def fake_time():
        now["t"] += 60.0  # advance 60s per call
        return now["t"]

    monkeypatch.setattr(wp.time, "time", fake_time)

    spec = wp.WorkerSpec(
        role="tts",
        env_var="TTS_WORKER_URL",
        local_port=8880,
        remote_port=8880,
        capability="tts",
        worker_mode="tts",
    )
    spec.worker_url = "http://example.com:8880"

    # Request a 1-hour timeout; the clamp must pull it back to 15 min.
    result = wp.wait_for_worker_healthy(spec, timeout=60 * 60, poll_interval=1)

    assert result is False
    assert spec.bootstrap_error_category == "timeout"
    # With a 15-minute effective window and 60s fake steps, we should
    # never have spent more than ~16 iterations.
    assert len(calls) <= 20
