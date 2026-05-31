> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Pipeline Architecture Deep Dive — Event Sourcing Design Foundation

## 1. Execution Flow (Entry → Graph → Nodes → Agents → Tools → Return)

### 1.1 Entry Point
- **`run_strands.py:run_documentary()`** is the main async entry.
- It creates a fresh OTIO timeline file (`documentary_draft.otio`) with 3 canonical tracks: V1_Video, A1_Narration, A2_Music.
- Sets `PIPELINE_DIR` env var, acquires a PID-based file lock, runs preflight checks.
- Builds the Graph via `build_documentary_graph(hooks=..., model=...)`.
- Calls `await shell.run(brief, initial_state={"_timeline_path": timeline_path})`.

### 1.2 Graph Construction (`build_documentary_graph`)
- **5 nodes** are created as `GraphNode(node_id=..., executor=Agent(...))`:
  1. `scenario` — scenario_agent
  2. `otio` — otio_gate_agent
  3. `audio` — audio_agent
  4. `video` — video_agent
  5. `assembly` — assembly_agent

- **Forward edges** (scenario → otio → audio → otio → video → otio → assembly → otio):
  - Each forward edge from OTIO to a stage has a `_*_not_completed` condition.
  - These conditions check `RecoveryShell.completed_stages` **and** OTIO disk state (clips, metadata) to decide whether to skip.

- **Backward edges** (recovery loops):
  - OTIO → scenario/audio/video/assembly when validation fails.
  - Conditions: `_needs_*_retry` checks the gate agent's output for `recovery_target`.

- **Graph config**: `reset_on_revisit=True`, `max_node_executions=50/200`, `id="documentary_pipeline"`.

### 1.3 Graph Execution Engine (Strands `Graph` class)
- **`RecoveryShell.run()`** → `graph.invoke_async(task)` → `graph.stream_async()` → `_execute_graph()`.
- `_execute_graph()` maintains a `ready_nodes` list and runs them in **parallel batches** via `_execute_nodes_parallel()`.
- `_execute_nodes_parallel()` creates async tasks per node, each streaming events into a shared `asyncio.Queue`.
- **Fail-fast**: any exception in any node cancels all other tasks and stops the graph.

### 1.4 Node Execution (`_execute_node`)
- Before execution: hooks fire `BeforeNodeCallEvent` (can cancel_node).
- If `reset_on_revisit=True` and node was previously completed, `node.reset_executor_state()` is called — **agent messages, state, and model_state are wiped**.
- Node input is built by `_build_node_input()`:
  - For entry nodes: the original task string.
  - For dependent nodes: a formatted text block combining "Original Task" + "Inputs from previous nodes" (flattened agent results from completed predecessors).
- The executor is invoked via `node.executor.stream_async(node_input, invocation_state=invocation_state)`.
- After execution: hooks fire `AfterNodeCallEvent`. Node is marked COMPLETED/FAILED.

### 1.5 Agent Execution (Strands `Agent` class)
- Each agent is a `strands.Agent` with a `system_prompt`, `tools` list, and `model`.
- The agent receives the node input as a message, reasons via LLM, and calls tools.
- **Tool context**: some tools use `@tool(context=True)` to receive the agent's `ToolContext`, giving access to `tool_context.agent.state`.
- Agents are **stateful internally** (messages, state dict, model_state), but the pipeline design aims for statelessness by resetting them.

### 1.6 Tool Execution
- Tools are plain Python functions decorated with `@tool`.
- Tools read/write the OTIO file directly via `resolve_timeline_path()`.
- Tools may call external services (Vast.ai CLI, TTS workers, video workers).
- Tools may also checkpoint by copying the OTIO file to `checkpoint_dir(run_id)/agents/{stage}/`.

### 1.7 Return Path
- `shell.run()` returns the `GraphResult` (or raises on failure after max_retries).
- `run_documentary()` verifies `master.mp4` exists before declaring success.
- Finally block: commits agent memory to git, destroys VMs, stops auto-tracer, releases lock.

---

## 2. State Model (What Data Exists, Where It Lives, How It Flows)

### 2.1 GraphState (Runtime, In-Memory Only)
```python
@dataclass
class GraphState:
    task: MultiAgentInput = ""
    status: Status = Status.PENDING
    completed_nodes: set[GraphNode] = field(default_factory=set)
    failed_nodes: set[GraphNode] = field(default_factory=set)
    interrupted_nodes: set[GraphNode] = field(default_factory=set)
    execution_order: list[GraphNode] = field(default_factory=list)
    results: dict[str, NodeResult] = field(default_factory=dict)
    accumulated_usage: Usage = field(default_factory=lambda: Usage(...))
    accumulated_metrics: Metrics = field(default_factory=lambda: Metrics(...))
    execution_count: int = 0
    execution_time: int = 0
```
- **Transient**: GraphState is recreated on every `invoke_async()` call. There is no persistence of GraphState between runs.
- **Communication mechanism**: Nodes do NOT share state through GraphState. The only inter-node data flow is `_build_node_input()`, which formats predecessor results as text strings.

