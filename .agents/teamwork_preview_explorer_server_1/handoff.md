# Handoff Report — Compliance Check of server/

This handoff report summarizes the comprehensive paragraph-to-paragraph compliance check of all Python files under `server/` against the technical specifications in the `obsidian-vault/` directory.

---

## 1. Observation

### 1.1 Structural Schema Mismatch (Undocumented Subclasses & Fields)
* **Observed File**: `server/effects.py`
  - Defines 12 undocumented subclasses of Job-related effects (lines 120-230) not specified in `obsidian-vault/02 - Event Store and Effect Schemas.md`. For example, `QueueAudioJob` is defined as:
    ```python
    class QueueAudioJob(QueueJob):
        kind: Literal["queue_audio_job"] = "queue_audio_job"
        job_type: Literal["tts"] = "tts"
    ```
  - Defines an extra undocumented field `start_sec` in `MergeIntoOTIO` class:
    ```python
    start_sec: float = Field(default=0.0, description="Optional start time coordinate for coordinate-based schema")
    ```
* **Observed File**: `server/event_store.py`
  - Initializes a separate undocumented database table `agent_memories` inside the `events.db` SQLite file (lines 46-51):
    ```python
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_memories (
            agent TEXT PRIMARY KEY,
            memories_json TEXT NOT NULL
        )
    """)
    ```

### 1.2 Isolated Read Path Violations
* **Observed File**: `server/agent_base.py`
  - Instantiates `event_store` directly and queries it directly inside `read_last_n_effects` (line 263) and inside `start_autonomous_loop` (line 1151):
    ```python
    all_events = event_store.read_all()
    ```

### 1.3 Direct DB Memory Mutations (Sole Source of Truth Violations)
* **Observed File**: `server/agent_base.py`
  - Writes memories directly to `events.db` via `save_memories` (line 628):
    ```python
    event_store.save_memories(agent_role, updated_memories)
    ```
* **Observed File**: `server/event_store.py`
  - Defines custom database writing logic for the memories table (lines 184-198):
    ```python
    def save_memories(self, agent: str, memories: list[str]) -> None:
        ...
        conn.execute("INSERT OR REPLACE INTO agent_memories (agent, memories_json) VALUES (?, ?)", ...)
    ```

### 1.4 Prohibited Timeouts on Production Execution Paths
* **Observed File**: `server/effect_parser.py`
  - Utilizes a timeout without health/probe labeling on a production validation path (line 603):
    ```python
    resp = await client.get("http://127.0.0.1:8000/", timeout=1.5)
    ```
* **Observed File**: `server/otio_timeline_model.py`
  - Utilizes a timeout of 30 seconds on the primary media probe subprocess execution path without health/probe labeling (line 630):
    ```python
    result = subprocess.run(..., timeout=30)
    ```

### 1.5 Agent Factory Thinking Parameter Discrepancy
* **Observed File**: `server/agent_base.py`
  - Factory function `create_pipeline_agent` initializes with `thinking=False` (line 578), whereas the spec `04 - Agent Architecture and Systems.md` requires `thinking=True`.
    ```python
    thinking=False,
    ```

### 1.6 Compliance Scanner (cheat_check.py) Output
* **Command Run**: `.venv/bin/python server/cheat_check.py server`
* **Result**: Output identified 34 violations including swallowed exceptions (using `pass` or `logger.debug` in `except` blocks without notifying maintainers) and fixed loops (`await asyncio.sleep(2.0)` at line 1112 and `await asyncio.sleep(poll_interval)` at line 1130 in `agent_base.py`).

---

## 2. Logic Chain

1. **Undocumented Schema Structures**: By comparing Pydantic classes and database definitions against `02 - Event Store and Effect Schemas.md`, we find that the codebase defines 12 undocumented effect models (`QueueAudioJob`, `AudioJobStarted`, etc.), an extra `start_sec` property on `MergeIntoOTIO`, and an additional SQLite table `agent_memories`. Each is a structural mismatch.
2. **Database Isolation Violations**: Invariant 3 states that GSA is the sole component reading the event database, and all other agents must query GSA via `GET /`. However, because `agent_base.py` calls `event_store.read_all()`, agents read directly from `events.db` at runtime.
3. **Sole Source of Truth Violations**: Invariant 1 dictates that all system state must fold passively over the event log. Storing agent memories in a separate table (`agent_memories`) via SQL inserts/updates bypasses the event log, making it impossible to restore memories through event replay.
4. **Timeout Policy Violations**: Invariant 5 and Section 3.2 of `08 - Testing, Concurrency, and Rollout.md` strictly prohibit timeouts on production execution paths. Probing exceptions must contain `health`/`probe` labels. Since the timeouts in `effect_parser.py` and `otio_timeline_model.py` lack these labels and run on production paths, they violate this policy.
5. **Thinking Parameter Discrepancy**: By comparing the arguments passed to `create_deep_agent` in `agent_base.py` against Section 3.1 of `04 - Agent Architecture and Systems.md`, we find that `thinking=False` deviates from the specified `thinking=True` parameter.
6. **Static Compliance Scanner Findings**: Running `cheat_check.py` on the `server` directory returns 34 compliance violations, validating the presence of swallowed exceptions and fixed loop sleeps.

---

## 3. Caveats

* **No Code Modification**: In line with the read-only constraint of the Explorer role, no compliance fixes have been implemented in the codebase.
* **Offline Mode**: Some integration tests (e.g., `test_real_assembly_bdd.py`) require external APIs (e.g. OpenAI, Vast.ai) which will fail locally without active internet access and credentials. However, all mock/in-memory BDD tests were successfully run and verified.
* **Workers Scope**: VM Workers under `scripts/` (e.g. `scripts/tts_worker.py`) were excluded as they are outside the `server/` directory boundaries.

---

## 4. Conclusion

The `server/` codebase has multiple structural mismatches and direct violations of the core invariants established in the Obsidian Vault:
1. **Isolated Read Path via GSA** is violated by direct `read_all()` database queries during agent turns.
2. **Event Log as Sole Source of Truth** is violated by mutable SQL operations on the custom `agent_memories` table.
3. **No Timeouts** policy is violated by undocumented HTTP and subprocess timeouts on active production routes.
4. **Schema Alignment** is compromised by 12 undocumented subclasses of Job-related effects and the undocumented `start_sec` field.
5. **Agent Capabilities Configuration** has a mismatch with `thinking=False` set instead of `thinking=True` on DeepAgent.

---

## 5. Verification Method

* **Cheat Scanner Verification**:
  ```bash
  .venv/bin/python server/cheat_check.py server
  ```
  Expected: Returns exit code 1 with 34 violations (excluding `cheat_check.py` self-matches).
* **BDD Integration Tests**:
  ```bash
  .venv/bin/pytest tests/units/test_coordinate_timeline_bdd.py tests/units/test_concurrency_intervention_bdd.py tests/units/test_pipeline_faults_bdd.py tests/units/test_provisioning_happy_path_bdd.py
  ```
  Expected: All 12 test cases pass successfully.
* **Inspect Key Files**:
  - `server/effects.py`: Check lines 120-230 and line 402 for undocumented subclasses/fields.
  - `server/agent_base.py`: Inspect line 263 (`read_last_n_effects`), line 578 (`thinking=False`), and line 1151 (`start_autonomous_loop`) for direct `read_all()` database queries and deep agent parameters.
  - `server/event_store.py`: Check lines 184-211 (`save_memories`) and lines 46-51 (`agent_memories` table schema) for direct SQL mutations.
