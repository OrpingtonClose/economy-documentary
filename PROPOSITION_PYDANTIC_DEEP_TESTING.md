> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Proposition: pydantic-deep Components for Deterministic Agents + Causal Logging

## Insight

pydantic-deep already has the primitives we need. Agents are composed of **Components** (ToolsCapability, MemoryCapability, ContextManagerCapability, etc.) with lifecycle hooks (`on_before_run`, `on_after_run`, `on_before_tool_call`, `on_after_tool_call`). We don't build custom HTTP services for deterministic agents. We build a **pydantic-deep Component** that replaces the LLM with authored content.

The Causal Log is also a **pydantic-deep Component** that hooks into every agent's lifecycle. It attaches to both LLM agents and deterministic agents transparently.

---

## Architecture

```
pydantic-deep Agent
├── Components (pluggable)
│   ├── LLMCapability              # Existing: calls OpenRouter/DeepSeek
│   │   └── replaced by:
│   ├── ContentCapability          # NEW: reads authored YAML, returns text
│   │
│   ├── ToolsCapability            # Existing: tool registry + execution
│   ├── MemoryCapability           # Existing: message history management
│   ├── ContextManagerCapability   # Existing: token compaction
│   ├── TodoCapability             # Existing: task tracking (the "todo lib")
│   ├── FilesystemCapability       # Existing: workspace file access
│   │
│   └── CausalLogCapability        # NEW: records every lifecycle event
│       ├── on_before_run          # logs: agent name, input payload, GSA state
│       ├── on_after_run           # logs: agent text output, turn duration
│       ├── on_before_tool_call    # logs: tool name, args
│       ├── on_after_tool_call     # logs: tool result, duration
│       └── on_before_compress     # logs: compaction decisions
│
└── Agent.run(payload) → text
    ├── CausalLogCapability.on_before_run
    ├── ContentCapability.generate_text (or LLMCapability.generate_text)
    │   └── text (natural language)
    ├── ToolsCapability.execute_tools (if text requests tools)
    │   ├── CausalLogCapability.on_before_tool_call
    │   ├── tool execution (real bash, real docker, real file ops)
    │   └── CausalLogCapability.on_after_tool_call
    ├── MemoryCapability.update_history
    ├── ContextManagerCapability.compact_if_needed
    │   └── CausalLogCapability.on_before_compress
    └── CausalLogCapability.on_after_run
```

---

## 1. ContentCapability (Replaces LLM)

A pydantic-deep Component that reads authored natural language from a YAML file instead of calling an LLM API.

```python
# server/components/content_capability.py
from pydantic_deep import Component
from pathlib import Path
import yaml

class ContentCapability(Component):
    """Generates agent responses from authored YAML content.
    
    Replaces LLMCapability in deterministic agents. The operator writes
    natural language text in YAML. This component serves that text via
    the standard pydantic-deep Agent interface.
    
    Compatible with all other Components: ToolsCapability, MemoryCapability,
    TodoCapability, ContextManagerCapability, CausalLogCapability.
    """
    
    name: str = "content"
    
    def __init__(self, content_path: Path):
        self.content = yaml.safe_load(content_path.read_text())
        self.turn_counter = 0
    
    def generate_text(self, context: AgentContext) -> str:
        """Return the next authored text for this turn."""
        turns = self.content.get("turns", [])
        if self.turn_counter < len(turns):
            text = turns[self.turn_counter].get("text", "")
        else:
            text = "Nothing more to do."
        self.turn_counter += 1
        return text
    
    def on_agent_wake(self, payload: dict) -> None:
        """Reset turn counter on new wake (if configured)."""
        if self.content.get("reset_on_wake", True):
            self.turn_counter = 0
```

### How It's Used

```python
# Build a deterministic scenario agent
from pydantic_deep import Agent, create_deep_agent
from server.components.content_capability import ContentCapability
from server.components.causal_log_capability import CausalLogCapability

scenario_agent = create_deep_agent(
    model=None,  # No LLM
    components=[
        ContentCapability(content_path=Path("content/agents/scenario.yaml")),
        ToolsCapability(tools=[docker_cli]),
        MemoryCapability(max_history=10),
        TodoCapability(),  # The "todo lib" — tracks tasks per run
        CausalLogCapability(run_id="test-001", output_dir="/var/log/pipeline"),
    ],
    system_prompt="You are a scenario agent.",  # Still used for tool context
)

# The agent is a real pydantic-deep agent
# It uses ToolsCapability if its text requests tool calls
# It uses MemoryCapability to track conversation history
# It uses TodoCapability to manage tasks
# It uses CausalLogCapability to record everything
```

