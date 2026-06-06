# Comprehensive Compliance Audit Report (server/ vs. obsidian-vault/)

This report presents a paragraph-to-paragraph compliance check of all Python files under `server/` against the technical specifications in the `obsidian-vault/` directory.

---

## 1. [R1 & R4] Event Store & Schema Alignment

### 1.1 Undocumented Subclasses (Structural Mismatch)
* **Specification Violated**: `obsidian-vault/02 - Event Store and Effect Schemas.md` (Section 1.3 "Job Effects" & Section 1.9 "Discriminator Union")
* **Observed Mismatch**: `server/effects.py` introduces 12 undocumented subclasses of Job-related effects which are not part of the official Pydantic Effect Schemas specification. These subclasses override the `kind` field via Literal and are explicitly included in `EffectUnion` and `KIND_TO_MODEL` mapping:
  - `QueueAudioJob` (lines 120-123)
  - `QueueVideoJob` (lines 125-128)
  - `AudioJobStarted` (lines 138-140)
  - `VideoJobStarted` (lines 142-144)
  - `AudioJobCompleted` (lines 164-166)
  - `VideoJobCompleted` (lines 168-170)
  - `AudioJobFailed` (lines 191-193)
  - `VideoJobFailed` (lines 195-197)
  - `AudioJobRequeued` (lines 207-209)
  - `VideoJobRequeued` (lines 211-213)
  - `AudioJobApproved` (lines 224-226)
  - `VideoJobApproved` (lines 228-230)
* **Citations**:
  - `server/effects.py` lines 120-123:
    ```python
    class QueueAudioJob(QueueJob):
        kind: Literal["queue_audio_job"] = "queue_audio_job"
        job_type: Literal["tts"] = "tts"
    ```
  - `server/effects.py` lines 597-613 (`EffectUnion` inclusion of undocumented classes):
    ```python
        QueueAudioJob,
        QueueVideoJob,
        JobStarted,
        AudioJobStarted,
        VideoJobStarted,
        JobCompleted,
        AudioJobCompleted,
        VideoJobCompleted,
        JobFailed,
        AudioJobFailed,
        VideoJobFailed,
        JobRequeued,
        AudioJobRequeued,
        VideoJobRequeued,
        JobApproved,
        AudioJobApproved,
        VideoJobApproved,
    ```

### 1.2 Undocumented Fields
* **Specification Violated**: `obsidian-vault/02 - Event Store and Effect Schemas.md` (Section 1.6 "OTIO / Timeline Effects")
* **Observed Mismatch**: The `MergeIntoOTIO` Pydantic class in the codebase defines an extra undocumented field `start_sec` which does not exist in the official spec.
* **Citations**:
  - `server/effects.py` line 402:
    ```python
    start_sec: float = Field(default=0.0, description="Optional start time coordinate for coordinate-based schema")
    ```

### 1.3 Undocumented Database Tables & Memory Mutations
* **Specification Violated**: `obsidian-vault/02 - Event Store and Effect Schemas.md` (Section 2.1 "Schema")
* **Observed Mismatch**: The database initialization SQL script creates a completely undocumented table named `agent_memories` inside `events.db`.
* **Citations**:
  - `server/event_store.py` lines 46-51:
    ```python
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_memories (
            agent TEXT PRIMARY KEY,
            memories_json TEXT NOT NULL
        )
    """)
    ```

---

## 2. [R1 & R5] Complete NoOp Elimination Check

* **Specification Violated**: `obsidian-vault/02 - Event Store and Effect Schemas.md` (Section 2 "Event Store" & Section 1.8 `NoOp` model)
* **Evaluation**: Compliant. The EventStore append boundary in `server/event_store.py` checks for `noop` event kinds and returns a mock `EventRecord` with `seq=-1` without executing any SQL insert statement.
* **Citations**:
  - `server/event_store.py` lines 71-76:
    ```python
    if effect.kind == "noop":
        return EventRecord(
            seq=-1,
            effect=cast(EffectUnion, effect),
            otio_hash_before=otio_hash_before
        )
    ```
