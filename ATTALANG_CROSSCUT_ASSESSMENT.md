> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# AttaLang × Prisoner Architecture — Cross-Cutting Simplification Assessment

> AttaLang = Docker orchestration agent (3 implementations: V1 LangGraph/HITL, V2 Pydantic, V3 programmatic). Prisoner = economy-documentary-work event-sourced pipeline (agents communicate via HTTP + EventStoreDB, parser extracts effects from natural language).

---

## 1. The Three-Version Problem — Unify Via Effect Modes

**Current state:** AttaLang maintains three full implementations (~2,800 LOC across V1/V2/V3) that differ only in how the LLM invokes Docker operations:

| Version | Invocation Style | Agent Framework | Lines of Agent Code |
|---|---|---|---|
| V1 | One tool call at a time | LangGraph + deepagents | 315 + 117 |
| V2 | One tool call at a time | Pydantic AI + deepagents | 386 + 86 |
| V3 | Batch via Python script | LangGraph + deepagents | 352 + 64 |

**Cross-cutting simplification:** Treat the three versions as **parser strategies** feeding a single execution core, exactly as Prisoner's parser extracts effects from agent text.

```
User Input
    ├──→ Parser Strategy A (V1 mode) → extracts individual tool calls → Executor
    ├──→ Parser Strategy B (V2 mode) → extracts structured SDK calls → Executor
    └──→ Parser Strategy C (V3 mode) → extracts Python script → CodeExecutor
```

**Wins:**
- One agent class, one runtime, one tool layer
- Parser strategy is a config flag, not a forked codebase
- V3's programmatic mode becomes the default; V1/V2 become compatibility parsers for constrained models

---

## 2. Agent Wrapper Boilerplate — DeepAgents Should Provide It

**Current state:** All three AttaLang agents duplicate the same ~80 lines:
- `_extract_text` / `_extract_output` (message content extraction from dict/list/str)
- `_make_config` (thread_id + recursion_limit)
- `invoke` / `ainvoke` / `stream` (sync/async/iterator wrappers)
- Workspace directory setup
- Skills directory resolution

**Prisoner parallel:** Prisoner's agents are HTTP services; they don't have this problem because POST / is the only interface. But AttaLang's boilerplate could collapse to a base class provided by deepagents.

**Wins:**
- ~240 lines of duplication eliminated per agent variant
- Less surface area for bugs (V1 and V3 `_extract_text` already diverge in subtle ways)

---

## 3. Tool Layer — One Unified Surface, Not Two

**Current state:** V1 uses a single `docker_cli` whitelisted-subprocess tool (424 lines). V2 reimplements the entire Docker SDK as individual tools (1,478 lines). They share only `_truncate_data` and `_docker_client` logic, which is copy-pasted with minor drift.

**Cross-cutting simplification:** Define Docker operations as **effect types**, not tools. The executor applies effects to Docker.

```python
class ContainerRun(Effect):
    kind: Literal["container_run"] = "container_run"
    image: str
    name: str | None
    ports: dict[str, int] | None

class ContainerStop(Effect):
    kind: Literal["container_stop"] = "container_stop"
    container_id: str
    timeout: int = 10
```

**Wins:**
- One schema serves CLI mode (V1), SDK mode (V2), and programmatic mode (V3)
- Effect validation happens once, not per-tool
- HITL approval applies at the effect level, not the tool level — framework-agnostic
- Trajectory/logging is free (effects are the audit trail)

---

## 4. HITL as Effects, Not Framework Interrupts

**Current state:** V1 uses LangGraph's `interrupt_on` mechanism with `Command(resume={"decisions": [...]})`. This ties safety to LangGraph's internal message format. V2 has no HITL at all. V3 blocks destructive ops via prompt instructions only.

**Prisoner approach:** Safety is an effect type, not a framework feature.

```python
class HumanInstruction(Effect):
    kind: Literal["human_instruction"] = "human_instruction"
    action: Literal["approve_command", "emergency_abort"]
    instruction: str
```

**Cross-cutting simplification:**
- Dangerous effects (ContainerRemove, VolumePrune) are never auto-executed
- Instead, the executor emits a `ClarificationRequest` effect
- A human appends a `HumanInstruction` effect to approve/reject
- The same mechanism works in V1, V2, and V3 without framework-specific interrupt code

**Wins:**
- V2 gets HITL "for free" (it currently lacks it entirely)
- V3's prompt-based blocking becomes enforceable (not just a suggestion in system prompt)
- Safety logic is ~40 lines instead of V1's 90-line interrupt handler

---

## 5. State Persistence — Event Sourcing vs In-Memory

**Current state:** All three versions use `MemorySaver()` — in-memory, ephemeral, no restart safety. A crash loses all context.

**Prisoner approach:** Every agent turn produces effects → EventStoreDB → projections rebuild state on restart.

**Cross-cutting simplification for AttaLang:** Even without full EventStoreDB, adopt the event log pattern:
- Every LLM turn appends to an append-only JSONL file
- On restart, replay the log to rebuild container state
- Trajectory collection becomes automatic (no separate `trajectory/collector.py`)

