"""Resource telemetry — peak VRAM + peak disk since VM boot.

The goal is to feed concrete numbers into the gpu-sizing ledger on
destruction. Probes are injected as callables so unit tests can supply
deterministic values without touching ``nvidia-smi`` or the filesystem.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Callable returning ``(total_gb, used_gb)`` for the GPU. Units are
#: GiB. Returning ``None`` (instead of a tuple) means the GPU is
#: unavailable and probes are skipped — the agent still runs for
#: non-GPU VMs like the Langfuse controller.
VramProber = Callable[[], "tuple[int, int] | None"]

#: Callable returning ``(total_gb, used_gb)`` for a filesystem path.
DiskProber = Callable[[str], "tuple[int, int]"]


@dataclass(frozen=True)
class TelemetrySnapshot:
    """A point-in-time view of VRAM + disk.

    Attributes:
        vram_total_gb: GPU total memory in GiB, or ``None`` if no GPU.
        vram_used_gb: GPU used memory in GiB, or ``None`` if no GPU.
        vram_peak_gb: Highest ``vram_used_gb`` seen since boot.
        disk_total_gb: Filesystem total in GiB at the monitored path.
        disk_used_gb: Filesystem used in GiB at the monitored path.
        disk_peak_gb: Highest ``disk_used_gb`` seen since boot.
    """

    vram_total_gb: int | None
    vram_used_gb: int | None
    vram_peak_gb: int | None
    disk_total_gb: int
    disk_used_gb: int
    disk_peak_gb: int


@dataclass
class ResourceTelemetry:
    """Stateful peak-tracker around injectable probes.

    Typical use::

        telemetry = ResourceTelemetry(
            vram_prober=nvidia_smi_prober,
            disk_prober=shutil_disk_prober,
            disk_path="/",
        )
        snapshot = telemetry.sample()

    Thread-safe. :func:`sample` may be called from both the HTTP handler
    thread (``/infra/status``) and the runner's ticker.
    """

    vram_prober: VramProber
    disk_prober: DiskProber
    disk_path: str = "/"
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _vram_peak: int | None = field(default=None)
    _disk_peak: int = field(default=0)

    def sample(self) -> TelemetrySnapshot:
        """Probe current usage and update peaks. Returns a snapshot."""
        vram = self.vram_prober()
        disk = self.disk_prober(self.disk_path)

        with self._lock:
            if vram is not None:
                _vram_total, vram_used = vram
                if self._vram_peak is None or vram_used > self._vram_peak:
                    self._vram_peak = vram_used
            vram_peak = self._vram_peak

            disk_total, disk_used = disk
            if disk_used > self._disk_peak:
                self._disk_peak = disk_used
            disk_peak = self._disk_peak

        if vram is None:
            return TelemetrySnapshot(
                vram_total_gb=None,
                vram_used_gb=None,
                vram_peak_gb=vram_peak,
                disk_total_gb=disk_total,
                disk_used_gb=disk_used,
                disk_peak_gb=disk_peak,
            )

        vram_total, vram_used = vram
        return TelemetrySnapshot(
            vram_total_gb=vram_total,
            vram_used_gb=vram_used,
            vram_peak_gb=vram_peak,
            disk_total_gb=disk_total,
            disk_used_gb=disk_used,
            disk_peak_gb=disk_peak,
        )


def shutil_disk_prober(path: str) -> tuple[int, int]:
    """Default disk prober using :func:`shutil.disk_usage`.

    Returns ``(total_gib, used_gib)`` rounded down to GiB.
    """
    usage = shutil.disk_usage(str(Path(path)))
    total_gb = int(usage.total // (1024**3))
    used_gb = int((usage.total - usage.free) // (1024**3))
    return total_gb, used_gb


def nvidia_smi_prober() -> tuple[int, int] | None:
    """Default VRAM prober via ``nvidia-smi``.

    Returns ``(total_mem_gib, used_mem_gib)`` for device 0, or ``None``
    if ``nvidia-smi`` is absent or errors. Not used by unit tests (those
    inject a deterministic stub) — only wired up on the real worker VM.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total,memory.used",
                "--format=csv,noheader,nounits",
                "--id=0",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.debug("error=<%s> | nvidia-smi probe failed", exc)
        return None

    line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not line:
        logger.debug("stdout=<%s> | nvidia-smi empty output", result.stdout)
        return None

    parts = [p.strip() for p in line.split(",")]
    if len(parts) != 2:
        logger.debug("line=<%s> | nvidia-smi unexpected columns", line)
        return None

    try:
        total_mib = int(parts[0])
        used_mib = int(parts[1])
    except ValueError:
        logger.debug("line=<%s> | nvidia-smi non-integer columns", line)
        return None

    total_gb = total_mib // 1024
    used_gb = used_mib // 1024
    return total_gb, used_gb