---

## 2. CausalLogCapability (Observability Component)

A pydantic-deep Component that hooks into every lifecycle event. Attaches to any agent (LLM or deterministic) transparently.

```python
# server/components/causal_log_capability.py
from pydantic_deep import Component
from datetime import datetime
import json
from pathlib import Path

class CausalLogCapability(Component):
    """Records the complete causal graph of agent execution.
    
    Hooks into pydantic-deep's lifecycle events:
    - on_before_run: agent receives wake, reads GSA
    - on_after_run: agent produces text, memory updated
    - on_before_tool_call: tool execution starts
    - on_after_tool_call: tool execution completes
    - on_before_compress: context compaction starts
    
    This is a production Component, not test instrumentation.
    It runs in every environment: dev, staging, production.
    """
    
    name: str = "causal_log"
    
    def __init__(self, run_id: str, output_dir: str = "/var/log/pipeline"):
        self.run_id = run_id
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.file = open(self.output_dir / f"{run_id}.jsonl", "a")
        self.sequence = 0
    
    def _emit(self, event_type: str, data: dict):
        entry = {
            "ts": datetime.utcnow().isoformat(),
            "seq": self.sequence,
            "type": event_type,
            "run_id": self.run_id,
            **data,
        }
        self.file.write(json.dumps(entry, default=str) + "\n")
        self.file.flush()
        self.sequence += 1
    
    def on_before_run(self, context: AgentContext) -> None:
        """Agent is about to process a wake request."""
        self._emit("agent_wake", {
            "agent": context.agent_name,
            "payload": context.payload,
            "gsa_state_summary": context.gsa_state.summary() if context.gsa_state else None,
            "memory_turns": len(context.memory),
            "todo_count": len(context.todos) if hasattr(context, "todos") else 0,
        })
    
    def on_after_run(self, context: AgentContext, output_text: str) -> None:
        """Agent has produced text output."""
        self._emit("agent_output", {
            "agent": context.agent_name,
            "text_length": len(output_text),
            "text_preview": output_text[:200],
            "memory_turns_after": len(context.memory),
        })
    
    def on_before_tool_call(self, context: AgentContext, tool_name: str, tool_args: dict) -> None:
        """Tool execution is about to start."""
        self._emit("tool_call_start", {
            "agent": context.agent_name,
            "tool": tool_name,
            "args": tool_args,
        })
    
    def on_after_tool_call(self, context: AgentContext, tool_name: str, result: str, duration_ms: int) -> None:
        """Tool execution has completed."""
        self._emit("tool_call_end", {
            "agent": context.agent_name,
            "tool": tool_name,
            "result_length": len(result),
            "result_preview": result[:200],
            "duration_ms": duration_ms,
        })
    
    def on_before_compress(self, context: AgentContext, original_tokens: int, target_tokens: int) -> None:
        """Context compaction is about to run."""
        self._emit("compress_start", {
            "agent": context.agent_name,
            "original_tokens": original_tokens,
            "target_tokens": target_tokens,
        })
    
    def on_parser_invoke(self, agent_name: str, input_text: str, system_prompt: str, 
                         chain_of_thought: str, effects: list[dict], 
                         confidence: int, model: str, temperature: float, 
                         latency_ms: int, reask_count: int) -> None:
        """Called by the parser after extraction (parser passes this in)."""
        self._emit("parser_result", {
            "agent": agent_name,
            "input_text_length": len(input_text),
            "input_text_preview": input_text[:200],
            "system_prompt_hash": hashlib.sha256(system_prompt.encode()).hexdigest()[:8],
            "chain_of_thought": chain_of_thought,
            "effects": effects,
            "confidence": confidence,
            "model": model,
            "temperature": temperature,
            "latency_ms": latency_ms,
            "reask_count": reask_count,
        })
    
    def on_handler_append(self, agent_name: str, effect: dict, esdb_sequence: int) -> None:
        """Called by the handler after appending to EventStoreDB."""
        self._emit("event_appended", {
            "agent": agent_name,
            "effect_kind": effect.get("kind", "unknown"),
            "esdb_sequence": esdb_sequence,
        })
    
    def on_handler_reject(self, agent_name: str, effect: dict, reason: str) -> None:
        """Called by the handler when rejecting an effect."""
        self._emit("event_rejected", {
            "agent": agent_name,
            "effect_kind": effect.get("kind", "unknown"),
            "reason": reason,
        })
    
    def on_gsa_update(self, event_kind: str, projections_updated: list[str], projection_snapshots: dict) -> None:
        """Called by GSA when applying an event to projections."""
        self._emit("gsa_update", {
            "event_kind": event_kind,
            "projections_updated": projections_updated,
            "projection_snapshots": projection_snapshots,
        })
```

