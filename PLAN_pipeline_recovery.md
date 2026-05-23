# Pipeline Recovery Plan: From Broken to Movie-Producing

## Context
Pipeline Run 3 crashed after 1651s (~27 min). DeepSeek API dropped connection mid-request. No resume path. 12 VMs orphaned and billing. Architecture analysis revealed the `infra_agent` guardian exists in bootstrap scripts but is NOT deployed by the current provisioning path.

## Constraints (From User Feedback)

1. **No hardcoded VM lifetime limits** — Agent decides when to destroy, not a timer
2. **Agent installs the onstart script** — Provisioning agent pushes bootstrap to VM via SSH
3. **VM pings back connection details** — VM reports IP/port/health via plain text
4. **NO string/regex parsing** — Agents read text and reason; code does not parse patterns like `ltx=no`
5. **ONLY TEXT COMMUNICATION** — No JSON, no structured returns, no dataclasses across agent boundaries. Plain text only.
6. **NO NEW HTTP ENDPOINTS** — Use existing `GET /` and `POST /` only
7. **MORE TRACING** — Extend CPython `sys.monitoring` for deeper observability
8. **Update /cheat skill** — Add "text-only communication" and "no regex parsing" rules

## Problems & Fixes

### 1. VMs Don't Self-Destruct — CRITICAL

**Root cause:** `vast_provisioning.py:onstart_cmd` runs `scripts/gpu_worker.py` directly. The `infra_agent` guardian is never started. Bootstrap scripts (`qwen3_tts_worker_bootstrap.sh`, `ltx_video_worker_bootstrap.sh`) that deploy the guardian exist but are unused.

**Fix:** The provisioning agent uses `bash_command` to:
1. SCP the bootstrap script to the VM
2. SSH execute the bootstrap script with env vars (VAST_INSTANCE_ID, VAST_AI_API_KEY)
3. The bootstrap script installs systemd services for both infra_agent and gpu_worker
4. The VM's `gpu_worker.py` reports status via plain text on `GET /`
5. The orchestrator agent reads the text and decides when to destroy

**No hardcoded timeouts.** The agent reads `GET /` output (e.g., "ok NVIDIA A100 vram=0.0/80.0GB mode=ltx worker_ready=no") and reasons: "Worker not ready yet, I'll check again later." Or: "Worker has been idle for 20 minutes with no jobs, I'll destroy it."

### 2. No Resume on Crash — CRITICAL

**Root cause:** `RecoveryShell.resume = False` (hardcoded). `SnapshotHook` exists but not registered. B2 checkpoint system exists but not wired.

**Fix:**
- Wire `SnapshotHook` into `build_documentary_graph()` hooks list (one-line change)
- Read `metadata.json` on startup to populate `completed_stages`
- Set `resume=True` when metadata shows incomplete run
- Seed timeline from last checkpoint OTIO
- Re-query Vast.ai for active VMs on resume (do not reprovision if still running)
- On DeepSeek `ConnectionError`, save checkpoint before raising (enables exact-moment resume)

### 3. DeepSeek Connection Drops — CRITICAL

**Root cause:** Strands agent streaming has no retry. `EventLoopException` propagates and kills the graph.

**Fix:** Wrap the LLM call in a resilient retry loop:
- Exponential backoff with jitter (1s, 2s, 4s, 8s, max 60s)
- Only retry on transient errors (ConnectionError, TimeoutError)
- Max 5 retries per node
- On exhaustion, save checkpoint and raise
- **NO TIMEOUTS** — the agent waits as long as needed, retrying with backoff

### 4. GPU Model Download Incomplete — HIGH

**Root cause:** `ltx-2.3-22b-dev.safetensors.tmp` exists (25GB partial). Worker reports failure because checkpoint not found.