### 2.2 Agent Internal State (Per-Node, Reset on Revisit)
- `agent.messages` — conversation history.
- `agent.state` — `AgentState` dict (key-value bag).
- `agent._model_state` — LLM-specific state.
- **Volatile**: With `reset_on_revisit=True`, all of this is deep-copied from initial values and wiped on every revisit.

### 2.3 OTIO File on Disk (The "Real" State)
- **Single source of truth** for the pipeline. The code explicitly says: "Data flows through the OTIO file on disk (stateless). No shared Python state."
- Contains:
  - Timeline structure with tracks and clips.
  - `timeline.metadata["documentary"]` — pipeline metadata: `scenes`, `visual_style`, `style_lock`, `whisperx_alignment`, `assembly_output_path`, `gate_*`, `lifecycle_state`, etc.
- Each metadata write includes provenance (timestamp, agent, tool).

### 2.4 Checkpoint Directory Layout
```
{PIPELINE_DIR}/checkpoints/{run_id}/
├── otio/              → OTIO timeline drafts and authoritative files
├── agents/            → Per-agent working state and outputs
│   ├── scenario/scenario_timeline.otio
│   ├── audio/audio_timeline.otio
│   ├── video/video_timeline.otio
│   └── assembly/assembly_timeline.otio
├── renders/           → Final and intermediate video renders
├── previews/          → QA preview artifacts
├── logs/              → Execution logs and critique records
└── metadata.json      → Run-level metadata envelope
```
- `metadata.json` contains `completed_stages: ["scenario", "audio", ...]` — the deduplicated list of finished stages.

### 2.5 RecoveryShell State
```python
class RecoveryShell:
    graph: Graph | None
    max_retries: int = 3
    _recovery_count: int = 0
    resume: bool = False          # ALWAYS False in current code
    run_id: str = ""
    latest_checkpoint: str = ""   # ALWAYS "" in current code
    completed_stages: list[str] = []
```
- `RecoveryShell` is a **global singleton** (`_recovery_shell`).
- In `build_documentary_graph()`, it is initialized with `resume=False` and empty checkpoint fields. The comment says: "NO CHECKPOINTS: every run starts from scratch."

### 2.6 ToolGatekeeper State (Advisory VM Tracking)
```python
class ToolGatekeeper:
    _vms: dict[str, _VmLifecycle]  # role → VM state
```
- Tracks: `state` (none/provisioning/running/healthy/error), check counts, attempt counts, errors.
- **Purely advisory** — agents query `get_state()` and decide themselves. No hard blocks.

### 2.7 Auto-Trace DB (SQLite WAL)
```sql
CREATE TABLE runs (run_id, topic, start_ts, end_ts, status);
CREATE TABLE calls (id, run_id, ts, elapsed_ms, func, module, event, duration_ms, parent_func);
```
- Captures every Python function call/return in `/server/` via `sys.monitoring`.
- **Background flush** every 3 seconds via ring buffer swap.

---

## 3. Current Resumability (What Works, What Doesn't)

### 3.1 What Works
1. **Disk-based idempotency within a single run**: Tools like `generate_scene_narration` check if WAV files already exist and skip regeneration. `submit_gpu_production_job` checks for existing MP4s. This prevents duplicate work **during** a run.

2. **OTIO gate validation with backward edges**: If a stage produces invalid output, the gate detects it and the graph routes backward to retry. This is **intra-run recovery**, not cross-run resume.

3. **Stage skip logic via edge conditions**: The `_*_not_completed` conditions check both `RecoveryShell.completed_stages` and actual OTIO disk state. If clips exist, the stage is skipped. This **would** support resume if `completed_stages` were persisted.

4. **Checkpoint file writes**: Each agent has `save_*_checkpoint()` which copies the OTIO timeline to `checkpoint_dir/{run_id}/agents/{stage}/`. `_update_completed_stages()` appends to `metadata.json`.

5. **B2 checkpoint infrastructure**: `InMemoryB2CheckpointStore` and `LiveB2CheckpointStore` exist with content-addressed uploads, sha256 verification, idempotency keys, and monotonic revision guards. `resume()` in `b2_checkpoint/resume.py` can reconstruct `ResumeState` from a manifest.

6. **Agent memory tools**: `remember()` / `recall_memory()` persist learnings across runs via `agent_memory` module (git-backed).

### 3.2 What Doesn't Work (Critical Gaps)
1. **`resume` is hardcoded to `False`**: In `build_documentary_graph()` line 349-354, `RecoveryShell` is created with `resume=False`, `latest_checkpoint=""`, `completed_stages=[]`. There is no code path that enables resume.

2. **GraphState is never persisted**: Strands `Graph` has `serialize_state()` and `deserialize_state()` methods, but `SessionManager` is never attached to the graph. The pipeline creates the graph without a session manager.

