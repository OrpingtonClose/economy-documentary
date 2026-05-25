"""Video engine abstraction.

The :class:`VideoEngine` protocol is what the FastAPI surface calls.
The production implementation wraps LTX-Video 2.3 and loads weights
lazily inside the Vast.ai VM's CUDA runtime — see
``scripts/ltx_video_worker_bootstrap.sh`` for the weight-pull step.

Unit tests use :class:`StubVideoEngine`, a deterministic in-memory
engine that emits a minimal valid ISO-BMFF (MP4) byte sequence whose
duration equals the requested duration, so the FastAPI, registry, and
bump-middleware layers can be covered without any GPU or model weights
in CI.
"""

from __future__ import annotations

import hashlib
import logging
import struct
import time
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


DEFAULT_FPS: int = 24
DEFAULT_WIDTH: int = 1280
DEFAULT_HEIGHT: int = 720
MIN_DURATION_S: float = 0.1
MAX_DURATION_S: float = 30.0


class VideoEngineError(Exception):
    """Raised when rendering fails in a way the worker must surface."""


@dataclass(frozen=True)
class RenderRequest:
    """Inputs to a single ``POST /`` call.

    Attributes:
        prompt: The scene description the engine conditions on.
            Must be non-empty.
        duration_s: Target clip duration in seconds. Clamped to
            ``[MIN_DURATION_S, MAX_DURATION_S]`` by the engine.
        width: Output width in pixels.
        height: Output height in pixels.
        fps: Output framerate.
        style: Optional style hint (e.g. ``documentary``,
            ``cinematic``). Engine-specific.
        seed: Optional deterministic seed. Engines that don't support
            seeding ignore this silently.
        negative_prompt: Optional negative-guidance string.
    """

    prompt: str
    duration_s: float
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    fps: int = DEFAULT_FPS
    style: str | None = None
    seed: int | None = None
    negative_prompt: str | None = None


@dataclass(frozen=True)
class RenderResult:
    """Output of a successful render.

    Attributes:
        mp4_bytes: Raw MP4 payload (ISO-BMFF container). In tests this
            is a deterministic stub header, not playable video.
        duration_s: Actual clip duration in seconds.
        width: Output width in pixels.
        height: Output height in pixels.
        fps: Output framerate.
        engine: Human-readable engine identifier (e.g.
            ``ltx-video-2.3`` or ``stub``).
    """

    mp4_bytes: bytes
    duration_s: float
    width: int
    height: int
    fps: int
    engine: str


class VideoEngine(Protocol):
    """Minimal interface every video backend implements."""

    @property
    def engine_id(self) -> str:
        """Short identifier, e.g. ``ltx-video-2.3`` or ``stub``."""
        ...

    def render(self, request: RenderRequest) -> RenderResult:
        """Render one clip, blocking until complete."""
        ...


@dataclass
class StubVideoEngine:
    """Deterministic in-memory engine used by unit tests.

    Emits a tiny but structurally-valid ISO-BMFF byte sequence (an
    ``ftyp`` box followed by a synthetic ``mdat`` box whose size
    scales with ``duration_s``). The payload is not playable — it
    exists purely so downstream code that key off MIME sniffing,
    file-size telemetry, or round-trip checksums has something real
    to work with in CI.

    Attributes:
        width: Advertised output width.
        height: Advertised output height.
        fps: Advertised output framerate.
        bytes_per_second: Synthetic payload rate. Default gives a
            ~100 KB stub for a 1 s clip, which is roomy enough for
            any reasonable request body but cheap enough for CI.
        simulated_latency_s: Optional sleep before returning, so a
            bump-on-request test can observe the bump during render.
    """

    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    fps: int = DEFAULT_FPS
    bytes_per_second: int = 100_000
    simulated_latency_s: float = 0.0

    @property
    def engine_id(self) -> str:
        return "stub"

    def render(self, request: RenderRequest) -> RenderResult:
        if not request.prompt.strip():
            raise VideoEngineError("prompt must be non-empty")
        if request.duration_s <= 0:
            raise VideoEngineError("duration_s must be > 0")
        if request.width <= 0 or request.height <= 0:
            raise VideoEngineError("width and height must be > 0")
        if request.fps <= 0:
            raise VideoEngineError("fps must be > 0")

        if self.simulated_latency_s > 0:
            time.sleep(self.simulated_latency_s)

        clamped_duration = min(
            max(request.duration_s, MIN_DURATION_S), MAX_DURATION_S
        )
        mp4_bytes = _build_stub_mp4(
            prompt=request.prompt,
            duration_s=clamped_duration,
            bytes_per_second=self.bytes_per_second,
        )

        logger.debug(
            "prompt_chars=<%d>, duration_s=<%.3f>, bytes=<%d> | stub render ok",
            len(request.prompt),
            clamped_duration,
            len(mp4_bytes),
        )
        return RenderResult(
            mp4_bytes=mp4_bytes,
            duration_s=clamped_duration,
            width=request.width,
            height=request.height,
            fps=request.fps,
            engine=self.engine_id,
        )


def _build_stub_mp4(
    *, prompt: str, duration_s: float, bytes_per_second: int
) -> bytes:
    """Build a deterministic ISO-BMFF-shaped stub payload.

    The result starts with a valid ``ftyp`` box (``isom`` major brand,
    ``mp41`` compatible brand) followed by a single ``mdat`` box whose
    payload is a SHA-256 stream of the prompt string repeated to fill
    the requested byte count. Deterministic for a given
    ``(prompt, duration_s, bytes_per_second)`` triple.
    """

    ftyp = _make_box(
        box_type=b"ftyp",
        payload=(
            b"isom"  # major brand
            + struct.pack(">I", 512)  # minor version
            + b"isom"  # compat brand 1
            + b"mp41"  # compat brand 2
        ),
    )

    payload_size = max(int(duration_s * bytes_per_second), 64)
    digest = hashlib.sha256(prompt.encode("utf-8")).digest()
    repeats = (payload_size + len(digest) - 1) // len(digest)
    mdat_payload = (digest * repeats)[:payload_size]

    mdat = _make_box(box_type=b"mdat", payload=mdat_payload)
    return ftyp + mdat


def _make_box(*, box_type: bytes, payload: bytes) -> bytes:
    """Wrap ``payload`` in an ISO-BMFF box with the given 4-byte type."""
    size = 8 + len(payload)
    return struct.pack(">I", size) + box_type + payload