---

## 3. TodoCapability Integration

The existing pydantic-deep TodoCapability ("todo lib") tracks tasks. Deterministic agents use it to manage their authored content:

```python
# In ContentCapability.generate_text:

def generate_text(self, context: AgentContext) -> str:
    # Check todo list for pending tasks
    todos = context.components.get("todo", []).todos
    pending = [t for t in todos if t.status == "pending"]
    
    if pending:
        # Return text for the next pending task
        task = pending[0]
        text = self._text_for_task(task)
        task.status = "in_progress"
    else:
        # All tasks done
        text = "All tasks complete. Nothing more to do."
    
    return text
```

The TodoCapability gives deterministic agents a lightweight state machine without violating the "no state machine in code" principle — the state is emergent from the todo list, which is just another Component.

---

## 4. Pipeline Automation as pydantic-deep Agent

The automation driver is also a pydantic-deep agent — it just uses a special "orchestrator" system prompt and reads GSA state as its "input":

```python
# server/agents/orchestrator_agent.py
from pydantic_deep import create_deep_agent
from server.components.content_capability import ContentCapability
from server.components.causal_log_capability import CausalLogCapability

orchestrator = create_deep_agent(
    model=None,  # No LLM — deterministic rules
    components=[
        ContentCapability(content_path=Path("content/orchestrator/default.yaml")),
        CausalLogCapability(run_id="auto-001"),
    ],
    system_prompt="""You are the pipeline orchestrator.
    
    Read GSA state. Decide which agent to wake next.
    Output natural language describing your decision.
    The parser extracts the wake command as an effect.
    """,
)

# The orchestrator "runs" by:
# 1. Querying GSA via GET /
# 2. Matching authored rules against GSA state
# 3. Emitting text like "Wake audio agent for run test-001"
# 4. Parser extracts HTTP wake effect
# 5. Handler POSTs to the target agent
```

Wait — this is wrong. The orchestrator should not emit text that gets parsed. It should directly POST to agents. Let me correct:

```python
# The orchestrator is NOT a pydantic-deep agent
# It is a simple loop that reads GSA and POSTs to agents
# It uses the CausalLogCapability for observability

class PipelineAutomation:
    """Automates operator decisions. Not an agent — no LLM, no parser."""
    
    def __init__(self, rules_path: Path, causal_log: CausalLogCapability):
        self.rules = yaml.safe_load(rules_path.read_text())
        self.causal_log = causal_log
    
    def run(self, run_id: str):
        while True:
            state = self._query_gsa(run_id)
            
            for rule in self.rules["rules"]:
                if self._match(rule["condition"], state):
                    if rule["action"] == "terminate":
                        self.causal_log._emit("automation_terminate", {"run_id": run_id})
                        return
                    
                    target, text = self._parse(rule["action"])
                    
                    self.causal_log._emit("http_wake", {
                        "from": "automation",
                        "to": target,
                        "payload": {"run_id": run_id, "notification_type": "wake"},
                    })
                    
                    requests.post(target, json={
                        "run_id": run_id,
                        "notification_type": "wake",
                        "context": {},
                    })
                    break
            
            time.sleep(1)
```

---

## 5. Test = Mini Production Run

A test is not a test function. It is a **mini production run** with deterministic agents.

```bash
# Start EventStoreDB (test container)
docker run --rm -p 2113:2113 eventstore/eventstore:latest --in-memory

# Start GSA
python -m server.agents.global_state_agent --port 8000

# Start deterministic agents (each is a real pydantic-deep agent)
python -m server.agents.scenario_agent \
    --component content:content/agents/scenario.yaml \
    --component causal_log:run=test-001 \
    --port 8001

python -m server.agents.audio_agent \
    --component content:content/agents/audio.yaml \
    --component causal_log:run=test-001 \
    --port 8002

python -m server.agents.provisioner_agent \
    --component content:content/agents/provisioner.yaml \
    --component causal_log:run=test-001 \
    --port 8081

# Start automation
python -m server.orchestrator.automation \
    --rules content/orchestrator/default.yaml \
    --causal-log run=test-001 \
    --run-id test-001

# Wait for completion
# Read causal log
# Assert on event sequence
```

### Assert on Causal Log