3. **`RecoveryShell.seed_timeline()` is dead code**: `seed_timeline()` copies a checkpoint to the working timeline path, but it's only called if `self.resume and self.latest_checkpoint`, which never happens.

4. **`_recovery_shell` is recreated on every `build_documentary_graph()` call**: Since the graph is rebuilt per run, any in-memory completed_stages are lost.

5. **No event log for graph transitions**: There is no append-only log of node executions, edge traversals, tool calls, or state mutations. The auto-trace DB captures function calls but not semantic pipeline events.

6. **B2 checkpointing is NOT wired into the main pipeline**: The `CheckpointHook` tries to call `otio_manager.checkpoint()`, but `run_strands.py` never initializes a B2 store or calls the resume path. The B2 system exists in `b2_checkpoint/` but is orphaned from `graph_pipeline.py`.

7. **Race condition in OTIOStateManager**: `_write_timeline()` has mtime comparison to avoid clobbering, but there is no atomic write or file locking. Concurrent agents could corrupt the OTIO file.

8. **`reset_on_revisit=True` destroys agent memory within a run**: If a node is revisited (e.g., audio retry after gate failure), the agent's conversation history and internal state are wiped. The agent must rediscover everything from the OTIO file.

---

## 4. Where Event Sourcing Hooks Would Attach

### 4.1 Strands Hook System (Already Present)
The pipeline registers hooks on a `HookRegistry`. Available events:
- `BeforeNodeCallEvent` — can `cancel_node`
- `AfterNodeCallEvent` — read `node_id`, `invocation_state`
- `BeforeToolCallEvent` — can `cancel_tool`
- `MultiAgentInitializedEvent`, `MultiAgentHandoffEvent`, etc.

Current hooks: `BudgetHook`, `ApprovalGateHook`, `ImmutabilityHook`, `ShellGuardHook`.

### 4.2 Ideal Event Sourcing Hook Points

| Hook Point | Event Type | What to Log |
|---|---|---|
| Graph start | `BeforeMultiAgentInvocationEvent` | run_id, brief, model, timestamp, graph config |
| Node start | `BeforeNodeCallEvent` | node_id, input_summary, graph_state snapshot |
| Tool call | `BeforeToolCallEvent` | tool_name, args (sanitized), node_id, timestamp |
| Tool return | Custom hook needed | tool_name, result_status, duration, node_id |
| Node complete | `AfterNodeCallEvent` | node_id, result_status, execution_time, token usage |
| Edge traversal | `MultiAgentHandoffEvent` | from_node, to_node, condition_result |
| OTIO mutation | Instrument `OTIOStateManager` | operation, key, old_value, new_value, provenance |
| Checkpoint save | Instrument `save_*_checkpoint` | stage, checkpoint_path, metadata.json state |
| VM lifecycle | Instrument `ToolGatekeeper` | role, event (provision/status/health/destroy), state transition |
| Graph end | `AfterMultiAgentInvocationEvent` | final_status, accumulated metrics, completed_nodes |

### 4.3 OTIOStateManager as Event Source
- `set_pipeline_metadata()`, `add_clip()`, `guard_mutation()`, `set_authoritative()`, `begin_escalation()`, `end_escalation()` are the natural places to emit domain events.
- Each mutation should produce an immutable event: `{event_type, timestamp, run_id, operation, payload, provenance}`.

### 4.4 Graph State Serialization Gaps
- `Graph.serialize_state()` captures: completed_nodes, failed_nodes, interrupted_nodes, node_results, execution_order, next_nodes_to_execute, current_task, interrupt_state.
- **Missing from serialize_state**: edge condition evaluation results, tool call history, OTIO metadata at each point, checkpoint references, VM states.
- The serialized state is a **snapshot**, not an event log. For true event sourcing, we need an append-only log of events that can rebuild state.

### 4.5 Auto-Trace DB Gaps for Event Sourcing
- Current schema captures: `func, module, event, duration_ms, parent_func`.
- **Missing**: semantic event type ("node_started", "tool_called"), node_id, tool_name, run_id correlation, arguments, results, state diffs, checkpoint IDs.
- The auto-trace is a **function-level profiler**, not a **business event log**.

---

## 5. Key Architectural Observations

### 5.1 "Stateless" Design with Stateful Gaps
- The pipeline **claims** statelessness (all data in OTIO file), but:
  - `RecoveryShell.completed_stages` is in-memory only.
  - `ToolGatekeeper._vms` is in-memory only.
  - Graph `invocation_state` is passed but not persisted.
  - Agent internal state exists but is reset.

### 5.2 Two Checkpoint Systems, Neither Complete
1. **Local file checkpoints**: `metadata.json` + per-agent `.otio` copies. Written by agents themselves. No automatic recovery from these.
2. **B2 remote checkpoints**: Content-addressed, verified, resume-capable. Exists but is **not wired into the main pipeline**.

