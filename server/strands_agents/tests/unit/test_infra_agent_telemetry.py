"""Unit tests for the peak-tracking telemetry."""

from __future__ import annotations

import pytest

from strands_agents.infra_agent.telemetry import ResourceTelemetry, TelemetrySnapshot


class _ProberStub:
    """Callable stub returning a scripted sequence of readings."""

    def __init__(self, readings: list[tuple[int, int] | None]) -> None:
        self._readings = list(readings)
        self.calls = 0

    def __call__(self, *_args: object) -> tuple[int, int] | None:
        self.calls += 1
        if not self._readings:
            raise AssertionError("telemetry prober called more times than scripted")
        return self._readings.pop(0)


def test_snapshot_records_vram_and_disk_from_probes() -> None:
    vram = _ProberStub([(80, 20)])
    disk = _ProberStub([(500, 120)])
    telemetry = ResourceTelemetry(
        vram_prober=vram,
        disk_prober=disk,  # type: ignore[arg-type]
        disk_path="/data",
    )

    snap = telemetry.sample()

    assert snap.vram_total_gb == 80
    assert snap.vram_used_gb == 20
    assert snap.vram_peak_gb == 20
    assert snap.disk_total_gb == 500
    assert snap.disk_used_gb == 120
    assert snap.disk_peak_gb == 120


def test_peak_tracks_max_across_samples() -> None:
    vram = _ProberStub([(80, 10), (80, 35), (80, 15)])
    disk = _ProberStub([(500, 20), (500, 50), (500, 40)])
    telemetry = ResourceTelemetry(
        vram_prober=vram, disk_prober=disk  # type: ignore[arg-type]
    )

    first = telemetry.sample()
    assert first.vram_peak_gb == 10
    assert first.disk_peak_gb == 20

    second = telemetry.sample()
    assert second.vram_peak_gb == 35
    assert second.disk_peak_gb == 50

    third = telemetry.sample()
    assert third.vram_used_gb == 15
    assert third.vram_peak_gb == 35  # peak retained
    assert third.disk_used_gb == 40
    assert third.disk_peak_gb == 50  # peak retained


def test_vram_prober_returning_none_means_no_gpu() -> None:
    vram = _ProberStub([None])
    disk = _ProberStub([(500, 10)])
    telemetry = ResourceTelemetry(
        vram_prober=vram, disk_prober=disk  # type: ignore[arg-type]
    )

    snap = telemetry.sample()

    assert snap.vram_total_gb is None
    assert snap.vram_used_gb is None
    assert snap.vram_peak_gb is None


def test_snapshot_is_a_frozen_dataclass() -> None:
    snap = TelemetrySnapshot(
        vram_total_gb=None,
        vram_used_gb=None,
        vram_peak_gb=None,
        disk_total_gb=100,
        disk_used_gb=10,
        disk_peak_gb=10,
    )
    with pytest.raises(AttributeError):
        snap.disk_used_gb = 999  # type: ignore[misc]
