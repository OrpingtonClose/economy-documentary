"""Unit tests for the worker registry + VRAM pre-flight probe (slice 3)."""

from __future__ import annotations

import pytest

from strands_agents.playground.worker_registry import (
    HEARTBEAT_STALE_SECONDS,
    NO_WORKERS_REGISTERED,
    VRAM_INSUFFICIENT,
    DuplicateWorkerError,
    NoWorkersRegisteredError,
    VoiceAlreadyPinnedError,
    VoiceOnNonTtsWorkerError,
    VramInsufficientError,
    VramProbeResult,
    VramShortfall,
    Worker,
    WorkerAlreadyHasVoiceError,
    WorkerNotFoundError,
    WorkerRegistry,
    get_default_registry,
    preflight_vram,
    set_default_registry,
    vram_insufficient_envelope,
)


# ---- fixtures ----------------------------------------------------------


@pytest.fixture
def clock() -> list[float]:
    """Mutable clock: tests drive time by mutating ``clock[0]``."""

    return [1_000.0]


@pytest.fixture
def registry(clock: list[float]) -> WorkerRegistry:
    return WorkerRegistry(now=lambda: clock[0])


# ---- registration basics ----------------------------------------------


def test_register_worker_records_state(registry: WorkerRegistry) -> None:
    w = registry.register_worker(
        worker_id="tts-a3f",
        role="tts",
        endpoint_url="http://10.0.0.5:8080",
        vram_gb=24,
    )
    assert w.worker_id == "tts-a3f"
    assert w.role == "tts"
    assert w.vram_gb == 24
    assert w.voice_id is None
    assert registry.has_worker("tts-a3f") is True
    assert registry.get_worker("tts-a3f") is w


def test_register_rejects_duplicate_id(registry: WorkerRegistry) -> None:
    registry.register_worker(
        worker_id="tts-a3f", role="tts", endpoint_url="http://x", vram_gb=24,
    )
    with pytest.raises(DuplicateWorkerError) as excinfo:
        registry.register_worker(
            worker_id="tts-a3f", role="tts", endpoint_url="http://x", vram_gb=24,
        )
    assert excinfo.value.worker_id == "tts-a3f"


def test_register_rejects_unknown_role(registry: WorkerRegistry) -> None:
    with pytest.raises(ValueError, match="unknown worker role"):
        registry.register_worker(
            worker_id="x-1", role="captioning", endpoint_url="http://x", vram_gb=8,  # type: ignore[arg-type]
        )


def test_register_rejects_nonpositive_vram(registry: WorkerRegistry) -> None:
    with pytest.raises(ValueError, match="vram_gb must be positive"):
        registry.register_worker(
            worker_id="x-1", role="tts", endpoint_url="http://x", vram_gb=0,
        )


def test_unregister_removes_worker_and_releases_voice(
    registry: WorkerRegistry,
) -> None:
    registry.register_worker(
        worker_id="tts-a3f",
        role="tts",
        endpoint_url="http://x",
        vram_gb=24,
        voice_id="narrator_male_1",
    )
    registry.unregister_worker("tts-a3f")
    assert registry.has_worker("tts-a3f") is False
    assert registry.voice_owner("narrator_male_1") is None
    # Voice is now available for a fresh worker:
    registry.register_worker(
        worker_id="tts-b7c",
        role="tts",
        endpoint_url="http://y",
        vram_gb=24,
        voice_id="narrator_male_1",
    )
    assert registry.voice_owner("narrator_male_1") == "tts-b7c"


def test_unregister_unknown_id_raises(registry: WorkerRegistry) -> None:
    with pytest.raises(WorkerNotFoundError):
        registry.unregister_worker("nope")


# ---- one-voice-per-VM invariant (AGENTS.md §1) ------------------------


def test_voice_at_register_rejects_non_tts_role(registry: WorkerRegistry) -> None:
    with pytest.raises(VoiceOnNonTtsWorkerError):
        registry.register_worker(
            worker_id="ltx-9bb",
            role="ltx_render",
            endpoint_url="http://x",
            vram_gb=48,
            voice_id="narrator_male_1",
        )
    assert registry.has_worker("ltx-9bb") is False