**Wins:**
- Delete `trajectory/collector.py` (47 lines) and `trajectory/models.py` — events ARE the trajectory
- Container state is reconstructible after restart
- No need for `MemorySaver()` checkpoints

---

## 6. Timeout Discipline

**Current state:** AttaLang has timeouts everywhere:
- `DOCKER_CLI_TIMEOUT_SECONDS = 30` (tool-level)
- `PROGRAMMATIC_TIMEOUT_SECONDS = 120` (code execution)
- `subprocess.run(..., timeout=timeout)`
- `signal.alarm()` and `threading.Thread.join(timeout=...)`

**Prisoner principle:** No timeouts in code. Operator monitors via GET / and intervenes manually. Timeouts cause silent failures and data loss.

**Cross-cutting simplification:** Remove all timeout parameters. Instead:
- Long-running operations stream progress via stdout
- Operator observes hang and kills the process externally
- No hidden truncation of Docker build logs or compose output

**Wins:**
- ~20 lines of timeout logic removed
- No more truncated docker build output because 30s expired
- Aligns with Prisoner's Principle 4

---

## 7. The `_extract_text` Pattern — Standardize or Eliminate

**Current state:** V1 has `_extract_text` (31 lines). V2 has `_extract_output` (17 lines). V3 has `_extract_text` again (28 lines, slightly different from V1). All do the same thing: pull a string out of a deeply nested LLM response structure.

**Cross-cutting simplification:** DeepAgents should return a string directly, or provide a single `extract_content()` utility. Alternatively, adopt Prisoner's model: the agent writes natural language → parser extracts effects → executor applies them. The LLM never returns nested structures; it returns text.

**Wins:**
- 3 extraction methods → 0 (if deepagents handles it) or 1 (shared utility)
- No more `getattr(last, "content", None)` gymnastics

---

## 8. Runtime Wrappers — Collapse to One

**Current state:** Three runtime wrappers with identical interfaces:
- `DockerGraphRuntime.run_turn()` (V1)
- `DockerRuntimeV2.run_turn()` / `arun_turn()` / `run_turn_verbose()` (V2)
- `ProgrammaticDockerRuntime.run_turn()` (V3)

All do: validate input → call agent → return string.

**Cross-cutting simplification:** One runtime. The parser strategy (V1/V2/V3) lives inside the agent, not in the runtime wrapper.

**Wins:**
- 3 runtime classes (~270 lines) → 1 class (~60 lines)
- CLI entry points all call the same runtime with a `--mode` flag

---

## 9. V3 Programmatic Mode — Make It Primary

**Current state:** V3 is treated as an alternative. But it's architecturally superior:
- One LLM call produces a batch script instead of N round-trip tool calls
- The LLM can use loops, conditionals, variables
- Token usage is lower (one tool description vs N individual calls)

**Prisoner parallel:** Prisoner's parser already extracts multiple effects from a single agent response. V3's `execute_docker_code` is essentially a batch effect.

**Cross-cutting simplification:** Make V3 the core. V1 and V2 become **output formatters** that constrain the LLM to emit individual calls instead of a script. The executor handles both:

```python
def execute(effects: list[Effect] | str):
    if isinstance(effects, str):
        # V3 mode: compiled Python script
        return code_executor.run(effects)
    for effect in effects:
        # V1/V2 mode: individual effects
        apply_effect(effect)
```

**Wins:**
- V3's `CodeExecutor` (187 lines) and `ProgrammaticToolBridge` become the canonical path
- V1/V2 shrink to parser front-ends (~100 lines each)
- Total codebase drops from ~2,800 to ~1,200 lines

---

## 10. What Prisoner Could Learn From AttaLang

**A. Programmatic batch execution**
Prisoner's agents emit one effect at a time per LLM turn. For VM provisioning (multi-step: allocate → start → verify), a programmatic mode where the Provisioner writes a Python script that calls VM operations would reduce round-trips from 3+ to 1.

**B. Direct tool access for operators**
AttaLang exposes `docker_cli` directly to the LLM. Prisoner could similarly expose a `bash_tool` effect that lets the Provisioner write shell scripts for Vast.ai CLI operations, rather than mapping every Vast.ai operation to a bespoke effect type.

**C. Sandbox execution**
AttaLang V3's `CodeExecutor` with restricted builtins is a reusable component. Prisoner could use it for safe execution of operator-provided scripts (e.g., custom post-processing on the final MP4).

---

## Summary: Simplification Ledger

| Component | Current (LOC) | Simplified (LOC) | Savings |
|---|---|---|---|
| Agent classes (×3) | 1,053 | 180 (1 base + parser variants) | 873 |
| Runtime wrappers (×3) | 267 | 60 | 207 |
| Tool layers (V1 + V2) | 1,902 | 400 (effect definitions + executor) | 1,502 |
| HITL/interrupt logic | 90 | 40 (effect-based) | 50 |
| Trajectory collection | 47 | 0 (events are trajectory) | 47 |
| Timeout handling | 25 | 0 | 25 |
| **Total** | **~3,384** | **~680** | **~2,704** |

**Core insight:** AttaLang's three versions are not three architectures — they are three parsers. Prisoner already solved this problem: one agent, one executor, multiple parser strategies. The savings are an 80% reduction in code surface.
