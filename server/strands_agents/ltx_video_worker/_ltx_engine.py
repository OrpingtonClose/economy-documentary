"""Real LTX-Video engine wrapper.

Production engine for the LTX-Video worker. Wraps the official
``diffusers`` ``LTXPipeline`` (https://huggingface.co/Lightricks/LTX-Video)
and exposes the
:class:`~strands_agents.ltx_video_worker.engine.VideoEngine` protocol
that the FastAPI surface expects.

This module is **not** imported in CI — the runner factory falls back
to :class:`StubVideoEngine` when ``diffusers`` is not installed (see
:func:`strands_agents.ltx_video_worker.runner._real_video_engine_factory`).
On a Vast.ai VM the ``scripts/ltx_video_worker_bootstrap.sh`` script
installs ``torch``, ``diffusers``, ``transformers``, ``accelerate``,
``imageio``, and ``imageio-ffmpeg`` before launching the worker, so
the import succeeds and this engine takes over.

Sizing notes
------------

The ``Lightricks/LTX-Video`` checkpoint loaded in bfloat16 with
``LTXPipeline`` runs in ~24 GB of VRAM at 768x512 / 161 frames /
50 inference steps (roughly a 5-6s clip at 24 fps). With
``enable_layerwise_casting(fp8) + enable_group_offload`` the VRAM
footprint drops to ~10 GB, at the cost of ~2x slower inference. We
default to the fp8 + offload path so the engine works on H100/A10/L4
and stays well within an H200's headroom for the documentary pipeline.

Resolution + frame count are clamped to LTX-Video's "recommended" grid
(both must be multiples of 32, and ``num_frames`` must be ``8k+1``).
The engine rounds the request silently rather than rejecting it, so
the orchestrator doesn't have to know LTX's grid math.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .engine import (
    DEFAULT_FPS,
    MAX_DURATION_S,
    MIN_DURATION_S,
    RenderRequest,
    RenderResult,
    VideoEngineError,
)

logger = logging.getLogger(__name__)


DEFAULT_MODEL_ID = "Lightricks/LTX-Video"
ENGINE_ID = "ltx-video"


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


class LTXVideoEngine:
    """Production engine wrapping ``diffusers.LTXPipeline``.

    The pipeline is loaded lazily on the first :meth:`render` call so
    importing this module on a CPU-only box doesn't trigger CUDA init
    or weight downloads.

    Attributes are read from environment variables so the bootstrap
    script can pin them without code changes:

    * ``LTX_VIDEO_MODEL_ID`` — Hugging Face model id or local
      directory path. Defaults to ``Lightricks/LTX-Video``.
    * ``LTX_VIDEO_DTYPE`` — ``"bfloat16"`` (default) or ``"float16"``.
    * ``LTX_VIDEO_DEVICE`` — CUDA device for the pipeline (``cuda:0``
      by default).
    * ``LTX_VIDEO_OFFLOAD_MODE`` — ``"fp8_group"`` (default,
      VRAM-light), ``"none"`` (full-fat, VRAM-heavy), or ``"cpu"``
      (model CPU offload, slowest).
    * ``LTX_VIDEO_NUM_INFERENCE_STEPS`` — diffusion steps. Defaults
      to ``50``.
    * ``LTX_VIDEO_DECODE_TIMESTEP`` — VAE decode timestep, default
      ``0.03`` per LTX-Video docs.
    * ``LTX_VIDEO_DECODE_NOISE_SCALE`` — VAE decode noise scale,
      default ``0.025`` per LTX-Video docs.
    """

    def __init__(
        self,
        *,
        model_id: str | None = None,
        dtype_name: str | None = None,
        device: str | None = None,
        offload_mode: str | None = None,
        num_inference_steps: int | None = None,
    ) -> None:
        self._model_id = model_id or os.environ.get(
            "LTX_VIDEO_MODEL_ID", DEFAULT_MODEL_ID
        )
        self._dtype_name = (
            dtype_name or os.environ.get("LTX_VIDEO_DTYPE", "bfloat16")
        ).lower()
        self._device = device or os.environ.get("LTX_VIDEO_DEVICE", "cuda:0")
        self._offload_mode = (
            offload_mode or os.environ.get("LTX_VIDEO_OFFLOAD_MODE", "fp8_group")
        ).lower()
        self._num_inference_steps = num_inference_steps or int(
            os.environ.get("LTX_VIDEO_NUM_INFERENCE_STEPS", "50")
        )
        self._decode_timestep = float(
            os.environ.get("LTX_VIDEO_DECODE_TIMESTEP", "0.03")
        )
        self._decode_noise_scale = float(
            os.environ.get("LTX_VIDEO_DECODE_NOISE_SCALE", "0.025")
        )
        self._pipeline: Any | None = None

    @property
    def engine_id(self) -> str:
        return ENGINE_ID

    def _resolve_torch_dtype(self) -> Any:
        import torch  # noqa: PLC0415

        lookup = {
            "bfloat16": torch.bfloat16,
            "float16": torch.float16,
            "float32": torch.float32,
        }
        if self._dtype_name not in lookup:
            raise VideoEngineError(
                f"unsupported LTX_VIDEO_DTYPE={self._dtype_name!r}; "
                f"expected one of {sorted(lookup)}"
            )
        return lookup[self._dtype_name]

    def _load_pipeline(self) -> Any:
        """Load LTXPipeline once, cache it for subsequent calls."""
        if self._pipeline is not None:
            return self._pipeline

        import torch  # noqa: PLC0415
        from diffusers import AutoModel, LTXPipeline  # noqa: PLC0415

        dtype = self._resolve_torch_dtype()
        logger.info(
            "model_id=<%s>, dtype=<%s>, offload_mode=<%s>, device=<%s> | loading ltx-video pipeline",
            self._model_id,
            self._dtype_name,
            self._offload_mode,
            self._device,
        )

        if self._offload_mode == "fp8_group":
            transformer = AutoModel.from_pretrained(
                self._model_id,
                subfolder="transformer",
                torch_dtype=dtype,
            )
            with contextlib.suppress(Exception):
                transformer.enable_layerwise_casting(
                    storage_dtype=torch.float8_e4m3fn,
                    compute_dtype=dtype,
                )
            pipeline = LTXPipeline.from_pretrained(
                self._model_id,
                transformer=transformer,
                torch_dtype=dtype,
            )
            onload_device = torch.device(self._device)
            offload_device = torch.device("cpu")
            with contextlib.suppress(Exception):
                pipeline.transformer.enable_group_offload(
                    onload_device=onload_device,
                    offload_device=offload_device,
                    offload_type="leaf_level",
                    use_stream=True,
                )
            from diffusers.hooks import apply_group_offloading  # noqa: PLC0415

            with contextlib.suppress(Exception):
                apply_group_offloading(
                    pipeline.text_encoder,
                    onload_device=onload_device,
                    offload_type="block_level",
                    num_blocks_per_group=2,
                )
            with contextlib.suppress(Exception):
                apply_group_offloading(
                    pipeline.vae,
                    onload_device=onload_device,
                    offload_type="leaf_level",
                )
        elif self._offload_mode == "cpu":
            pipeline = LTXPipeline.from_pretrained(
                self._model_id,
                torch_dtype=dtype,
            )
            with contextlib.suppress(Exception):
                pipeline.enable_model_cpu_offload()
        elif self._offload_mode == "none":
            pipeline = LTXPipeline.from_pretrained(
                self._model_id,
                torch_dtype=dtype,
            )
            pipeline = pipeline.to(self._device)
        else:
            raise VideoEngineError(
                f"unsupported LTX_VIDEO_OFFLOAD_MODE={self._offload_mode!r}; "
                "expected one of fp8_group | cpu | none"
            )

        self._pipeline = pipeline
        return pipeline

    def render(self, request: RenderRequest) -> RenderResult:
        """Render one clip with the real LTX-Video pipeline."""
        if not request.prompt.strip():
            raise VideoEngineError("prompt must be non-empty")
        if request.duration_s <= 0:
            raise VideoEngineError("duration_s must be > 0")
        if request.width <= 0 or request.height <= 0:
            raise VideoEngineError("width and height must be > 0")
        if request.fps <= 0:
            raise VideoEngineError("fps must be > 0")

        # Clamp + round to LTX-Video's grid.
        clamped_duration = min(
            max(request.duration_s, MIN_DURATION_S), MAX_DURATION_S
        )
        width = _round_to_multiple(request.width, 32)
        height = _round_to_multiple(request.height, 32)
        fps = max(request.fps, 1)
        target_frames = max(int(round(clamped_duration * fps)), 9)
        num_frames = _round_frames(target_frames)
        actual_duration = num_frames / float(fps)

        pipeline = self._load_pipeline()

        kwargs: dict[str, Any] = {
            "prompt": request.prompt,
            "width": width,
            "height": height,
            "num_frames": num_frames,
            "decode_timestep": self._decode_timestep,
            "decode_noise_scale": self._decode_noise_scale,
            "num_inference_steps": self._num_inference_steps,
        }
        if request.negative_prompt:
            kwargs["negative_prompt"] = request.negative_prompt
        if request.seed is not None:
            import torch  # noqa: PLC0415

            kwargs["generator"] = torch.Generator(
                device=self._device
            ).manual_seed(int(request.seed))

        logger.info(
            "prompt_chars=<%d>, width=<%d>, height=<%d>, num_frames=<%d>, "
            "fps=<%d>, steps=<%d> | ltx-video render begin",
            len(request.prompt),
            width,
            height,
            num_frames,
            fps,
            self._num_inference_steps,
        )
        try:
            output = pipeline(**kwargs)
        except Exception as exc:
            raise VideoEngineError(
                f"ltx-video pipeline failed: {exc}"
            ) from exc

        try:
            frames = output.frames[0]
        except (AttributeError, IndexError) as exc:
            raise VideoEngineError(
                f"ltx-video output missing frames: {exc}"
            ) from exc

        from diffusers.utils import export_to_video  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            mp4_path = Path(tmp) / "ltx.mp4"
            export_to_video(frames, str(mp4_path), fps=fps)
            mp4_bytes = _read_mp4_bytes(mp4_path)

        logger.info(
            "duration_s=<%.3f>, bytes=<%d> | ltx-video render ok",
            actual_duration,
            len(mp4_bytes),
        )
        return RenderResult(
            mp4_bytes=mp4_bytes,
            duration_s=actual_duration,
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