### 5.3 The OTIO File as Ambiguous State Store
- OTIO serves as: timeline structure, pipeline metadata store, checkpoint artifact, and cross-agent communication bus.
- **Problem**: It's a mutable file, not an append-only log. Overwrites lose history. The mtime guard in `_write_timeline()` is rudimentary.

### 5.4 Hook-Based Enforcement vs. Agent Autonomy
- Hooks like `ImmutabilityHook` and `ShellGuardHook` can cancel tools/nodes.
- `ApprovalGateHook` can block stages pending human approval.
- `BudgetHook` tracks costs but is explicitly noted as "log-only" because hard aborts are dangerous.
- This creates a **layered control architecture**: agents decide, hooks veto.

### 5.5 RecoveryShell is Misnamed
- `RecoveryShell.run()` does **retry loops** on `graph.invoke_async()`, but:
  - It does NOT resume from checkpoints.
  - It seeds `_recovery_target` and `_recovery_reason` into `initial_state`, but `initial_state` is not deeply used by the graph.
  - `seed_timeline()` is dead code.

---

## 6. Summary for Event Sourcing Design

To add event sourcing to this pipeline, you would need to:

1. **Define the event schema**: `PipelineStarted`, `NodeStarted`, `ToolCalled`, `ToolReturned`, `NodeCompleted`, `EdgeTraversed`, `OtioMutated`, `CheckpointSaved`, `VmLifecycleChanged`, `PipelineEnded`.

2. **Add an event store**: Append-only log (file, SQLite, or external) that captures events at the hook points and OTIOStateManager mutations.

3. **Persist GraphState as events, not snapshots**: Instead of `serialize_state()` producing a blob, produce a stream of events that `deserialize_state()` can replay.

4. **Wire B2 checkpoints into the pipeline**: Connect `CheckpointHook` to actually upload after each stage, and initialize `RecoveryShell` with `resume=True` + manifest data on restart.

5. **Make `RecoveryShell` stateful across runs**: Persist `completed_stages`, `latest_checkpoint`, and `run_id` to durable storage (metadata.json is already there — just use it).

6. **Instrument `ToolGatekeeper`**: Emit events on every VM state transition so provisioning history is recoverable.

7. **Extend auto-trace or replace it**: Add a semantic event table alongside the function-call table, capturing agent decisions and pipeline milestones.

8. **Consider OTIO file versioning**: Instead of overwriting the OTIO file, write versioned snapshots and reference them from events.

---

## 7. Supplementary Findings from Code Read (2026-05-23)

### 7.1 SnapshotHook / SnapshotStore — Already Implemented Event Sourcing Layer
The pipeline already has a **working event-sourcing store** (`tracing/snapshot_store.py` + `tracing/snapshot_hooks.py`) that the plan under-emphasizes:

- **Schema**: SQLite WAL table `snapshots` with columns `(run_id, timestamp, event_type, agent_name, payload, sequence_num)`.
- **Event types**: `tool_call`, `llm_turn`, `graph_transition`, `vm_state`, `otio_state`, `file_state`, `decision`.
- **Writes**: Every `BeforeToolCallEvent` / `AfterToolCallEvent`, `BeforeNodeCallEvent` / `AfterNodeCallEvent`, `BeforeModelCallEvent` / `AfterModelCallEvent` is captured by `SnapshotHook` (a `HookProvider`).
- **LLM fidelity**: Records full `messages`, `params`, `response_text`, `usage`, `duration_ms`.
- **Tool fidelity**: Records `tool_name`, `args`, `result`, `duration_ms`, `agent`.
- **File state**: After every node, `_scan_artifacts()` walks `timelines/`, `renders/`, `audio/`, `video/`, `checkpoints/` and records path → (size, mtime).
- **Resume API**: `reconstruct_state(run_id)` walks the snapshot stream backward, overlaying the latest `otio_state`, `vm_state`, `file_state`, `decision`, `llm_turn`, `tool_call`, and `graph_history` into a single `ResumeContext`.
- **Gap**: This hook is **not registered** in `run_strands.py`. The hooks list is `[ImmutabilityHook(), ApprovalGateHook(), ShellGuardHook(), BudgetHook()]`. `SnapshotHook` exists but is orphaned — it must be added to the `hooks=` list in `build_documentary_graph()` to activate.

### 7.2 Edge Condition Logic — Dual Check Pattern
All forward-edge conditions (`_scenario_not_completed`, `_audio_not_completed`, `_video_not_completed`, `_assembly_not_completed`) follow the same dual-check pattern:

1. **In-memory**: `if shell and STAGE in shell.completed_stages: return False`
2. **Disk fallback**: If `completed_stages` is empty (which it always is today because `resume=False`), the condition falls back to inspecting the OTIO file directly — clips on tracks, metadata keys, or WAV/MP4 files on disk.

This means the skip logic **already works** for idempotency within a run, but cross-run resume is disabled because `RecoveryShell` is never initialized with `resume=True`.

