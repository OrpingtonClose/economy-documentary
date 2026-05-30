from __future__ import annotations

import os
import time
from typing import Optional
from fastapi import FastAPI, HTTPException, Header, Query
from pydantic import BaseModel

from effects import Effect
from event_store import EventStore
from projections import (
    Timeline,
    Jobs,
    VMs,
    BudgetProjection,
    StateProjection,
    GlobalStateResponse,
    OTIOResponse,
    OTIOSlotState,
    JobResponse,
    JobResponseItem,
    VMResponse,
    VMResponseItem,
    StateResponse,
    PhaseChangeItem,
    BudgetResponse,
)

app = FastAPI(title="Global State Agent", description="Read-only projection server")

# Default logs directory matches DATA_DIR in config.py
LOG_DIR = "/tmp/documentary-pipeline"
event_store = EventStore(log_dir=LOG_DIR)


def build_global_state(run_id: str) -> GlobalStateResponse:
    """Replay all events from sequence 0 for a run_id and return the GlobalStateResponse."""
    # Build clean projection states on every call (stateless catch-up/replay from 0)
    timeline = Timeline()
    jobs = Jobs()
    vms = VMs()
    budget = BudgetProjection()
    state = StateProjection()

    # Tick all projections from sequence 0
    timeline.tick(run_id, event_store)
    jobs.tick(run_id, event_store)
    vms.tick(run_id, event_store)
    budget.tick(run_id, event_store)
    state.tick(run_id, event_store)

    # 1. Map OTIO (Timeline)
    scenes = set()
    slots = {}
    measured_slots = 0
    delivered_slots = 0
    dirty_slots = 0
    for addr, s in timeline.slots.items():
        scenes.add(s["scene_num"])
        if s["status"] == "measured":
            measured_slots += 1
        elif s["status"] == "delivered":
            delivered_slots += 1
        elif s["status"] == "scripted":
            dirty_slots += 1
        slots[addr] = OTIOSlotState(
            scene_num=s["scene_num"],
            block_id=s["block_id"],
            speaker=s["speaker"],
            text=s["text"],
            scripted_sec=s["scripted_sec"],
            measured_sec=s["measured_sec"],
            status=s["status"],
            artifact_uri=s["artifact_uri"],
        )
    otio_res = OTIOResponse(
        scenes=len(scenes),
        total_slots=len(timeline.slots),
        measured_slots=measured_slots,
        delivered_slots=delivered_slots,
        dirty_slots=dirty_slots,
        duration_sec=timeline.get_timeline_duration_sec(),
        slots=slots,
    )

    # 2. Map Jobs
    job_items = {}
    for job_id, j in jobs.jobs.items():
        job_items[job_id] = JobResponseItem(
            job_id=j.job_id,
            job_type=j.job_type,
            slot_id=j.slot_id,
            status=j.status,
            params=j.params,
            artifact_uri=j.artifact_uri,
            duration_sec=j.duration_sec,
            error_message=j.error_message,
            requeue_count=j.requeue_count,
            created_at=j.created_at,
            completed_at=j.completed_at,
            vm_instance_id=j.vm_instance_id,
        )
    jobs_res = JobResponse(
        jobs=job_items,
        reconciliation_complete=jobs.reconciliation_complete,
        dirty_blocks=list(jobs.dirty_blocks),
        clean_blocks=list(jobs.clean_blocks),
        block_attempts=dict(jobs.block_attempts),
        spent_usd=jobs.spent_usd,
        production_failures=jobs.production_failures,
    )

    # 3. Map VMs
    vm_items = {}
    active_count = 0
    role_breakdown = {}
    for instance_id, v in vms.vms.items():
        vm_items[instance_id] = VMResponseItem(
            instance_id=v.instance_id,
            status=v.status,
            role=v.role,
            offer_id=v.offer_id,
            worker_url=v.worker_url,
            hourly_rate_usd=v.hourly_rate_usd,
            started_at=v.started_at,
            observed_status=v.observed_status,
        )
        if v.status == "active":
            active_count += 1
            if v.role:
                role_breakdown[v.role] = role_breakdown.get(v.role, 0) + 1
    vms_res = VMResponse(
        vms=vm_items,
        active_count=active_count,
        total_count=len(vms.vms),
        estimated_hourly_cost_usd=vms.estimated_hourly_cost(),
        role_breakdown=role_breakdown,
    )

    # 4. Map State
    phase_changes = [
        PhaseChangeItem(
            from_phase=c.from_phase,
            to_phase=c.to_phase,
            reason=c.reason,
            at_sequence=c.at_sequence,
        )
        for c in state.phase_history
    ]
    state_res = StateResponse(
        current_phase=state.current_phase,
        phase_changes=phase_changes,
        agents_tracked=list(state.recent_effects.keys()),
        latest_sequence=state.last_sequence,
    )

    # 5. Map Budget
    budget_res = BudgetResponse(
        budget_cap_usd=budget.budget_cap_usd,
        spent_usd=budget.spent_usd,
        remaining_usd=budget.remaining_usd(),
        exceeded=budget.exceeded,
        vm_costs=budget.vm_costs,
    )

    # Find the maximum sequence number among all projections
    latest_seq = max(
        timeline.last_sequence,
        jobs.last_sequence,
        vms.last_sequence,
        state.last_sequence,
        budget.last_sequence,
    )

    return GlobalStateResponse(
        run_id=run_id,
        timestamp=time.time(),
        otio=otio_res,
        jobs=jobs_res,
        vms=vms_res,
        state=state_res,
        budget=budget_res,
        latest_sequence=latest_seq,
    )


@app.get("/", response_model=GlobalStateResponse)
async def get_state(
    run_id: Optional[str] = Query(None, description="Run ID to retrieve state for"),
    x_run_id: Optional[str] = Header(None, alias="X-Run-ID", description="Fallback Run ID header"),
):
    """Retrieve the authoritative rebuilt projections for the given run_id."""
    effective_run_id = run_id or x_run_id
    if not effective_run_id:
        raise HTTPException(
            status_code=400,
            detail="run_id must be provided via query parameter or X-Run-ID header",
        )

    try:
        return build_global_state(effective_run_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to build projection state: {exc}",
        )