def test_voice_already_pinned_elsewhere_blocks_new_registration(
    registry: WorkerRegistry,
) -> None:
    registry.register_worker(
        worker_id="tts-a3f",
        role="tts",
        endpoint_url="http://x",
        vram_gb=24,
        voice_id="narrator_male_1",
    )
    with pytest.raises(VoiceAlreadyPinnedError) as excinfo:
        registry.register_worker(
            worker_id="tts-b7c",
            role="tts",
            endpoint_url="http://y",
            vram_gb=24,
            voice_id="narrator_male_1",
        )
    assert excinfo.value.voice_id == "narrator_male_1"
    assert excinfo.value.other_worker_id == "tts-a3f"
    assert registry.has_worker("tts-b7c") is False


def test_pin_voice_on_unregistered_worker_raises(
    registry: WorkerRegistry,
) -> None:
    with pytest.raises(WorkerNotFoundError):
        registry.pin_voice("ghost", "narrator_male_1")


def test_pin_voice_on_non_tts_worker_raises(registry: WorkerRegistry) -> None:
    registry.register_worker(
        worker_id="ltx-9bb", role="ltx_render", endpoint_url="http://x", vram_gb=48,
    )
    with pytest.raises(VoiceOnNonTtsWorkerError):
        registry.pin_voice("ltx-9bb", "narrator_male_1")


def test_pin_voice_is_idempotent_on_same_pair(registry: WorkerRegistry) -> None:
    registry.register_worker(
        worker_id="tts-a3f", role="tts", endpoint_url="http://x", vram_gb=24,
    )
    registry.pin_voice("tts-a3f", "narrator_male_1")
    registry.pin_voice("tts-a3f", "narrator_male_1")  # no-op, no raise
    assert registry.get_worker("tts-a3f").voice_id == "narrator_male_1"


def test_pin_second_voice_on_same_worker_raises(
    registry: WorkerRegistry,
) -> None:
    registry.register_worker(
        worker_id="tts-a3f", role="tts", endpoint_url="http://x", vram_gb=24,
    )
    registry.pin_voice("tts-a3f", "narrator_male_1")
    with pytest.raises(WorkerAlreadyHasVoiceError) as excinfo:
        registry.pin_voice("tts-a3f", "narrator_female_2")
    assert excinfo.value.existing_voice_id == "narrator_male_1"
    assert excinfo.value.new_voice_id == "narrator_female_2"


def test_pin_voice_already_on_other_worker_raises(
    registry: WorkerRegistry,
) -> None:
    registry.register_worker(
        worker_id="tts-a3f", role="tts", endpoint_url="http://x", vram_gb=24,
        voice_id="narrator_male_1",
    )
    registry.register_worker(
        worker_id="tts-b7c", role="tts", endpoint_url="http://y", vram_gb=24,
    )
    with pytest.raises(VoiceAlreadyPinnedError) as excinfo:
        registry.pin_voice("tts-b7c", "narrator_male_1")
    assert excinfo.value.other_worker_id == "tts-a3f"


# ---- heartbeat / probe ------------------------------------------------


def test_heartbeat_updates_timestamp_and_free_vram(
    clock: list[float], registry: WorkerRegistry
) -> None:
    registry.register_worker(
        worker_id="ltx-9bb", role="ltx_render", endpoint_url="http://x", vram_gb=48,
    )
    clock[0] = 1_020.0
    registry.heartbeat("ltx-9bb", free_vram_gb=45)
    w = registry.get_worker("ltx-9bb")
    assert w.last_heartbeat_at == 1_020.0
    assert w.last_probe is not None
    assert w.last_probe.total_gb == 48
    assert w.last_probe.free_gb == 45


def test_heartbeat_unknown_worker_raises(registry: WorkerRegistry) -> None:
    with pytest.raises(WorkerNotFoundError):
        registry.heartbeat("ghost")


def test_record_probe_replaces_vram_from_worker_self_report(
    clock: list[float], registry: WorkerRegistry
) -> None:
    registry.register_worker(
        worker_id="ltx-9bb", role="ltx_render", endpoint_url="http://x", vram_gb=24,
    )
    # Probe reads torch.cuda.get_device_properties directly; trust it
    # over the worker's self-report on register.
    clock[0] = 1_030.0
    registry.record_probe(
        VramProbeResult(
            worker_id="ltx-9bb",
            total_gb=48,
            free_gb=47,
            compute_capability=(8, 0),
            probed_at=1_030.0,
        )
    )
    w = registry.get_worker("ltx-9bb")
    assert w.vram_gb == 48
    assert w.last_heartbeat_at == 1_030.0
    assert w.last_probe is not None
    assert w.last_probe.compute_capability == (8, 0)