### 7.3 RecoveryLedger — Separate Mutable State for Production SubAgent
`strands_agents/recovery.py` contains `_RecoveryLedger`, a **thread-safe, process-wide singleton** (`_LEDGER`) tracking per-scene retry/fix/skip budgets:

- `RETRY_BUDGET = 2`, `FIX_BUDGET = 1` per scene.
- Tools: `retry_scene`, `fix_scene`, `skip_scene`, `request_escalation`.
- **Volatile**: Not persisted. Reset only via `ledger.reset()`.
- If the pipeline crashes mid-production, all retry/fix counters are lost.

### 7.4 Pipeline Hooks — 8 Concrete Implementations
`strands_agents/hooks/pipeline_hooks.py` defines:

1. `StageContractHook` — validates pre/post conditions from a contract dict; can `cancel_node`.
2. `ImmutabilityHook` — blocks tools in `MUTATION_TOOLS` (overwrite_audio, delete_clip, etc.).
3. `BudgetHook` — accumulates `_stage_cost` from `invocation_state`; **log-only** (does not cancel).
4. `ApprovalGateHook` — cancels nodes unless `state[f"_approved_{node_id}"]` is True.
5. `ScopeHook` — informational; logs out-of-scope keys per stage.
6. `QANodeHook` — placeholder; logs QA check start.
7. `CheckpointHook` — calls `otio_manager.checkpoint(f"after_{node_id}")` if available.
8. `ShellGuardHook` — allowlists binaries for shell tools (ffprobe, ffmpeg, python3, etc.).

### 7.5 Auto-Tracer — Ring Buffer + Batched Inserts
`tracing/auto_trace.py` uses `sys.monitoring` (Python 3.12+) with a **double-buffer ring**:

- Two lists swap: `_active` (monitor writes) ↔ `_drain` (background thread reads).
- Lock held only during swap.
- `_FLUSH_INTERVAL = 3.0s`, `_BATCH_SIZE = 500`.
- Schema: `runs(run_id, topic, start_ts, end_ts, status)` + `calls(id, run_id, ts, elapsed_ms, func, module, event, duration_ms, parent_func)`.
- **Gap**: Captures every Python call/return in `/server/`, but no semantic node_id, tool_name, or run_id correlation beyond the run table. The `snapshot_store` table is the semantic layer, but it's not wired.

### 7.6 OTIOStateManager — Mutation Guard with mtime Check
`otio_manager.py` `_write_timeline()` has an **mtime-based stale-cache guard**:

```python
if current_mtime != self._timeline_mtime:
    logger.warning("...changed on disk since last refresh...")
    self.refresh_from_disk()
    return
```

- This prevents clobbering but is **not atomic** — no file locking.
- `guard_mutation()` raises `OtioStateViolation` if `state == authoritative` and no escalation is active.
- `set_pipeline_metadata()` wraps every value as `{"value": ..., "timestamp": ..., "provenance": ...}`.

### 7.7 What Is Needed to "Resume from Any Exact Moment"
To resume from any exact moment, the following state would need to be snapshotted and restored:

| State | Current Persistence | Needed for Resume |
|---|---|---|
| `GraphState` (completed_nodes, failed_nodes, results) | **None** — recreated per `invoke_async()` | Persist as events or snapshots |
| `Agent.messages` / `agent.state` / `agent._model_state` | **None** — reset on revisit | If `reset_on_revisit=True`, this is intentionally wiped; for true resume, need to save/restore or accept re-derivation from OTIO |
| `RecoveryShell.completed_stages` | `metadata.json` (written) but `resume=False` | Read `metadata.json` on startup; set `resume=True` |
| `RecoveryShell.latest_checkpoint` | **None** | Point to the last successful stage checkpoint |
| `OTIO file` | Disk + checkpoint copies | Seed working timeline from checkpoint |
| `OTIOStateManager._otio_state` | In-memory | Persist with checkpoint or derive from OTIO file |
| `OTIOStateManager._escalation` | In-memory | Persist if mid-escalation resume is needed |
| `ToolGatekeeper._vms` | **None** | Persist VM IDs, IPs, ports, health state |
| `RecoveryLedger` (retry/fix/skip counts) | **None** | Persist per-scene budgets |
| `SnapshotStore` events | SQLite WAL | Already there; just wire the hook |
| `Worker VMs` (external) | Vast.ai API | Re-query Vast.ai on resume; do not reprovision if still running |
| `File outputs` (WAV, MP4) | Disk | Check existence; skip regeneration |
| `LLM conversation context` | **None** | For deterministic replay, record all LLM requests/responses (SnapshotStore already does) |

**Minimal viable resume**: Wire `SnapshotHook`, read `metadata.json` + last checkpoint OTIO, set `RecoveryShell(resume=True, completed_stages=..., latest_checkpoint=...)`, re-query Vast.ai for active VMs, and let the graph's existing skip logic handle the rest.

---

## 8. Verification Notes from Code Read (2026-05-23)

