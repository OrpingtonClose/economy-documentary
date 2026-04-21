"""Plan Vast.ai VMs that host the judge fleet.

Judges run on the same Vast.ai provider as TTS/LTX but on different VMs
with a different worker script (``scripts/judge_worker.py``, shipped in a
follow-up PR) and different validation rules.  The ADK provisioner
(``server/worker_provisioner.py``) hard-wires ``scripts/gpu_worker.py``
and validates ``worker_mode`` against ``("tts", "ltx", "both")``; judge
modes would trip its checks if we tried to share the code path.

Rather than mutate the live ADK provisioner (strangler-fig: leave the
ADK path untouched until cutover), this module returns a dedicated
:class:`JudgeWorkerSpec` — the input to the judge-specific
``provision_judge_vm`` function that lands alongside the judge worker
script in PR-C.  For now, :class:`JudgeWorkerSpec` is a planning
artifact: runbooks print it, cost estimators read it, unit tests
assert on its fields.

Kept free of heavy imports so the module can be unit-tested without
needing the ``vastai`` CLI, a running B2 session, or a worker runtime.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from strands_agents.judges.models import JudgeModelSpec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JudgeWorkerSpec:
    """Vast.ai provisioning plan for a single judge VM.

    Deliberately **not** ``server.worker_provisioner.WorkerSpec``.  The
    ADK ``WorkerSpec`` + ``provision_vm`` path is purpose-built for
    ``gpu_worker.py`` (TTS / LTX).  Judges run ``judge_worker.py`` and
    load different models; shoving them through ``provision_vm`` would
    either require loosening ``normalize_worker_mode`` on the ADK path
    (which the strangler-fig rules forbid) or silently pick a TTS/LTX
    image that can't serve the judge.

    The fields mirror the subset of ``WorkerSpec`` that the judge
    provisioner actually needs — role, ports, GPU/VRAM/disk floors,
    price ceiling.  ``judge_mode`` replaces ``worker_mode`` so the
    naming can't be confused with gpu_worker's ``--mode`` flag.

    Attributes:
        role: Stable role label used for logging and for the tunnel
            manager to index workers.  Always ``judge_<key>``.
        env_var: Environment variable the driver exports with the
            localhost tunnel URL once provisioning succeeds.  Always
            ``JUDGE_<KEY>_URL``.
        local_port: Localhost tunnel port — unique per role so the
            driver can run multiple judges without collision.
        remote_port: Port ``judge_worker.py`` binds on the VM.
        capability: Capability tag (``judge_safety`` /
            ``judge_av_primary`` / ``judge_av_tiebreaker``) that the
            fleet coordinator reads when routing :class:`JudgeRequest`s.
        gpu_type: Vast.ai GPU family filter.
        min_vram_gb: VRAM floor for offer search.
        max_price: Per-hour price ceiling.
        min_disk_gb: Disk floor for offer search.
        disk_gb: ``--disk`` argument for ``vast create instance``.
        judge_mode: Identifier the judge worker script uses to pick
            which model to load (``gemma4_abliterated`` / ``qwen35_omni``
            / ``video_salmonn_2_72b``).
        model_key: The catalog key this spec was built from.  Lets
            callers look up the full :class:`JudgeModelSpec` without
            threading it through separately.
    """

    role: str
    env_var: str
    local_port: int
    remote_port: int
    capability: str
    gpu_type: str
    min_vram_gb: int
    max_price: float
    min_disk_gb: int
    disk_gb: int
    judge_mode: str
    model_key: str
    extra: dict[str, Any] = field(default_factory=dict)


def build_judge_worker_spec(
    spec: JudgeModelSpec,
    *,
    local_port: int,
    remote_port: int = 8880,
    max_price: float = 5.00,
    min_vram_gb: int | None = None,
    min_disk_gb: int | None = None,
    gpu_type: str = "H100_SXM5",
) -> JudgeWorkerSpec:
    """Translate a :class:`JudgeModelSpec` into a :class:`JudgeWorkerSpec`.

    Args:
        spec: Which judge model the VM should serve.
        local_port: localhost tunnel port — unique per role so the
            driver can run multiple judges behind distinct
            ``JUDGE_*_URL`` env vars without collision.
        remote_port: Port ``judge_worker.py`` binds on the VM.
            Defaults to ``8880`` for parity with ``gpu_worker.py``.
        max_price: Ceiling for ``vastai search offers --max-dph``.
            Judges are short-lived and run on H100-class nodes, so the
            default is higher than the TTS/LTX default (``$2/hr``).
            Callers running against cheaper cards should lower this.
        min_vram_gb: VRAM floor.  Defaults to :attr:`spec.runtime_vram_gb`;
            override only when squeezing SALMONN 72B onto a smaller card
            with quantisation (explicit opt-in, not silent degrade).
        min_disk_gb: Disk floor for offer search.  Defaults to
            :attr:`spec.disk_gb`.  Kept separate from the ``--disk``
            argument so the search filter can be looser than the final
            allocation (occasional offers report undersized disk).
        gpu_type: GPU family filter for offer search.  Defaults to
            ``H100_SXM5`` because all three judge models fit
            comfortably there; callers may downgrade to ``A100_SXM4``
            for smaller quantised builds.

    Returns:
        A :class:`JudgeWorkerSpec` ready to be consumed by the judge
        provisioning flow (shipped with ``scripts/judge_worker.py`` in
        a follow-up PR).  Deliberately **not** a ``WorkerSpec``: the
        ADK path can't serve judges, and we want that barrier explicit
        rather than discovered at runtime.
    """

    vram_floor = min_vram_gb if min_vram_gb is not None else spec.runtime_vram_gb
    disk_floor = min_disk_gb if min_disk_gb is not None else spec.disk_gb

    env_var = f"JUDGE_{spec.key.upper()}_URL"

    logger.info(
        "key=<%s>, role=<%s>, vram_gb=<%d>, disk_gb=<%d>, gpu_type=<%s> | "
        "building judge worker spec",
        spec.key,
        spec.role,
        vram_floor,
        disk_floor,
        gpu_type,
    )

    return JudgeWorkerSpec(
        role=f"judge_{spec.key}",
        env_var=env_var,
        local_port=local_port,
        remote_port=remote_port,
        capability=f"judge_{spec.role}",
        gpu_type=gpu_type,
        min_vram_gb=int(vram_floor),
        max_price=max_price,
        min_disk_gb=int(disk_floor),
        disk_gb=int(spec.disk_gb),
        judge_mode=spec.key,
        model_key=spec.key,
    )


def describe_judge_fleet(specs: list[JudgeModelSpec]) -> list[dict[str, Any]]:
    """Return a JSON-friendly summary of a proposed judge fleet.

    Used by the provisioning playbook + dashboards to preview which
    hardware we're about to reserve before spending Vast.ai credits.

    Args:
        specs: Ordered list of judge models to stand up.

    Returns:
        List of dicts (one per spec) with the fields the cost table /
        runbook needs: ``key``, ``role``, ``display_name``,
        ``runtime_vram_gb``, ``disk_gb``, ``weights_gb``.  Deterministic
        field order because downstream consumers diff these dumps.
    """

    summary: list[dict[str, Any]] = []
    for s in specs:
        d = asdict(s)
        summary.append(
            {
                "key": d["key"],
                "display_name": d["display_name"],
                "role": d["role"],
                "runtime_vram_gb": d["runtime_vram_gb"],
                "disk_gb": d["disk_gb"],
                "weights_gb": d["weights_gb"],
                "hf_source": d["hf_source"],
                "b2_prefix": d["b2_prefix"],
            }
        )
    return summary