* **Callers Safety**: In `server/agent_base.py`, callers explicitly filter out `noop` events before writing, preventing the sequence number `-1` from polluting other system logs.
  - `server/agent_base.py` lines 892-895:
    ```python
    # Append effects
    for effect in effects:
        if effect.kind == "noop":
            continue
        event_store.append(effect, otio_hash)
    ```

---

## 3. [R2] REST Endpoint Control Protocols

* **Specification Violated**: `obsidian-vault/01 - Philosophy and Topology.md` (Section 2.3 "HTTP Contract Specification")

### 3.1 Lock Serialization on GET and POST
* **Evaluation**: Compliant. The endpoints `GET /` and `POST /` in pipeline agents serialize execution using the loop-bound `run_lock_manager` lock, performing no heavy inline processing.
* **Citations**:
  - `server/agent_base.py` lines 941-942 (`GET /` handler):
    ```python
    lock = run_lock_manager.get_lock()
    async with lock:
    ```
  - `server/agent_base.py` lines 976-977 (`POST /` handler):
    ```python
    lock = run_lock_manager.get_lock()
    async with lock:
    ```

### 3.2 PUT Intervention & Task Cancellation
* **Evaluation**: Mostly Compliant. `PUT /` immediately cancels the active asyncio task running the agent turn, launches a new turn background execution, and returns `204 No Content`.
* **Subprocess Handling Exception**: While `bash_command` / `run_cmd` handles task cancellation by killing the shell process group (`signal.SIGKILL`), synchronous, blocking `subprocess.run` calls (e.g. `ffmpeg` concats/probes inside the assembly agent turn on lines 703, 711, 718, 794, 810, 818) block the main event loop thread and cannot be cancelled instantly or catch `CancelledError`.
* **Citations**:
  - `server/agent_base.py` line 1089 (`PUT /` response):
    ```python
    return PlainTextResponse("", status_code=204)
    ```
  - `server/agent_base.py` lines 1029-1036 (`PUT /` task cancellation):
    ```python
    # Cancel existing task if running
    global active_tasks
    existing_task = active_tasks.get(role)
    if existing_task and not existing_task.done():
        existing_task.cancel()
        try:
            await existing_task
        except asyncio.CancelledError:
            pass
    ```

### 3.3 Strict Endpoint Middleware & Sub-endpoints
* **Evaluation**: Compliant. Middleware blocks all query parameters and sub-endpoints (e.g. `/health`, `/status`), returning 404 or 400.
* **Minor Mismatch**: GSA (`global_state_agent.py`) middleware allows `POST` requests at the routing filter level, though no `@app.post("/")` handler is registered in the application. Per specification, the GSA should only accept `GET /`.
* **Citations**:
  - `server/agent_base.py` lines 916-921:
    ```python
    if request.url.path != "/":
        return PlainTextResponse("Not Found: Only root '/' is permitted", status_code=404)
    if request.method not in ("GET", "POST", "PUT"):
        return PlainTextResponse("Method Not Allowed: Only GET, POST and PUT permitted", status_code=405)
    if request.query_params:
        return PlainTextResponse("Bad Request: Query parameters are prohibited", status_code=400)
    ```
  - `server/global_state_agent.py` lines 35-36:
    ```python
    if request.method not in ("GET", "POST"):
        return PlainTextResponse("Method Not Allowed: Only GET and POST permitted", status_code=405)
    ```

---

## 4. General Module Compliance & Fundamental Invariants

### 4.1 Invariant 1: Event Log as Sole Source of Truth (VIOLATED)
* **Specification Violated**: `obsidian-vault/01 - Philosophy and Topology.md` (Section 1.1 "Event Log as Sole Source of Truth")
* **Violation**: The codebase bypasses the event store to manage agent-specific memories. Instead of emitting typed Pydantic effects for memory updates, it performs direct SQL mutations (`INSERT OR REPLACE`) to an independent database table `agent_memories` inside `events.db`. Replaying the event log fails to rebuild these memories.
* **Citations**:
  - `server/agent_base.py` line 628:
    ```python
    event_store.save_memories(agent_role, updated_memories)
    ```
  - `server/event_store.py` lines 184-198:
    ```python
    def save_memories(self, agent: str, memories: list[str]) -> None:
        """Save list of atomic memories for the specified agent."""
        import json
        memories_json = json.dumps(memories)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO agent_memories (agent, memories_json) VALUES (?, ?)",
                    (agent, memories_json),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    ```