```python
# Not a pytest test. A script that reads the causal log.

import json
from pathlib import Path

def verify_run(run_id: str) -> bool:
    path = Path(f"/var/log/pipeline/{run_id}.jsonl")
    events = [json.loads(line) for line in path.read_text().strip().split("\n")]
    
    # Extract event kinds in order
    kinds = [e["effect_kind"] for e in events if e["type"] == "event_appended"]
    
    expected = [
        "pipeline_started", "budget_set",
        "update_script", "update_script",
        "queue_job", "vm_allocated",
        "job_started", "job_completed",
        "reconciliation_complete",
        "merge_into_otio",
        "pipeline_complete",
    ]
    
    if kinds != expected:
        print(f"EVENT MISMATCH:")
        print(f"  Expected: {expected}")
        print(f"  Actual:   {kinds}")
        return False
    
    # Verify parser health
    parser_events = [e for e in events if e["type"] == "parser_result"]
    low_conf = [e for e in parser_events if e["confidence"] < 8]
    if low_conf:
        print(f"LOW CONFIDENCE: {len(low_conf)} parser invocations below threshold")
        for e in low_conf:
            print(f"  agent={e['agent']} confidence={e['confidence']}")
        return False
    
    reasks = sum(e["reask_count"] for e in parser_events)
    if reasks > 0:
        print(f"REASKS DETECTED: {reasks} total reasks")
        return False
    
    print(f"RUN {run_id}: VALID")
    return True
```

---

## 6. What the Todo Capability Gives Us

| Without TodoCapability | With TodoCapability |
|---|---|
| Agent returns text in fixed sequence | Agent checks todo list, returns text for next pending task |
| Cannot handle branching | Can branch based on todo state (e.g., "if TTS failed, add retry task") |
| No task tracking | Full task lifecycle: pending → in_progress → completed → verified |
| State is just turn counter | State is emergent from todo list (fits architecture philosophy) |

Example todo-driven content:

```yaml
# content/agents/audio.yaml
agent: audio
turns:
  - task: "check dirty blocks"
    text: |
      Checking OTIO state. Dirty blocks: A1:1:1, A1:2:1.
      Need TTS for both. Adding tasks.
  - task: "queue tts for A1:1:1"
    text: |
      Queuing TTS job for block A1:1:1.
      Voice: V1. Text: "In 1924, the world stood at a crossroads."
  - task: "queue tts for A1:2:1"
    text: |
      Queuing TTS job for block A1:2:1.
      Voice: V1. Text: "Then came the crash."
  - task: "verify reconciliation"
    text: |
      All audio generated and measured.
      Reconciliation complete. All blocks within tolerance.
```

The ContentCapability reads the todo list, finds the next pending task, and returns the text for that task. If a handler appends an effect that creates a new task (e.g., `ReconciliationFailed` adds "retry block A1:1:1"), the next turn picks up that task.

---

## 7. File Structure

```
server/
├── agents/
│   ├── scenario_agent.py            # LLM agent (existing)
│   ├── audio_agent.py               # LLM agent (existing)
│   ├── provisioner_agent.py         # LLM agent (existing)
│   └── ...
│
├── components/                      # NEW: pydantic-deep Components
│   ├── content_capability.py        # Reads YAML, returns authored text
│   └── causal_log_capability.py     # Records lifecycle events
│
├── orchestrator/
│   └── automation.py                # NEW: Reads GSA, wakes agents
│
├── content/                         # NEW: Authorable content
│   └── agents/
│       ├── scenario.yaml
│       ├── audio.yaml
│       ├── video.yaml
│       ├── assembly.yaml
│       ├── provisioner.yaml
│   └── orchestrator/
│       └── default.yaml
│
└── verify.py                        # NEW: Reads causal log, asserts
```

---

## 8. No Production Code Changes

**Zero files modified.** All additions in new directories. The only touch point is that `create_deep_agent()` accepts `components=` parameter, which it already does.

---

## Summary

| Component | pydantic-deep Integration | Purpose |
|---|---|---|
| **ContentCapability** | Replaces LLMCapability | Deterministic text from authored YAML |
| **CausalLogCapability** | Hooks into all lifecycle events | Records every decision to JSONL |
| **TodoCapability** | Already exists in pydantic-deep | Task tracking for deterministic agents |
| **ToolsCapability** | Already exists | Real bash, real docker, real file ops |
| **MemoryCapability** | Already exists | Conversation history |
| **ContextManagerCapability** | Already exists | Token compaction |

**A test is a mini production run.** Start EventStoreDB. Start GSA. Start deterministic pydantic-deep agents (with ContentCapability + CausalLogCapability). Run automation. Read causal log. Assert on event sequence, parser confidence, and reask count.

**Slow is correct.** Real parser. Real handler. Real event store. Real projections. Real bash (for Vast.ai tests). No mocks. No stubs. No test doubles.