### 8.1 `RecoveryShell.run()` — `state_overrides` is dead code
Reading `graph_pipeline.py:232-271` confirms:
- `state_overrides` is built with `_recovery_target` and `_recovery_reason`
- But `self.graph.invoke_async(task)` is called **without** any state argument
- `initial_state` parameter of `run()` is never passed to the graph either
- The retry loop re-runs the graph from scratch with the same task string every time

### 8.2 `CheckpointHook` is silently no-op
Reading `pipeline_hooks.py:258-268`:
- `otio_manager = state.get("otio_manager")` — but `OTIOStateManager` is injected into stage modules directly (`_audio_stage_mod._otio_manager = _shared_otio_manager`), not placed into `invocation_state`
- Result: `CheckpointHook` always hits the `else` branch and logs at DEBUG level

### 8.3 Hook registration has two patterns
Reading `Graph.__init__` and `HookRegistry.add_callback` source:
- Pattern A: `register_hooks(registry)` explicitly calls `registry.add_callback(EventType, handler)`
- Pattern B: Convention-based — `HookRegistry` infers event types from method signatures (`on_before_node_call`, `on_after_node_call`, `on_before_tool_call`)
- `StageContractHook`, `ScopeHook`, `QANodeHook`, `CheckpointHook` use Pattern B
- `ImmutabilityHook`, `BudgetHook`, `ApprovalGateHook`, `ShellGuardHook` use Pattern A

### 8.4 Event attributes confirmed
From `strands.hooks` introspection:
- `BeforeNodeCallEvent`: `cancel_node`, `interrupt`, `invocation_state`, `should_reverse_callbacks`
- `AfterNodeCallEvent`: `invocation_state`, `should_reverse_callbacks`
- `BeforeToolCallEvent`: `cancel_tool`, `interrupt`, `should_reverse_callbacks`
- Note: there is **no** `AfterToolCallEvent` exposed in the public API at the `strands.hooks` level. Tool returns are captured inside the Agent runtime, not through a hook event.

### 8.5 `_execute_nodes_parallel()` fail-fast mechanism
Reading `Graph._execute_nodes_parallel` source:
- Creates `asyncio.Queue` and one task per node
- Consumes with `asyncio.wait_for(event_queue.get(), timeout=0.1)`
- If any task raises `Exception`, all other tasks are cancelled immediately
- `finally` block cancels any remaining tasks and gathers with `return_exceptions=True`

### 8.6 Node input construction
Reading `Graph._build_node_input` source:
- `node_input = self._build_node_input(node)` — for dependent nodes, this formats "Original Task" + "Inputs from previous nodes" as a single text block
- The only inter-node data flow is this formatted text string
- No Python objects, dicts, or structured state is passed between nodes

### 8.7 `reset_on_revisit=True` behavior confirmed
Reading `Graph._execute_node` source:
```python
if self.reset_on_revisit and node in self.state.completed_nodes:
    node.reset_executor_state()
    self.state.completed_nodes.remove(node)
```
- `reset_executor_state()` deep-copies initial values back into the agent, wiping messages, state, and model_state
- This means on backward-edge retry, the agent starts completely fresh

---

## 9. Implementation Plan: Stage-Level Snapshotting with pyeventsourcing

### 9.1 Core Insight: The Pipeline IS an Event-Sourced Aggregate
The documentary pipeline is a single long-running aggregate that evolves through stages. Each stage completion is a domain event. The OTIO file is the aggregate state. The checkpoint system is a manual snapshotting mechanism.

**pyeventsourcing** provides the infrastructure we need:
- `Aggregate` base class with `@event` decorator for domain events
- `Application` base class for event store and repository
- Snapshotting to avoid replaying thousands of events
- SQLite persistence module (already used for traces)

### 9.2 The Aggregate: `DocumentaryPipeline`

```python
from eventsourcing.domain import Aggregate, event

class DocumentaryPipeline(Aggregate):
    """Event-sourced aggregate representing one pipeline run."""
    
    @event("Started")
    def __init__(self, brief: str, model_id: str, run_id: str) -> None:
        self.brief = brief
        self.model_id = model_id
        self.run_id = run_id
        self.completed_stages: list[str] = []
        self.current_stage: str = "scenario"
        self.vm_registry: dict[str, dict] = {}
        self.checkpoints: dict[str, str] = {}  # stage → checkpoint_path
        self.tool_calls: list[dict] = []
        self.status = "running"
    
    @event("StageCompleted")
    def complete_stage(self, stage: str, checkpoint_path: str) -> None:
        if stage not in self.completed_stages:
            self.completed_stages.append(stage)
        self.checkpoints[stage] = checkpoint_path
        self.current_stage = self._next_stage(stage)
    
    @event("ToolCalled")
    def record_tool_call(self, agent: str, tool: str, args: dict, 
                         result: dict, duration_ms: float) -> None:
        self.tool_calls.append({
            "agent": agent, "tool": tool, "args": args,
            "result": result, "duration_ms": duration_ms,
            "timestamp": time.time(),
        })
    
    @event("VMProvisioned")
    def record_vm(self, role: str, vm_id: str, ip: str, port: int,
                  ssh_port: int) -> None:
        self.vm_registry[role] = {
            "vm_id": vm_id, "ip": ip, "port": port,
            "ssh_port": ssh_port, "state": "provisioning",
        }
    
    @event("VMStateChanged")
    def update_vm(self, role: str, state: str) -> None:
        if role in self.vm_registry:
            self.vm_registry[role]["state"] = state
    
    @event("VMDestroyed")
    def destroy_vm(self, role: str) -> None:
        self.vm_registry.pop(role, None)
    
    @event("Failed")
    def mark_failed(self, reason: str) -> None:
        self.status = "failed"
        self.failure_reason = reason
    
    @event("Completed")
    def mark_completed(self, output_path: str) -> None:
        self.status = "completed"
        self.output_path = output_path
    
    def _next_stage(self, stage: str) -> str:
        order = ["scenario", "audio", "video", "assembly"]
        try:
            idx = order.index(stage)
            return order[idx + 1] if idx + 1 < len(order) else "done"
        except ValueError:
            return "done"
```