def test_is_stale_tracks_heartbeat_age(
    clock: list[float], registry: WorkerRegistry
) -> None:
    clock[0] = 1_000.0
    registry.register_worker(
        worker_id="ltx-9bb", role="ltx_render", endpoint_url="http://x", vram_gb=48,
    )
    w = registry.get_worker("ltx-9bb")
    clock[0] = 1_000.0 + HEARTBEAT_STALE_SECONDS - 1
    assert registry.is_stale(w) is False
    clock[0] = 1_000.0 + HEARTBEAT_STALE_SECONDS + 1
    assert registry.is_stale(w) is True


# ---- preflight_vram ---------------------------------------------------


def test_preflight_passes_when_all_workers_meet_requirement(
    registry: WorkerRegistry,
) -> None:
    registry.register_worker(
        worker_id="ltx-a", role="ltx_render", endpoint_url="http://a", vram_gb=48,
    )
    registry.register_worker(
        worker_id="ltx-b", role="ltx_render", endpoint_url="http://b", vram_gb=80,
    )
    eligible = preflight_vram(
        registry, role="ltx_render", required_gb=48, model="ltx-video-2.3",
    )
    assert {w.worker_id for w in eligible} == {"ltx-a", "ltx-b"}


def test_preflight_raises_with_exact_shortfall(registry: WorkerRegistry) -> None:
    registry.register_worker(
        worker_id="ltx-a", role="ltx_render", endpoint_url="http://a", vram_gb=40,
    )
    registry.register_worker(
        worker_id="ltx-b", role="ltx_render", endpoint_url="http://b", vram_gb=80,
    )
    with pytest.raises(VramInsufficientError) as excinfo:
        preflight_vram(
            registry,
            role="ltx_render",
            required_gb=48,
            model="ltx-video-2.3",
        )
    err = excinfo.value
    assert err.role == "ltx_render"
    assert err.required_gb == 48
    assert err.model == "ltx-video-2.3"
    # Only the insufficient worker shows up in shortfalls -- not the
    # ones that would have passed. This is what drives the frontend
    # "40 GB < 48 GB required" line.
    assert len(err.shortfalls) == 1
    assert err.shortfalls[0] == VramShortfall(
        worker_id="ltx-a", actual_gb=40, required_gb=48
    )
    assert "40GB" in str(err)


def test_preflight_lists_all_shortfalls_not_just_first(
    registry: WorkerRegistry,
) -> None:
    registry.register_worker(
        worker_id="ltx-a", role="ltx_render", endpoint_url="http://a", vram_gb=24,
    )
    registry.register_worker(
        worker_id="ltx-b", role="ltx_render", endpoint_url="http://b", vram_gb=40,
    )
    with pytest.raises(VramInsufficientError) as excinfo:
        preflight_vram(
            registry,
            role="ltx_render",
            required_gb=48,
            model="ltx-video-2.3",
        )
    ids = {s.worker_id for s in excinfo.value.shortfalls}
    assert ids == {"ltx-a", "ltx-b"}


def test_preflight_raises_when_no_workers_registered(
    registry: WorkerRegistry,
) -> None:
    with pytest.raises(NoWorkersRegisteredError) as excinfo:
        preflight_vram(
            registry,
            role="ltx_render",
            required_gb=48,
            model="ltx-video-2.3",
        )
    assert excinfo.value.role == "ltx_render"


def test_preflight_treats_stale_workers_as_absent(
    clock: list[float], registry: WorkerRegistry
) -> None:
    clock[0] = 1_000.0
    registry.register_worker(
        worker_id="ltx-a", role="ltx_render", endpoint_url="http://a", vram_gb=80,
    )
    # Advance past the staleness threshold -- no heartbeat in between.
    clock[0] = 1_000.0 + HEARTBEAT_STALE_SECONDS + 60.0
    with pytest.raises(NoWorkersRegisteredError):
        preflight_vram(
            registry,
            role="ltx_render",
            required_gb=48,
            model="ltx-video-2.3",
        )
    # Same state, include_stale=True -> passes (useful for admin
    # pages that want to show stale workers too).
    eligible = preflight_vram(
        registry,
        role="ltx_render",
        required_gb=48,
        model="ltx-video-2.3",
        include_stale=True,
    )
    assert len(eligible) == 1


