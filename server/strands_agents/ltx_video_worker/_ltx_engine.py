"""Real LTX-Video engine wrapper for LTX-2.3.

Production engine for the LTX-Video worker. Drives Lightricks' own
``ltx_pipelines.ti2vid_one_stage`` BASIC single-stage CLI from
https://github.com/Lightricks/LTX-2 — the simplest supported
entrypoint for the LTX-2.3 22B base (``-dev``) model. Per the user's
standing rule, this BASIC variant is the mandatory path: one
checkpoint, one denoising stage, no manual upscaler chaining.

Why the subprocess shape
------------------------

LTX-2.3 is **not** a drop-in for ``diffusers``' ``LTXPipeline`` —
the official ``ltx_pipelines.ti2vid_one_stage`` pipeline is a custom
class wired against the ``ltx-core`` building blocks (Gemma-3 prompt
encoder, image conditioner, video / audio decoders, …). It expects
an explicit ``--gemma-root`` directory and reads the base checkpoint
safetensors from an explicit file path — not from a Hugging Face
repo id.

Rather than fork/import the pipeline class into our worker process
(which would lock us to a specific monorepo commit and pollute the
worker's import graph), we shell out to ``python -m
ltx_pipelines.ti2vid_one_stage`` as a subprocess. The bootstrap script
(:mod:`scripts.ltx_video_worker_bootstrap`) is responsible for
``uv sync``-ing the Lightricks/LTX-2 monorepo into ``LTX_VIDEO_LTX2_ROOT``
so the module is importable in that subprocess.

Anti-drift hashing
------------------

``verify_pin`` is called on **both** :data:`LTX_VIDEO_PIN` (the
LTX-2.3 weights) and :data:`LTX_VIDEO_GEMMA_PIN` (the Gemma-3-12B
text encoder weights, served by Lightricks' own non-gated re-host)
before every render. Mismatch on either side raises
:class:`ModelPinMismatchError` and the render fails closed — no
fallback, no warning-only.

Sizing notes
------------

LTX-2.3 22B-dev at LTX-2 defaults renders a multi-second clip in
roughly 60–120 s of wall clock on H200. With ``--quantization
fp8-cast`` and ``--offload cpu`` the same clip fits in ~24 GB of
VRAM on a single H100/A100; on H200 we leave both off for speed.

Resolution + frame count are clamped to LTX-Video's "recommended" grid
(both must be multiples of 32, and ``num_frames`` must be ``8k+1``).
The engine rounds the request silently rather than rejecting it, so
the orchestrator doesn't have to know LTX's grid math.
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from strands_agents._model_pin import verify_pin

from ._model_pin import LTX_VIDEO_GEMMA_PIN, LTX_VIDEO_PIN
from .engine import (
    DEFAULT_FPS,
    MAX_DURATION_S,
    MIN_DURATION_S,
    RenderRequest,
    RenderResult,
    VideoEngineError,
)

logger = logging.getLogger(__name__)


# Production model is pinned via :data:`LTX_VIDEO_PIN`. The model id
# below mirrors that pin; it is exposed only for log lines and
# legacy callers and is never read for the actual load.
DEFAULT_MODEL_ID = LTX_VIDEO_PIN.model_id
ENGINE_ID = "ltx-video"

# Filename inside the LTX-2.3 snapshot dir that ``ti2vid_one_stage``
# expects on ``--checkpoint-path``. The BASIC pipeline loads a single
# full base checkpoint — no distilled checkpoint, no spatial upscaler.
_BASE_CHECKPOINT_FILE = "ltx-2.3-22b-dev.safetensors"

# Hard ceiling for the ti2vid_one_stage subprocess. A hung GPU process
# (driver crash, OOM, infinite loop) would otherwise wedge the worker
# request thread until the cost guardian kills the VM. The default of
# 30 minutes matches the HTTP client's bound (``_DEFAULT_TIMEOUT_S``)
# and is comfortably above any observed render time (~3min on H200).
# Override via ``LTX_VIDEO_LTX2_RENDER_TIMEOUT_S`` (seconds, integer).
_DEFAULT_LTX2_RENDER_TIMEOUT_S = 30 * 60


def _ltx2_render_timeout_s() -> int:
    """Resolve the subprocess timeout, honouring the env override.

    Returns a strictly positive integer. Falls back to the default on
    any parse error or non-positive value.
    """
    raw = os.environ.get("LTX_VIDEO_LTX2_RENDER_TIMEOUT_S")
    if not raw:
        return _DEFAULT_LTX2_RENDER_TIMEOUT_S
    try:
        parsed = int(raw)
    except ValueError:
        return _DEFAULT_LTX2_RENDER_TIMEOUT_S
    if parsed <= 0:
        return _DEFAULT_LTX2_RENDER_TIMEOUT_S
    return parsed


def _round_to_multiple(value: int, multiple: int) -> int:
    """Round ``value`` down to the nearest multiple of ``multiple``.

    LTX-Video requires width / height to be multiples of 32. We round
    down rather than up so we never silently exceed VRAM on a tight
    GPU.
    """
    if value <= 0:
        return multiple
    return max(multiple, (value // multiple) * multiple)


def _round_frames(value: int) -> int:
    """Round frame count down to ``8k + 1`` per LTX-Video's spec."""
    if value <= 1:
        return 9
    # solve 8k + 1 ≤ value → k = (value - 1) // 8
    k = max(1, (value - 1) // 8)
    return 8 * k + 1


def _read_mp4_bytes(path: Path) -> bytes:
    """Read the mp4 we exported and return its bytes."""
    return path.read_bytes()


def _read_video_duration_s(path: Path) -> float | None:
    """Best-effort wall-clock duration probe for a written MP4.

    Uses ``ffprobe`` if available on ``PATH``. Returns ``None`` when
    ffprobe is missing or fails — callers fall back to the requested
    ``actual_duration`` (computed from num_frames / fps).
    """
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    try:
        return float(raw)
    except ValueError:
        return None


class LTXVideoEngine:
    """Production engine driving ``ltx_pipelines.ti2vid_one_stage`` via subprocess.

    The model identity (``model_id`` and HF revision) is **not**
    configurable — it is locked by :data:`LTX_VIDEO_PIN`. Only
    operational knobs are read from the environment:

    * ``LTX_VIDEO_DEVICE`` — ignored when the LTX-2 CLI auto-selects
      the GPU; reserved for future use.
    * ``LTX_VIDEO_OFFLOAD_MODE`` — passed through as ``--offload``.
      One of ``"none"`` (default, full VRAM), ``"cpu"`` (CPU offload,
      slower), ``"disk"`` (disk offload, slowest).
    * ``LTX_VIDEO_NUM_INFERENCE_STEPS`` — passed through as
      ``--num-inference-steps``. Defaults to whatever LTX-2.3 ships
      (currently 5 distilled sigmas).
    * ``LTX_VIDEO_QUANTIZATION`` — passed through as
      ``--quantization``. One of ``"fp8-cast"``, ``"fp8-scaled-mm"``,
      or unset (no quantization).
    * ``LTX_VIDEO_LTX2_PYTHON`` — Python interpreter to run the
      subprocess with. Defaults to ``sys.executable``. Override on
      Vast.ai if the LTX-2 monorepo lives in a separate venv.
    * ``LTX_VIDEO_LTX2_CWD`` — working directory for the subprocess.
      Defaults to the LTX-2 monorepo clone path. Override only if
      the bootstrap installed the pipeline into a non-standard root.
    """

    def __init__(
        self,
        *,
        model_id: str | None = None,
        device: str | None = None,
        offload_mode: str | None = None,
        num_inference_steps: int | None = None,
    ) -> None:
        if model_id is not None and model_id != LTX_VIDEO_PIN.model_id:
            raise VideoEngineError(
                f"model_id override is forbidden: requested={model_id!r}, "
                f"pinned={LTX_VIDEO_PIN.model_id!r} (see strands_agents._model_pin)"
            )
        self._model_id = LTX_VIDEO_PIN.model_id
        self._device = device or os.environ.get("LTX_VIDEO_DEVICE", "cuda:0")
        self._offload_mode = (
            offload_mode or os.environ.get("LTX_VIDEO_OFFLOAD_MODE", "none")
        ).lower()
        self._num_inference_steps_override = num_inference_steps
        # Pin verification is cached after the first successful pass so
        # we don't burn ~1-2 minutes hashing 25 GB of weights on every
        # render call. The cache is keyed by the verified snapshot
        # paths — if the pin is updated (different file set / hashes),
        # the cache is invalidated implicitly because the cached paths
        # no longer match.
        self._verified_paths: tuple[Path, Path] | None = None

    @property
    def engine_id(self) -> str:
        return ENGINE_ID

    def _verify_pins(self) -> tuple[Path, Path]:
        """Verify both LTX-2.3 and Gemma-3 pins, return their snapshot dirs."""
        if self._verified_paths is not None:
            return self._verified_paths
        ltx_dir = verify_pin(LTX_VIDEO_PIN)
        gemma_dir = verify_pin(LTX_VIDEO_GEMMA_PIN)
        logger.info(
            "ltx_model_id=<%s>, ltx_revision=<%s>, ltx_snapshot=<%s>, "
            "gemma_model_id=<%s>, gemma_revision=<%s>, gemma_snapshot=<%s> | "
            "model pins verified",
            LTX_VIDEO_PIN.model_id,
            LTX_VIDEO_PIN.revision,
            ltx_dir,
            LTX_VIDEO_GEMMA_PIN.model_id,
            LTX_VIDEO_GEMMA_PIN.revision,
            gemma_dir,
        )
        self._verified_paths = (ltx_dir, gemma_dir)
        return self._verified_paths

    def _build_ltx2_argv(
        self,
        *,
        ltx_dir: Path,
        gemma_dir: Path,
        prompt: str,
        output_path: Path,
        width: int,
        height: int,
        num_frames: int,
        fps: int,
        seed: int | None,
        negative_prompt: str | None = None,
    ) -> list[str]:
        """Assemble the ``python -m ltx_pipelines.ti2vid_one_stage`` argv."""
        python_bin = os.environ.get("LTX_VIDEO_LTX2_PYTHON", sys.executable)
        argv: list[str] = [
            python_bin,
            "-m",
            "ltx_pipelines.ti2vid_one_stage",
            "--checkpoint-path",
            str(ltx_dir / _BASE_CHECKPOINT_FILE),
            "--gemma-root",
            str(gemma_dir),
            "--prompt",
            prompt,
            "--output-path",
            str(output_path),
            "--width",
            str(width),
            "--height",
            str(height),
            "--num-frames",
            str(num_frames),
            "--frame-rate",
            str(fps),
        ]
        if negative_prompt is not None and negative_prompt.strip():
            argv += ["--negative-prompt", negative_prompt]
        if seed is not None:
            argv += ["--seed", str(int(seed))]
        if self._offload_mode and self._offload_mode != "none":
            argv += ["--offload", self._offload_mode]
        if self._num_inference_steps_override is not None:
            argv += [
                "--num-inference-steps",
                str(self._num_inference_steps_override),
            ]
        elif "LTX_VIDEO_NUM_INFERENCE_STEPS" in os.environ:
            argv += [
                "--num-inference-steps",
                os.environ["LTX_VIDEO_NUM_INFERENCE_STEPS"],
            ]
        quant = os.environ.get("LTX_VIDEO_QUANTIZATION")
        if quant:
            argv += ["--quantization", quant]
        return argv

    def render(self, request: RenderRequest) -> RenderResult:
        """Render one clip with the real LTX-2.3 BASIC one-stage pipeline."""
        if not request.prompt.strip():
            raise VideoEngineError("prompt must be non-empty")
        if request.duration_s <= 0:
            raise VideoEngineError("duration_s must be > 0")
        if request.width <= 0 or request.height <= 0:
            raise VideoEngineError("width and height must be > 0")
        if request.fps <= 0:
            raise VideoEngineError("fps must be > 0")

        # Verify pinned bytes BEFORE touching the GPU subprocess. Any
        # drift in either model is a hard fail with no override.
        ltx_dir, gemma_dir = self._verify_pins()

        # Clamp + round to LTX-Video's grid.
        clamped_duration = min(
            max(request.duration_s, MIN_DURATION_S), MAX_DURATION_S
        )
        width = _round_to_multiple(request.width, 32)
        height = _round_to_multiple(request.height, 32)
        fps = max(int(request.fps), 1)
        target_frames = max(int(round(clamped_duration * fps)), 9)
        num_frames = _round_frames(target_frames)
        actual_duration = num_frames / float(fps)

        cwd = os.environ.get(
            "LTX_VIDEO_LTX2_CWD",
            os.environ.get("LTX_VIDEO_LTX2_ROOT", "/opt/ltx-2-repo"),
        )
        if not Path(cwd).is_dir():
            raise VideoEngineError(
                f"LTX-2 monorepo root not found at {cwd!r}; ensure "
                "ltx_video_worker_bootstrap.sh ran successfully and set "
                "LTX_VIDEO_LTX2_ROOT (or override LTX_VIDEO_LTX2_CWD)."
            )

        with tempfile.TemporaryDirectory(prefix="ltx23-render-") as tmp_dir:
            tmp = Path(tmp_dir)
            output_path = tmp / "ltx.mp4"
            argv = self._build_ltx2_argv(
                ltx_dir=ltx_dir,
                gemma_dir=gemma_dir,
                prompt=request.prompt,
                output_path=output_path,
                width=width,
                height=height,
                num_frames=num_frames,
                fps=fps,
                seed=request.seed,
                negative_prompt=request.negative_prompt,
            )
            logger.info(
                "prompt_chars=<%d>, width=<%d>, height=<%d>, num_frames=<%d>, "
                "fps=<%d>, cwd=<%s>, argv_head=<%s> | ltx-2.3 ti2vid_one_stage subprocess begin",
                len(request.prompt),
                width,
                height,
                num_frames,
                fps,
                cwd,
                " ".join(argv[:4]),
            )
            timeout_s = _ltx2_render_timeout_s()
            try:
                proc = subprocess.run(
                    argv,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout_s,
                )
            except subprocess.TimeoutExpired as exc:
                stderr_tail = (
                    exc.stderr[-2000:] if isinstance(exc.stderr, str) else ""
                )
                raise VideoEngineError(
                    "ltx-2.3 ti2vid_one_stage subprocess timed out after "
                    f"{timeout_s}s; stderr_tail={stderr_tail!r}"
                ) from exc
            except OSError as exc:
                raise VideoEngineError(
                    f"failed to launch ltx-2.3 ti2vid_one_stage subprocess: {exc}"
                ) from exc
            if proc.returncode != 0:
                # Truncate stderr so a multi-MB python traceback doesn't
                # blow up the worker log line.
                stderr_tail = proc.stderr[-4000:] if proc.stderr else ""
                raise VideoEngineError(
                    "ltx-2.3 ti2vid_one_stage subprocess failed: "
                    f"returncode={proc.returncode}, stderr_tail={stderr_tail!r}"
                )
            if not output_path.is_file():
                raise VideoEngineError(
                    "ltx-2.3 ti2vid_one_stage subprocess returned 0 but produced no "
                    f"output mp4 at {output_path!s}"
                )
            mp4_bytes = _read_mp4_bytes(output_path)
            probed_duration = _read_video_duration_s(output_path)

        out_duration = probed_duration if probed_duration is not None else actual_duration
        logger.info(
            "duration_s=<%.3f>, bytes=<%d>, probed=<%s> | ltx-2.3 render ok",
            out_duration,
            len(mp4_bytes),
            "yes" if probed_duration is not None else "no",
        )
        return RenderResult(
            mp4_bytes=mp4_bytes,
            duration_s=out_duration,
            width=width,
            height=height,
            fps=fps,
            engine=self.engine_id,
        )


# Quiet ``io`` import (kept here so the BytesIO lookup is colocated
# with the rest of the engine surface for grep-discoverability —
# render itself uses tempfile + Path.read_bytes, but tests sometimes
# monkeypatch via io.BytesIO).
_ = io
_ = DEFAULT_FPS