**Fix:**
- Worker bootstrap checks for `.tmp` files before starting
- If partial found, delete and re-download (safetensors doesn't support resume)
- Worker reports status as plain text: "bootstrap error: model download incomplete, restarting"
- Agent reads this and decides: wait, retry, or reprovision

### 5. OTIO Metadata Thrashing — MEDIUM

**Root cause:** `otio_manager.py` calls `refresh_from_disk()` on every access. 68,216 `_to_native` calls per run (49/sec).

**Fix:**
- Cache OTIO timeline in memory with mtime-based invalidation
- Only refresh if file mtime changed since last read
- Batch metadata writes (accumulate changes, write once per second)
- The agent can call `otio_manager.get_status_text()` which returns plain text summary

### 6. SSH Death Spiral — MEDIUM

**Root cause:** Agent calls `bash_command("ssh ...")` for every health check. 95,190 SSH commands + 102,532 vast CLI commands in one run.

**Fix:**
- Agent caches worker status in memory (refresh every 30 seconds, not every cycle)
- Agent calls `GET /` on worker URL for health status (plain text, not SSH)
- Agent batches Vast CLI calls where possible
- Agent reasons: "I checked 5 seconds ago, I'll use cached status"

### 7. SnapshotHook Orphaned — LOW

**Fix:** One-line: add `SnapshotHook()` to `hooks=[...]` in `build_documentary_graph()`.

### 8. B2 Checkpoint System Orphaned — LOW

**Fix:** Wire `CheckpointHook` to call `otio_manager.checkpoint()` directly.

### 9. CPython Tracing — ENHANCEMENT

**Fix:** Extend `AutoTracer` to capture:
- Agent decision points (BeforeNodeCall, AfterNodeCall)
- Tool call boundaries (BeforeToolCall with tool name)
- VM lifecycle transitions (provision → running → healthy → destroy)
- LLM retry events (attempt N of M, backoff delay)
- OTIO read/write events (read count, write count, cache hit/miss)

Store in SQLite alongside existing `calls` table:
```sql
CREATE TABLE events (
    run_id TEXT, ts REAL, event_type TEXT,
    agent_name TEXT, node_id TEXT, tool_name TEXT,
    duration_ms REAL, detail TEXT
);
```

## Communication Protocol

**Worker ↔ Orchestrator:**
- `GET /` → plain text: `ok NVIDIA A100 vram=0.0/80.0GB mode=ltx worker_ready=yes`
- `POST /` → raw text instruction, raw bytes response

**No JSON. No schemas. No `/health`, `/health/vram`, `/video/render`, `/tts/render`.**

The agent reads the text and reasons. The agent never parses `ltx=no` with regex. The agent reads the entire text output and decides what to do.

## Implementation Priority

### Phase 1: Stop the Bleeding (1-2 hours)
1. Deploy infra_agent via bootstrap script on every VM
2. Wire SnapshotHook registration
3. Add LLM retry with exponential backoff (no timeouts)
4. Clean partial model downloads in worker bootstrap
5. Update /cheat skill with text-only and no-regex rules

### Phase 2: Resume & Persistence (2-3 hours)
6. Enable cross-run resume (read metadata.json)
7. Wire B2 checkpoint system
8. Add VM re-query on resume

### Phase 3: Performance & Observability (2-3 hours)
9. OTIO caching (mtime-based)
10. SSH reduction (status caching, batching)
11. Extend CPython tracing with semantic events table

## Success Criteria

- [ ] Pipeline runs from brief to `master.mp4` without manual intervention
- [ ] VMs are destroyed when agent decides (not by hardcoded timer)
- [ ] Pipeline resumes after crash at any stage boundary
- [ ] No orphaned VMs after pipeline completes
- [ ] OTIO reads < 1000 per run
- [ ] SSH commands < 1000 per run
- [ ] DeepSeek connection drops retried and recovered
- [ ] Partial model downloads detected and cleaned
- [ ] All agent communication is plain text (no JSON)
- [ ] Extended tracing captures semantic pipeline events