# ---- envelope -----------------------------------------------------


def test_vram_insufficient_envelope_shape() -> None:
    err = VramInsufficientError(
        role="ltx_render",
        required_gb=48,
        model="ltx-video-2.3",
        shortfalls=(
            VramShortfall(worker_id="ltx-a", actual_gb=40, required_gb=48),
        ),
    )
    env = vram_insufficient_envelope(err)
    assert env["stage"] == "production"
    assert env["reason"] == VRAM_INSUFFICIENT
    detail = env["detail"]
    assert isinstance(detail, dict)
    assert detail["role"] == "ltx_render"
    assert detail["required_gb"] == 48
    assert detail["model"] == "ltx-video-2.3"
    assert detail["shortfalls"] == [
        {"worker_id": "ltx-a", "actual_gb": 40, "required_gb": 48},
    ]


def test_vram_insufficient_envelope_accepts_custom_stage() -> None:
    err = VramInsufficientError(
        role="tts",
        required_gb=16,
        model="qwen3-tts",
        shortfalls=(
            VramShortfall(worker_id="tts-c", actual_gb=12, required_gb=16),
        ),
    )
    env = vram_insufficient_envelope(err, stage="timing")
    assert env["stage"] == "timing"


# ---- constants exposed as part of the public surface -----------------


def test_vram_insufficient_sentinel_is_stable() -> None:
    # Frozen-string contract with the frontend and CI matchers.
    assert VRAM_INSUFFICIENT == "VRAM_INSUFFICIENT"
    assert NO_WORKERS_REGISTERED == "NO_WORKERS_REGISTERED"


# ---- default registry ------------------------------------------------


def test_default_registry_is_process_wide() -> None:
    set_default_registry(None)
    try:
        a = get_default_registry()
        b = get_default_registry()
        assert a is b
    finally:
        set_default_registry(None)


def test_set_default_registry_swaps_and_clears() -> None:
    custom = WorkerRegistry()
    set_default_registry(custom)
    try:
        assert get_default_registry() is custom
    finally:
        set_default_registry(None)
    # Cleared -> fresh one next call.
    fresh = get_default_registry()
    assert fresh is not custom
    set_default_registry(None)


# ---- Worker dataclass ---------------------------------------------


def test_worker_dataclass_exposes_voice_and_probe() -> None:
    w = Worker(
        worker_id="tts-a",
        role="tts",
        endpoint_url="http://x",
        vram_gb=24,
        voice_id="narrator_male_1",
    )
    assert w.voice_id == "narrator_male_1"
    assert w.last_probe is None


# ---- FastAPI endpoint tests ------------------------------------------


@pytest.fixture
def http_client():  # type: ignore[no-untyped-def]
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from playground import router as playground_router

    # Install a fresh registry so endpoint tests don't leak state.
    fresh = WorkerRegistry()
    set_default_registry(fresh)
    try:
        app = FastAPI()
        app.include_router(playground_router)
        yield TestClient(app)
    finally:
        set_default_registry(None)


