"""Glue the judge catalog into the pipeline's Vast.ai provisioner.

The judge models run on the same Vast.ai fleet as TTS/LTX — different
VMs, same provisioning plumbing.  Instead of forking
:mod:`server.worker_provisioner`, we translate each
:class:`JudgeModelSpec` into a :class:`WorkerSpec` and hand it back;
callers feed that spec into the existing parallel-provisioning flow.

The translation is the only thing that lives here because the rest of
the provisioning behaviour (offer search, bootstrap script, health
polling, direct-connection resolution) is generic — whatever works for
a TTS worker works for a judge worker.

Kept free of heavy imports so the module can be unit-tested without
needing the real ``vastai`` CLI, a running B2 session, or a worker
runtime.  :class:`WorkerSpec` is lazily imported inside
:func:`build_judge_worker_spec` for the same reason.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from strands_agents.judges.models import JudgeModelSpec

logger = logging.getLogger(__name__)


def build_judge_worker_spec(
    spec: JudgeModelSpec,
    *,
    local_port: int,
    remote_port: int = 8880,
    max_price: float = 5.00,
    min_vram_gb: int | None = None,
    min_disk_gb: int | None = None,
    gpu_type: str = "H100_SXM5",
) -> Any:
    """Translate a :class:`JudgeModelSpec` into a ``WorkerSpec``.

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
        A ``server.worker_provisioner.WorkerSpec`` ready to feed into
        the existing parallel-provisioning flow.  Imported lazily so
        this module stays unit-testable without the full provisioner
        stack.
    """

    # Lazy import — only materialised at runtime.  Keeping it out of
    # module scope means ``from strands_agents.judges import
    # build_judge_worker_spec`` doesn't require ``b2sdk`` / ``vastai``
    # at import time.
    from worker_provisioner import WorkerSpec  # type: ignore[import-not-found]

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

    return WorkerSpec(
        role=f"judge_{spec.key}",
        env_var=env_var,
        local_port=local_port,
        remote_port=remote_port,
        capability=f"judge_{spec.role}",
        gpu_type=gpu_type,
        min_vram_gb=vram_floor,
        max_price=max_price,
        min_disk_gb=disk_floor,
        disk_gb=spec.disk_gb,
        worker_mode=f"judge_{spec.key}",
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
