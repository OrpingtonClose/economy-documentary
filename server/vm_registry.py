"""VM Registry — typed state extracted from raw CLI text.

Every VM provisioning decision flows through here:

    Raw CLI text → extract(VMState/VMRegistryDecision) → typed action

This replaces the agent's implicit text parsing with explicit structured
extraction. The agent still receives raw text (per /cheat: ONLY TEXT COMMUNICATION),
but the SYSTEM extracts types from that text before acting.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from models.vm_state import VMRegistryDecision, VMState, WorkerStatus
from structured_extract import extract

logger = logging.getLogger(__name__)

_DB_PATH = Path("/tmp/documentary-pipeline/vm_registry.db")
_LOCK = threading.Lock()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS vms (
            instance_id TEXT PRIMARY KEY,
            stage TEXT,
            status TEXT,
            ssh_host TEXT,
            ssh_port INTEGER,
            gpu_name TEXT,
            vram_gb REAL,
            price_per_hour REAL,
            worker_url TEXT,
            worker_ready INTEGER,
            worker_type TEXT,
            created_at REAL,
            last_seen_at REAL,
            raw_cli_text TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_stage ON vms(stage);
        CREATE INDEX IF NOT EXISTS idx_status ON vms(status);
        """
    )


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_provisioning(
    raw_cli_output: str,
    stage: str,
) -> VMState:
    """Parse raw `vastai create instance` output and record the VM.

    The agent receives raw CLI text. This function extracts structured state
    from that text and persists it. Returns the typed VMState for the agent
    to reason over.
    """
    vm = extract(
        VMState,
        raw_cli_output,
        system_prompt=(
            "Extract VM state from Vast.ai CLI output. "
            "If SSH host/port are present, include them. "
            "If the output shows a new instance was created, status is 'loading'. "
            "If the output shows an error, status is 'unknown'."
        ),
        temperature=0.0,
    )

    with _LOCK, _conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO vms
            (instance_id, stage, status, ssh_host, ssh_port,
             gpu_name, vram_gb, price_per_hour, worker_url, worker_ready,
             worker_type, created_at, last_seen_at, raw_cli_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                vm.instance_id,
                stage,
                vm.status,
                vm.ssh_host,
                vm.ssh_port,
                vm.gpu_name,
                vm.vram_gb,
                vm.price_per_hour,
                vm.worker_url,
                bool(vm.worker_status and vm.worker_status.ready),
                vm.worker_status.worker_type if vm.worker_status else "",
                time.time(),
                time.time(),
                raw_cli_output,
            ),
        )
        conn.commit()

    logger.info(
        "Recorded VM %s for stage=%s status=%s",
        vm.instance_id, stage, vm.status,
    )
    return vm


def record_health_check(
    instance_id: str,
    raw_worker_text: str,
) -> WorkerStatus:
    """Parse raw worker HTTP/SSH response and update worker status."""
    status = extract(
        WorkerStatus,
        raw_worker_text,
        system_prompt=(
            "Extract worker status from raw text. "
            "The text may be an HTTP response, SSH command output, or worker log. "
            "Look for signals like 'ready', 'tts=yes', 'ltx=yes', 'loading', "
            "GPU info, queue depth, etc. If the text is clearly an error, "
            "set ready=False and worker_type='unknown'."
        ),
        temperature=0.0,
    )

    with _LOCK, _conn() as conn:
        conn.execute(
            """
            UPDATE vms
            SET worker_ready = ?, worker_type = ?, last_seen_at = ?
            WHERE instance_id = ?
            """,
            (
                bool(status.ready),
                status.worker_type,
                time.time(),
                instance_id,
            ),
        )
        conn.commit()

    logger.info(
        "Updated worker %s: ready=%s type=%s",
        instance_id, status.ready, status.worker_type,
    )
    return status


def decide_provisioning_action(
    raw_agent_reasoning: str,
    stage: str,
) -> VMRegistryDecision:
    """Given the agent's reasoning text, decide what to do about VMs.

    This is the key insight: the agent emits raw reasoning text ("SSH failed,
    maybe the worker is down, let me provision a new one"). The SYSTEM extracts
    a typed decision from that text, but ALSO checks the registry to ground the
    decision in reality.
    """
    # First: ground the agent's reasoning with actual registry state
    with _LOCK, _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM vms WHERE stage = ?",
            (stage,),
        ).fetchall()

    existing_vms = [VMState(**{k: r[k] for k in r.keys()}) for r in rows]

    grounding = "\n".join(
        f"- VM {v.instance_id}: status={v.status}, worker_ready={v.worker_status.ready if v.worker_status else 'unknown'}"
        for v in existing_vms
    ) if existing_vms else "- No existing VMs for this stage."

    context = f"""Agent reasoning:
{raw_agent_reasoning}

Ground truth from VM registry:
{grounding}

Extract the decision, but if the agent wants to provision a new VM and an
existing VM is still running, bias toward 'use_existing' with the existing
instance_id."""

    return extract(
        VMRegistryDecision,
        context,
        system_prompt=(
            "Extract the provisioning decision from agent reasoning, but CORRECT "
            "it against ground truth. If the agent says 'provision new' but the "
            "registry shows a running VM, the action should be 'use_existing'. "
            "Only 'destroy_and_reprovision' if the VM is confirmed dead."
        ),
        temperature=0.0,
    )


def list_vms(stage: str = "") -> list[VMState]:
    """Return all VMs, optionally filtered by stage."""
    with _LOCK, _conn() as conn:
        if stage:
            rows = conn.execute(
                "SELECT * FROM vms WHERE stage = ?", (stage,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM vms").fetchall()
    return [VMState(**{k: r[k] for k in r.keys()}) for r in rows]


def get_vm(instance_id: str) -> VMState | None:
    """Get a single VM by ID."""
    with _LOCK, _conn() as conn:
        row = conn.execute(
            "SELECT * FROM vms WHERE instance_id = ?", (instance_id,)
        ).fetchone()
    if row is None:
        return None
    return VMState(**{k: row[k] for k in row.keys()})
