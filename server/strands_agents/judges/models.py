"""Judge model catalog — VRAM / disk / source-of-truth metadata.

Mirrors the shape of :mod:`server.worker_provisioner`'s ``WorkerSpec`` so
the judge fleet can piggyback on the existing Vast.ai provisioning flow
without needing a parallel pipeline.

The catalog is the single source of truth for:

- which open-weight judge model corresponds to which role,
- where to fetch the weights (HuggingFace hub vs. our private B2 mirror),
- how much GPU/disk the serving VM needs,
- which role each judge plays in :class:`JudgeEnsemble` (shipped in PR-C).

Keeping this as plain data (not code that imports torch) means the
catalog is importable in environments without GPU toolchains — including
CI, evaluators that only need the *name* of the judge, and docs
generators.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

JudgeRole = Literal["safety", "av_primary", "av_tiebreaker"]
"""The three roles the ensemble dispatches to.

``safety``: uncensored content-rejection judge — must not refuse.
``av_primary``: per-scene audio+video QA judge (high throughput).
``av_tiebreaker``: fine-grained AV verdict when the first two disagree.
"""


@dataclass(frozen=True)
class JudgeModelSpec:
    """Hardware + source metadata for one self-hosted judge model.

    All fields are immutable because the catalog is a constant — mutating
    it at runtime is a bug, not a feature.

    Attributes:
        key: Short stable identifier used as the dict key in
            :data:`JUDGE_CATALOG` and as the ``judge_mode`` on
            :class:`strands_agents.judges.provisioner.JudgeWorkerSpec`.
        display_name: Human-readable label for logs / dashboards.
        role: Which ensemble seat this model fills (:data:`JudgeRole`).
        hf_source: HuggingFace repo ID. Empty string for models that are
            only available via B2 (abliterated weights never upstream).
        b2_prefix: Optional B2 key prefix under the pipeline's main
            bucket. Non-empty for models we mirror privately. The
            fetcher concatenates this prefix with the file names in
            :attr:`checkpoint_files` to pull each shard.
        params_billions: Parameter count, used only for logging.
        dtype: Runtime dtype the serving code should load at.
        weights_gb: Approximate disk footprint of the weights alone.
        runtime_vram_gb: Minimum GPU VRAM the serving VM needs — passed
            to the Vast.ai ``gpu_ram>=`` filter.
        disk_gb: Total disk footprint (weights + tokenizer + scratch +
            docker layer overhead). Passed to ``vastai create instance
            --disk``.
        min_torch: Minimum PyTorch version the Docker image must ship.
        min_cuda: Minimum CUDA runtime.
        checkpoint_files: Files the fetcher must pull. Order matters
            only in so far as we download them sequentially for easier
            progress logging.
        torch_wheel_suffix: cuXXX suffix used by ``pip install torch``
            inside the worker bootstrap script.
    """

    key: str
    display_name: str
    role: JudgeRole
    hf_source: str
    b2_prefix: str
    params_billions: float
    dtype: str
    weights_gb: float
    runtime_vram_gb: int
    disk_gb: int
    min_torch: str
    min_cuda: str
    checkpoint_files: tuple[str, ...] = field(default_factory=tuple)
    torch_wheel_suffix: str = "cu126"


GEMMA4_ABLITERATED = JudgeModelSpec(
    key="gemma4_abliterated",
    display_name="Gemma 4 Abliterated (safety judge)",
    role="safety",
    # Abliterated builds are never pushed to the public HF hub — we pull
    # them from our private B2 mirror.  The ``hf_source`` stays empty so
    # the fetcher knows to use the B2 path exclusively.
    hf_source="",
    b2_prefix="models/judges/gemma4-abliterated",
    params_billions=27.0,
    dtype="bf16",
    weights_gb=54.0,
    runtime_vram_gb=60,
    disk_gb=180,
    min_torch="2.7.0",
    min_cuda="12.6",
    checkpoint_files=(
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "model.safetensors.index.json",
        "model-00001-of-00006.safetensors",
        "model-00002-of-00006.safetensors",
        "model-00003-of-00006.safetensors",
        "model-00004-of-00006.safetensors",
        "model-00005-of-00006.safetensors",
        "model-00006-of-00006.safetensors",
    ),
)


QWEN35_OMNI = JudgeModelSpec(
    key="qwen35_omni",
    display_name="Qwen3.5-Omni (AV primary)",
    role="av_primary",
    # Qwen3.5-Omni open weights live on the HF hub — we mirror to B2 as
    # a convenience so a Vast.ai VM with restricted egress can still
    # fetch them.  Both sources kept populated; the fetcher prefers B2
    # when it's present.
    hf_source="Qwen/Qwen3.5-Omni",
    b2_prefix="models/judges/qwen3.5-omni",
    params_billions=30.0,
    dtype="bf16",
    weights_gb=60.0,
    runtime_vram_gb=72,
    disk_gb=220,
    min_torch="2.7.0",
    min_cuda="12.6",
    checkpoint_files=(
        "config.json",
        "tokenizer.json",
        "preprocessor_config.json",
        "model.safetensors.index.json",
    ),
)


VIDEO_SALMONN_2_72B = JudgeModelSpec(
    key="video_salmonn_2_72b",
    display_name="video-SALMONN 2 72B (AV tiebreaker)",
    role="av_tiebreaker",
    hf_source="bytedance-research/video-SALMONN-2-72B",
    b2_prefix="models/judges/video-salmonn-2-72b",
    params_billions=72.0,
    dtype="bf16",
    weights_gb=144.0,
    runtime_vram_gb=160,
    disk_gb=400,
    min_torch="2.7.0",
    min_cuda="12.6",
    checkpoint_files=(
        "config.json",
        "tokenizer.json",
        "model.safetensors.index.json",
    ),
)


JUDGE_CATALOG: dict[str, JudgeModelSpec] = {
    GEMMA4_ABLITERATED.key: GEMMA4_ABLITERATED,
    QWEN35_OMNI.key: QWEN35_OMNI,
    VIDEO_SALMONN_2_72B.key: VIDEO_SALMONN_2_72B,
}
"""Registry indexed by :attr:`JudgeModelSpec.key`.

Ordered deterministically so catalog iteration order is stable across
runs — important for golden-file tests and reproducible trace dumps.
"""