### 9.3 The Application: `DocumentaryApp`

```python
from eventsourcing.application import Application
from eventsourcing.sqlite import SQLiteFactory

class DocumentaryApp(Application):
    """Event-sourced application for the documentary pipeline."""
    
    def start_pipeline(self, brief: str, model_id: str, run_id: str) -> UUID:
        pipeline = DocumentaryPipeline(brief, model_id, run_id)
        self.save(pipeline)
        return pipeline.id
    
    def complete_stage(self, pipeline_id: UUID, stage: str, 
                       checkpoint_path: str) -> None:
        pipeline = self.repository.get(pipeline_id)
        pipeline.complete_stage(stage, checkpoint_path)
        self.save(pipeline)
    
    def record_tool(self, pipeline_id: UUID, agent: str, tool: str,
                    args: dict, result: dict, duration_ms: float) -> None:
        pipeline = self.repository.get(pipeline_id)
        pipeline.record_tool_call(agent, tool, args, result, duration_ms)
        self.save(pipeline)
    
    def provision_vm(self, pipeline_id: UUID, role: str, vm_id: str,
                     ip: str, port: int, ssh_port: int) -> None:
        pipeline = self.repository.get(pipeline_id)
        pipeline.record_vm(role, vm_id, ip, port, ssh_port)
        self.save(pipeline)
    
    def get_pipeline(self, pipeline_id: UUID) -> DocumentaryPipeline:
        return self.repository.get(pipeline_id)
    
    def get_latest_snapshot(self, pipeline_id: UUID) -> dict:
        """Get the latest snapshot to avoid replaying all events."""
        # pyeventsourcing handles this automatically via snapshotting
        pipeline = self.repository.get(pipeline_id)
        return {
            "completed_stages": pipeline.completed_stages,
            "current_stage": pipeline.current_stage,
            "vm_registry": pipeline.vm_registry,
            "checkpoints": pipeline.checkpoints,
            "status": pipeline.status,
        }
```

### 9.4 Snapshot Strategy

pyeventsourcing supports **automatic snapshotting**:

```python
# In application config
os.environ["PERSISTENCE_MODULE"] = "eventsourcing.sqlite"
os.environ["SQLITE_DBNAME"] = os.path.expanduser("~/Documents/documentary-pipeline/snapshots.db")
os.environ["SNAPSHOT_INTERVAL"] = "10"  # Snapshot every 10 events
```

This means:
- Every 10 events, a snapshot is saved
- Loading an aggregate: if snapshot exists, load snapshot + replay only events after snapshot
- Without snapshotting: replay ALL events (slow for long runs)

### 9.5 Wiring into the Pipeline

**Step 1: Replace `RecoveryShell` with `DocumentaryApp`**

```python
async def run_documentary(brief, ...):
    app = DocumentaryApp()
    run_id = f"run_{int(time.time())}"
    
    # Check for existing run to resume
    existing = find_latest_run(app, brief)
    if existing and existing.status == "running":
        pipeline_id = existing.id
        # Resume from last checkpoint
        snapshot = app.get_latest_snapshot(pipeline_id)
        completed_stages = snapshot["completed_stages"]
        latest_checkpoint = snapshot["checkpoints"].get(snapshot["current_stage"], "")
    else:
        pipeline_id = app.start_pipeline(brief, model_id, run_id)
        completed_stages = []
        latest_checkpoint = ""
    
    # Build graph with resume state
    graph, shell = build_documentary_graph(
        hooks=hooks,
        model=model,
        resume=True,
        completed_stages=completed_stages,
        latest_checkpoint=latest_checkpoint,
    )
    
    # Run
    try:
        result = await shell.run(brief)
        app.mark_completed(pipeline_id, result.get("output_path", ""))
    except Exception as exc:
        app.mark_failed(pipeline_id, str(exc))
        raise
```

**Step 2: Hook `SnapshotHook` into tool calls**

