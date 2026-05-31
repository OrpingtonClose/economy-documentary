> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Migration Plan: Replace Strands Graph with pydantic-graph

## Goal
Replace Strands Graph orchestration with pydantic-graph to get **granular state snapshots before every node** and **exact-moment resume capability** via `iter_from_persistence()`.

## Why pydantic-graph v1 (BaseNode-based)

pydantic-graph has two APIs:
- **v1 (BaseNode)**: Deprecated but fully functional. Has `BaseStatePersistence`, `Graph.run(persistence=...)`, and `Graph.iter_from_persistence()` — exactly what we need for snapshots + resume.
- **v2 (GraphBuilder)**: New, has true parallel Fork/Join, but **no persistence layer yet**. Resume would require building custom persistence from scratch around `GraphRun.next()`.

We use **v1 for now** because native persistence is the user's stated priority. When v2 adds persistence, migration is mechanical (swap `@dataclass class Node(BaseNode)` for `@builder.step()`). Parallelism is handled **within** nodes via `asyncio.gather()`.

## Architecture

### State & Deps
```python
@dataclass
class PipelineState:
    timeline_path: str
    run_id: str
    brief: str
    completed_stages: list[str] = field(default_factory=list)
    vm_registry: dict[str, dict] = field(default_factory=dict)  # role → vm info
    recovery_target: str = ""
    recovery_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class PipelineDeps:
    model: Any  # DeepSeek model
    otio_manager: Any
    gatekeeper: Any
    hooks: list[HookProvider]
    checkpoint_dir: str
```

### Node Graph
```
Start → ScenarioNode → OtioGateNode → ProductionNode → OtioGateNode → AssemblyNode → End
                                          ↑_______________|
```

**Nodes:**
1. `ScenarioNode` — runs scenario agent, writes scene structure to OTIO
2. `OtioGateNode` — validates OTIO, decides next stage, handles backward retries
3. `ProductionNode` — **runs audio + video agents concurrently** via `asyncio.gather()`
4. `AssemblyNode` — runs assembly agent, produces final MP4

**Parallelism**: Audio and video agents run concurrently inside `ProductionNode.run()` using `asyncio.gather()`. This satisfies "media production must be parallel, one-vm per media" while keeping the graph simple and snapshot-friendly.

**Return types drive edges:**
```python
@dataclass
class ScenarioNode(BaseNode[PipelineState, PipelineDeps, None]):
    async def run(self, ctx: GraphRunContext) -> OtioGateNode:
        ...
        return OtioGateNode()

@dataclass
class OtioGateNode(BaseNode[PipelineState, PipelineDeps, None]):
    async def run(self, ctx: GraphRunContext) -> ScenarioNode | ProductionNode | AssemblyNode | End[None]:
        if needs_retry:
            return ScenarioNode()  # or AudioNode/VideoNode for fine-grained retry
        if audio_not_done or video_not_done:
            return ProductionNode()
        if assembly_not_done:
            return AssemblyNode()
        return End(None)

@dataclass
class ProductionNode(BaseNode[PipelineState, PipelineDeps, None]):
    async def run(self, ctx: GraphRunContext) -> OtioGateNode:
        audio_task = asyncio.create_task(_run_audio_agent(ctx))
        video_task = asyncio.create_task(_run_video_agent(ctx))
        await asyncio.gather(audio_task, video_task)
        ctx.state.completed_stages.extend(["audio", "video"])
        return OtioGateNode()

@dataclass
class AssemblyNode(BaseNode[PipelineState, PipelineDeps, None]):
    async def run(self, ctx: GraphRunContext) -> OtioGateNode | End[None]:
        ...
        return End(None)
```

### Custom Persistence: `SqliteStatePersistence`

Extends `BaseStatePersistence[PipelineState, None]`:

```python
@dataclass
class SqliteStatePersistence(BaseStatePersistence[PipelineState, None]):
    db_path: str
    run_id: str
    _history: list[Snapshot] = field(default_factory=list)
    _type_adapter: TypeAdapter | None = None

    async def snapshot_node(self, state, next_node):
        snap = NodeSnapshot(state=deepcopy(state), node=deepcopy(next_node))
        self._history.append(snap)
        await self._save_to_db(snap)

    async def load_next(self) -> NodeSnapshot | None:
        # Find latest 'created' snapshot for this run_id in SQLite
        # Set status to 'pending', return it

    async def load_all(self) -> list[Snapshot]:
        return self._history
```

**Schema:**
```sql
CREATE TABLE graph_snapshots (
    run_id TEXT,
    sequence_num INTEGER,
    snapshot_id TEXT PRIMARY KEY,
    kind TEXT,  -- 'node' | 'end'
    status TEXT,  -- 'created' | 'pending' | 'running' | 'success' | 'error'
    node_id TEXT,
    state_json TEXT,
    node_json TEXT,  -- serialized BaseNode
    start_ts TEXT,
    duration REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_run_status ON graph_snapshots(run_id, status);
```