### 4.2 Invariant 3: Isolated Read Path via GSA (VIOLATED)
* **Specification Violated**: `obsidian-vault/01 - Philosophy and Topology.md` (Section 2.5 "Global State Agent (GSA)" & Section 1.11 Principle 3 / Principle 12)
* **Violation**: According to Core Principle 1.11 (Principle 12) and Section 2.5, GSA (port 8000) must be the *sole* component reading the database. However, agents directly call `event_store.read_all()` on the database file in two critical locations:
  1. Inside `read_last_n_effects` (line 263), which is executed during *every* agent turn.
  2. Inside `start_autonomous_loop` (line 1151) when checking script reconciliation failure for the `scenario` agent.
* **Citations**:
  - `server/agent_base.py` lines 260-265:
    ```python
    def read_last_n_effects(agent: str, n: int) -> list[Effect]:
        """Read last n events generated by the specified agent from the store."""
        try:
            all_events = event_store.read_all()
    ```
  - `server/agent_base.py` lines 1148-1151:
    ```python
    if role == "scenario":
        slots = state.get("otio", {}).get("slots", {})
        if slots:
            all_events = event_store.read_all()
    ```

### 4.3 Invariant 5: No Timeouts in Production Code (VIOLATED)
* **Specification Violated**: `obsidian-vault/01 - Philosophy and Topology.md` (Section 1.4 "No Timeouts in Code") & `obsidian-vault/08 - Testing, Concurrency, and Rollout.md` (Section 3.2 "Timeout Policy")
* **Violation**: Production execution paths contain two hardcoded timeouts on HTTP calls and subprocess probes without `# health probe` comments or `health`/`probe` keyword labeling to bypass validation.
* **Citations**:
  - `server/effect_parser.py` line 603 (http get inside validation path):
    ```python
    resp = await client.get("http://127.0.0.1:8000/", timeout=1.5)
    ```
  - `server/otio_timeline_model.py` line 630 (ffprobe media duration check):
    ```python
    result = subprocess.run(
        ...
        timeout=30,
    )
    ```

### 4.4 Agent Factory Thinking Parameter Discrepancy
* **Specification Violated**: `obsidian-vault/04 - Agent Architecture and Systems.md` (Section 3.1 "Factory Function: create_pipeline_agent")
* **Violation**: The specifications state that the factory function `create_pipeline_agent` should define `thinking=True`. However, in the codebase it is initialized with `thinking=False`.
* **Citations**:
  - `server/agent_base.py` line 578:
    ```python
    thinking=False,
    ```

### 4.5 Violations Caught by static checker (`cheat_check.py`)
* **Fixed Polling Loops**: `agent_base.py` has two fixed sleep commands within looping threads without dynamic backoff or reasoning documented:
  - Line 1112: `await asyncio.sleep(2.0)`
  - Line 1130: `await asyncio.sleep(poll_interval)`
* **Swallowed Exceptions**: Numerous `except Exception:` handlers swallow exceptions using `pass` or `logger.debug` without notifying the maintainer via `notify_maintainer`:
  - `server/effects.py` line 42 (`pass` inside `parse_duration`)
  - `server/agent_base.py` lines 306, 835, 872, 909, 951, 996, 1007, 1036, 1068, 1103, 1109, 1209
  - `server/otio_timeline_model.py` lines 305, 546, 635
  - `server/slot_detail_model.py` lines 225, 236, 332, 339, 344, 444
  - `server/projections.py` lines 68, 301
  - `server/effect_parser.py` line 684
  - `server/critique/store.py` lines 318, 357