```python
class SnapshotHook(HookProvider):
    def __init__(self, app: DocumentaryApp, pipeline_id: UUID):
        self.app = app
        self.pipeline_id = pipeline_id
    
    def register_hooks(self, registry):
        registry.add_callback(BeforeToolCallEvent, self.on_tool_call)
        registry.add_callback(AfterNodeCallEvent, self.on_node_complete)
    
    def on_tool_call(self, event):
        # Tool calls are recorded as events
        pass  # AfterToolCallEvent needed for result
    
    def on_node_complete(self, event):
        node_id = event.node_id
        if node_id in ["scenario", "audio", "video", "assembly"]:
            checkpoint_path = save_checkpoint(node_id)
            self.app.complete_stage(self.pipeline_id, node_id, checkpoint_path)
```

**Step 3: Wire VM events**

```python
# In bash_command tool wrapper
def bash_command(command: str) -> str:
    result = _run_vast_cli(command)
    # Parse result for VM operations
    if "create instance" in command and "new_contract" in result:
        vm_id = parse_vm_id(result)
        app.provision_vm(pipeline_id, "video", vm_id, ip, port, ssh_port)
    return result
```

### 9.6 Resume from Exact Moment

With event sourcing + snapshotting, resuming works like this:

1. **Load aggregate**: `app.repository.get(pipeline_id)`
   - pyeventsourcing finds the latest snapshot
   - Replays events since the snapshot
   - Aggregate is restored to exact state at last event

2. **Restore OTIO**: Copy checkpoint OTIO to working path
   ```python
   checkpoint = aggregate.checkpoints.get(aggregate.current_stage, "")
   if checkpoint:
       shutil.copy2(checkpoint, timeline_path)
   ```

3. **Restore VMs**: Re-query Vast.ai for active VMs
   ```python
   for role, vm in aggregate.vm_registry.items():
       if vm["state"] in ("running", "healthy"):
           # Verify VM is still alive via vastai show instance
           # Re-register worker URL
   ```

4. **Continue graph**: The graph's skip logic sees `completed_stages` and skips finished stages
   - Edge conditions check `RecoveryShell.completed_stages` (now populated from aggregate)
   - OTIO disk state confirms clips exist
   - Graph proceeds to the next uncompleted stage

### 9.7 Granularity Levels

| Level | Event Type | Use Case |
|---|---|---|
| **Stage** | `StageCompleted` | Resume from stage boundary (coarse) |
| **Tool** | `ToolCalled` + `ToolReturned` | Resume from specific tool (medium) |
| **LLM turn** | `LLMResponse` | Resume from specific LLM interaction (fine) |
| **OTIO mutation** | `OtioMutated` | Resume from specific file change (very fine) |

With pyeventsourcing, all levels are just events in the same stream. Snapshots can be taken at any interval. The finer the events, the more precise the resume point.

### 9.8 Trade-offs

| Aspect | Stage-level | Tool-level | LLM-turn-level |
|---|---|---|---|
| **Storage** | Low (~100 events/run) | Medium (~1000 events/run) | High (~5000 events/run) |
| **Replay time** | Fast (<1s) | Medium (~5s) | Slow (~30s) |
| **Precision** | Stage boundary only | Specific tool | Exact LLM context |
| **Snapshot frequency** | Every stage | Every 10 tools | Every 50 turns |

**Recommendation**: Start with **stage-level** (already half-implemented via checkpoints) and add **tool-level** for VM provisioning/resilience. LLM-turn-level is overkill unless you need deterministic replay for debugging.

---

## 10. Files to Modify

1. **`tracing/snapshot_store.py`** — Already exists. Wire it into `run_strands.py`.
2. **`tracing/snapshot_hooks.py`** — Already exists. Register in `build_documentary_graph()`.
3. **`strands_agents/graph_pipeline.py`** — Pass `resume` flag, read `metadata.json`, populate `RecoveryShell`.
4. **`strands_agents/run_strands.py`** — Initialize `DocumentaryApp`, check for existing runs, pass resume state.
5. **`strands_agents/hooks/pipeline_hooks.py`** — Add `SnapshotHook` registration.
6. **New: `tracing/pipeline_aggregate.py`** — `DocumentaryPipeline` aggregate + `DocumentaryApp`.
7. **`tools/vastai_tools.py`** — Emit VM lifecycle events to aggregate.

---

## 11. Minimal Viable Implementation (2-3 hours)

Instead of full pyeventsourcing integration, the **fastest path** to resumability:

1. **Wire existing `SnapshotHook`** to `build_documentary_graph(hooks=[..., SnapshotHook()])`
2. **Read `metadata.json`** on startup to populate `completed_stages`
3. **Set `RecoveryShell(resume=True, ...)`** when `metadata.json` has completed stages
4. **Seed timeline** from last checkpoint OTIO
5. **Re-query Vast.ai** for active VMs on resume

This gives stage-level resume with the existing infrastructure — no new dependencies needed.
