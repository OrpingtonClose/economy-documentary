# 22 - Concurrency and Timeouts Invariants

This document specifies the concurrency model and timeout constraints of the documentary pipeline. It details how the system guarantees correct, observable execution across agent services, database writes, and integration tests.

---

## 1. Concurrency Model

The system operates under a dual-layer concurrency model: **Pipeline-level serialization** combined with **agent-level parallelism within a run**.

### 1.1 Single-Run Serialization (One Run at a Time)
The system is designed to execute exactly **one pipeline run at a time**.
- There is a single global SQLite database (`/tmp/documentary-pipeline/events.db`) containing the event log for the active run.
- Multiple separate runs (different documentaries) never compete for resources, port allocations, or SQLite database connections concurrently.
- **Functionality Implications**: 
  - **Traceability**: Every event written to the event store belongs strictly to the current run. There are no interleaved event streams from other runs, making trace debugging deterministic and simple.
  - **External Helpers & Humans**: An external agent or a human operator inspecting the active system receives a clear, singular view of the current pipeline state.

### 1.2 Parallel Agent Execution (Within a Run)
Within the boundaries of the active run, individual agent services (Scenario, Audio, Video, Assembly, and the Provisioner) are allowed to operate **concurrently and in parallel**.
- For example, the Audio Agent can submit narration generation tasks to the job queue while the Video Agent is concurrently rendering visual prompts.
- All agents read and write to the same global event store.

### 1.3 Process-Level Turn Serialization
To prevent internal race conditions and SQLite database lock contention when agents process events:
- Each agent service runs a global asynchronous lock instance (`LoopBoundLock`) within its process.
- The `execute_agent_turn` function wraps the agent's reasoning turn inside an `async with lock:` block. This guarantees that only **one turn can execute at a time** within that agent process.
- If a background polling loop or a manual HTTP `POST` wake-up triggers a turn while the lock is held, the agent process skips execution to avoid overlapping handler turns.

### 1.4 Agent Health and "Busy" States
Every agent app exposes a `GET /` health endpoint and a `POST /` trigger endpoint:
- **`GET /`**: Returns the health state (`"healthy"`, `"busy"`, or `"error"`) and status metadata.
- **`POST /`**: Used by the integration tests or orchestrator to trigger a turn.
- If the agent's status is already `"busy"` (meaning it is currently running a turn in the background), the `POST /` handler immediately returns a safe, empty response, avoiding overlapping turn execution.
- **Integration Test Synchronization**: Test harnesses must query the health endpoint (`GET /`) in a polling loop and wait until status is `"healthy"` before sending subsequent wakeup requests.

---

## 2. Timeout Policy

The system enforces a strict division between production processing paths and health/readiness probing.

### 2.1 Production Code: No Timeouts Banned (Principle 4)
- **Rule**: There are no timeout parameters, timers (`setTimeout`, `threading.Timer`, `asyncio.timeout`), or self-destruct logic in primary pipeline processing paths (e.g. LLM reasoning loops, rendering processes, B2 upload commands).
- **Rationale**: Timeouts lead to silent failures, half-written files, and state inconsistencies. All primary operations run to completion or wait indefinitely.
- **Intervention**: If a pipeline stage hangs, it remains blocked until the **human operator** intervenes manually (e.g., restarts the service or manually triggers recovery).

### 2.2 Exception: Health/Readiness Probes
- **Rule**: Timeouts are **only allowed** on lightweight network check requests (e.g. pinging a VM or another agent's health endpoint to see if it is responsive).
- **Rationale**: Probing is non-blocking and does not write state. Prohibiting timeouts on health checks would cause the main orchestrator or coordinator to block indefinitely on network partitions or dead VMs, halting system diagnostics.
- **Enforcement**: The static code analysis tool (`cheat_check.py`) scans code for `timeout=` calls on HTTP clients (`urllib`, `requests`, `httpx`). Probing calls are ignored by the scanner if the line contains a documented `# health probe` comment or contains the word `health` or `probe`.

---

## 3. Future Design Recommendations

Reconciliations and timeline compositions require special care as the system moves toward production:

### 3.1 Narration Validation and Conformance
Currently, narration scripts are revised or trimmed pragmatically to fit target durations:
- **Audio/Video Understanding Loops**: A future enhancement will introduce a video/audio analysis tool to review media outputs and feedback exact pacing or content constraints to the Scenario Agent, replacing simple length-based heuristics.
- **Exact Conformance via Overproduction**: Achieving exact visual-narration synchronization is best handled by over-producing media clips slightly and then cleanly cutting/trimming them to the exact desired timeline boundary.

### 3.2 OpenTimelineIO (OTIO) Data Isolation
To ensure high-fidelity timeline tracking:
- Decompose the physical `JobCompleted` event into a physical worker event and a logical `MediaExtracted` event.
- The `MediaExtracted` event should cleanly isolate measured clip durations, resolutions, and audio codecs, providing the Assembly Agent with the structured tracks needed to generate clean OTIO sequences.