def test_endpoint_register_then_list_workers(http_client) -> None:  # type: ignore[no-untyped-def]
    resp = http_client.post(
        "/playground/workers",
        json={
            "worker_id": "tts-a3f",
            "role": "tts",
            "endpoint_url": "http://10.0.0.5:8080",
            "vram_gb": 24,
            "voice_id": "narrator_male_1",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["worker_id"] == "tts-a3f"
    assert body["voice_id"] == "narrator_male_1"
    assert body["stale"] is False
    assert body["last_probe"] is None

    listed = http_client.get("/playground/workers").json()
    assert listed["total"] == 1
    assert listed["by_role"] == {"tts": 1, "ltx_render": 0, "assembly": 0}
    assert listed["workers"][0]["worker_id"] == "tts-a3f"


def test_endpoint_list_filters_by_role(http_client) -> None:  # type: ignore[no-untyped-def]
    for req in (
        {"worker_id": "tts-a", "role": "tts", "endpoint_url": "http://a", "vram_gb": 24},
        {
            "worker_id": "ltx-b",
            "role": "ltx_render",
            "endpoint_url": "http://b",
            "vram_gb": 80,
        },
    ):
        assert http_client.post("/playground/workers", json=req).status_code == 200
    only_ltx = http_client.get("/playground/workers?role=ltx_render").json()
    assert only_ltx["total"] == 1
    assert only_ltx["workers"][0]["worker_id"] == "ltx-b"


def test_endpoint_list_rejects_unknown_role(http_client) -> None:  # type: ignore[no-untyped-def]
    resp = http_client.get("/playground/workers?role=captioning")
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "unknown_role"


def test_endpoint_register_rejects_duplicate_worker(http_client) -> None:  # type: ignore[no-untyped-def]
    payload = {
        "worker_id": "ltx-a",
        "role": "ltx_render",
        "endpoint_url": "http://a",
        "vram_gb": 48,
    }
    assert http_client.post("/playground/workers", json=payload).status_code == 200
    dup = http_client.post("/playground/workers", json=payload)
    assert dup.status_code == 409
    assert dup.json()["detail"]["reason"] == "duplicate_worker"


def test_endpoint_register_rejects_voice_on_non_tts(http_client) -> None:  # type: ignore[no-untyped-def]
    resp = http_client.post(
        "/playground/workers",
        json={
            "worker_id": "ltx-a",
            "role": "ltx_render",
            "endpoint_url": "http://a",
            "vram_gb": 48,
            "voice_id": "narrator_male_1",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "voice_on_non_tts_worker"


def test_endpoint_register_rejects_voice_already_pinned(
    http_client,  # type: ignore[no-untyped-def]
) -> None:
    first = http_client.post(
        "/playground/workers",
        json={
            "worker_id": "tts-a",
            "role": "tts",
            "endpoint_url": "http://a",
            "vram_gb": 24,
            "voice_id": "narrator_male_1",
        },
    )
    assert first.status_code == 200
    conflict = http_client.post(
        "/playground/workers",
        json={
            "worker_id": "tts-b",
            "role": "tts",
            "endpoint_url": "http://b",
            "vram_gb": 24,
            "voice_id": "narrator_male_1",
        },
    )
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["reason"] == "voice_already_pinned"
    assert detail["voice_id"] == "narrator_male_1"
    assert detail["other_worker_id"] == "tts-a"


def test_endpoint_heartbeat_updates_free_vram(http_client) -> None:  # type: ignore[no-untyped-def]
    http_client.post(
        "/playground/workers",
        json={
            "worker_id": "ltx-a",
            "role": "ltx_render",
            "endpoint_url": "http://a",
            "vram_gb": 48,
        },
    )
    hb = http_client.post(
        "/playground/workers/ltx-a/heartbeat", json={"free_vram_gb": 45}
    )
    assert hb.status_code == 200
    body = hb.json()
    assert body["last_probe"]["free_gb"] == 45
    assert body["last_probe"]["total_gb"] == 48


def test_endpoint_heartbeat_unknown_worker_is_404(http_client) -> None:  # type: ignore[no-untyped-def]
    resp = http_client.post("/playground/workers/ghost/heartbeat", json={})
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason"] == "worker_not_found"


def test_endpoint_pin_voice_post_registration(http_client) -> None:  # type: ignore[no-untyped-def]
    http_client.post(
        "/playground/workers",
        json={
            "worker_id": "tts-a",
            "role": "tts",
            "endpoint_url": "http://a",
            "vram_gb": 24,
        },
    )
    resp = http_client.post(
        "/playground/workers/tts-a/voice",
        json={"voice_id": "narrator_female_2"},
    )
    assert resp.status_code == 200
    assert resp.json()["voice_id"] == "narrator_female_2"


def test_endpoint_unregister_then_404s(http_client) -> None:  # type: ignore[no-untyped-def]
    http_client.post(
        "/playground/workers",
        json={
            "worker_id": "ltx-a",
            "role": "ltx_render",
            "endpoint_url": "http://a",
            "vram_gb": 48,
        },
    )
    resp = http_client.delete("/playground/workers/ltx-a")
    assert resp.status_code == 200
    assert resp.json() == {"worker_id": "ltx-a", "unregistered": True}
    # Second delete is 404.
    gone = http_client.delete("/playground/workers/ltx-a")
    assert gone.status_code == 404