**Serialization:**
- `state_json`: `PipelineState` serialized via Pydantic `TypeAdapter`
- `node_json`: `NodeSnapshot.node` serialized via pydantic-graph's `build_snapshot_list_type_adapter()` (handles `Annotated[BaseNode, CustomNodeSchema()]`)

### Resume Logic

```python
async def run_or_resume(brief, run_id=None):
    db_path = f"{PIPELINE_DIR}/snapshots.db"

    if run_id:
        # Resume existing run
        persistence = SqliteStatePersistence(db_path, run_id)
        persistence.set_types(PipelineState, type(None))
        async with graph.iter_from_persistence(persistence, deps=deps) as run:
            async for node in run:
                logger.info(f"Resumed: {node}")
    else:
        # Fresh run
        run_id = f"run_{int(time.time())}"
        persistence = SqliteStatePersistence(db_path, run_id)
        state = PipelineState(timeline_path=..., run_id=run_id, brief=brief)
        result = await graph.run(ScenarioNode(), state=state, deps=deps, persistence=persistence)
```

**What gets restored on resume:**
- `PipelineState` (timeline_path, completed_stages, vm_registry, etc.)
- Next node to execute (from snapshot)
- OTIO file is re-read from disk (source of truth)
- VMs are re-queried from Vast.ai (not stored in state)

### Idempotency Within Nodes

Each Strands agent's tools already check file existence before regeneration:
- `generate_scene_narration`: skips if WAV exists
- `submit_gpu_production_job`: skips if MP4 exists
- OTIO metadata prevents duplicate clip insertion

So resuming mid-stage is safe — agents will skip already-completed work.

## Files to Modify

1. **`server/strands_agents/graph_pipeline.py`** (major rewrite)
   - Remove Strands `Graph`, `GraphNode`, `GraphEdge`, `RecoveryShell`
   - Define `PipelineState`, `PipelineDeps`
   - Define `ScenarioNode`, `OtioGateNode`, `ProductionNode`, `AssemblyNode` as `BaseNode` subclasses
   - Build pydantic-graph v1 `Graph`
   - Implement `_run_audio_agent()` and `_run_video_agent()` as coroutines

2. **`server/strands_agents/run_strands.py`** (moderate rewrite)
   - Replace `build_documentary_graph(hooks=...)` call with pydantic-graph setup
   - Initialize `SqliteStatePersistence`
   - Add resume logic: check for existing snapshots, call `iter_from_persistence()`
   - Keep preflight, auto-tracer, cleanup

3. **New: `server/persistence/graph_persistence.py`**
   - `SqliteStatePersistence` class
   - SQLite schema setup
   - State/node serialization helpers

4. **`server/tracing/snapshot_store.py`** (optional enhancement)
   - Wire existing `SnapshotHook` to ALSO write to the same SQLite DB
   - LLM-turn-level events as a separate table (`tool_calls`, `llm_turns`)
   - This gives us TWO granularities: graph-level (pydantic-graph) + tool-level (SnapshotHook)

## Migration Steps

1. **Create `PipelineState` and `PipelineDeps`** dataclasses
2. **Implement `SqliteStatePersistence`** with schema + serialize/deserialize
3. **Write `ScenarioNode`** — wraps existing scenario agent, returns `OtioGateNode`
4. **Write `OtioGateNode`** — validates OTIO, uses edge conditions to route
5. **Write `ProductionNode`** — runs audio+video agents via `asyncio.gather()`
6. **Write `AssemblyNode`** — wraps assembly agent
7. **Build pydantic-graph** in `graph_pipeline.py`
8. **Wire into `run_strands.py`** with resume logic
9. **Test**: run brief → kill mid-production → resume → verify no duplicate work

## Trade-offs

| Aspect | Approach |
|---|---|
| **Graph API** | v1 (deprecated, has persistence) |
| **Parallelism** | Within-node `asyncio.gather()` (not graph-level Fork) |
| **Agents** | Keep Strands Agent (minimal change) |
| **Snapshot granularity** | Node-level (before every node run) |
| **Resume point** | Any node boundary |
| **Future migration** | When v2 adds persistence, swap `@dataclass class Node(BaseNode)` for `@builder.step()` |

## Risk: v1 Deprecation

The v1 API is deprecated but functional. If it is removed in pydantic-graph v2 before persistence is added to v2, we would need to:
1. Pin `pydantic-graph<2` in requirements, OR
2. Implement custom persistence on top of v2 GraphRun

Both are viable fallbacks. The native v1 persistence gives us exactly what the user needs today.
