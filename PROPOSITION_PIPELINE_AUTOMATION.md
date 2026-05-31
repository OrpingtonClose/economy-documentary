> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Proposition: Operator-Authored Content Agents + Causal Observability

## Problem

The architecture requires a human operator to POST to agents, monitor GSA state, and decide which agent to wake next. For unattended execution, archival reproduction, and regression validation, the operator needs:

1. A way to author exactly what an agent should say
2. A way to automate the decision of which agent to wake next
3. A way to record every causal step for later inspection

## Solution

### 1. Content Agent (Production Component)

A first-class agent type that reads authored natural language from a file and returns it via HTTP POST.

```python
# server/agents/content_agent.py
from fastapi import FastAPI
from pathlib import Path
import yaml

class ContentAgent:
    """Agent whose responses are authored by the operator.
    
    The operator writes natural language text in YAML files.
    The agent serves them via HTTP POST.
    The parser extracts effects from this text.
    This is a first-class agent type, not a test fixture.
    """
    
    def __init__(self, content_path: Path):
        self.content = yaml.safe_load(content_path.read_text())
        self.turn = 0
        self.app = FastAPI()
        self.app.get("/")(self._get)
        self.app.post("/")(self._post)
    
    def _get(self):
        return {"status": "ok", "agent": self.content.get("agent", "content")}
    
    def _post(self, payload: dict):
        turns = self.content.get("turns", [])
        if self.turn < len(turns):
            text = turns[self.turn].get("text", "")
        else:
            text = "Nothing more to do."
        self.turn += 1
        return {"text": text}
```

### 2. Pipeline Automation (Production Component)

Automates the human operator's role. The architecture states the operator IS the launcher. This component automates that role.

```python
# server/orchestrator/automation.py
class PipelineAutomation:
    """Automates the human operator's launch and wake decisions.
    
    The operator queries GSA via GET / and decides which agent
    to wake via POST /. This component automates that loop.
    """
    
    def __init__(self, rules_path: Path, agents: dict[str, str]):
        self.rules = yaml.safe_load(rules_path.read_text())
        self.agents = agents
    
    def run(self, run_id: str):
        while True:
            state = self._query_gsa(run_id)
            for rule in self.rules["rules"]:
                if self._match(rule["condition"], state):
                    if rule["action"] == "terminate":
                        return
                    target, text = self._parse(rule["action"])
                    self._post(target, run_id, text)
                    break
            time.sleep(1)
```

### 3. Causal Observability (Production Infrastructure)

Records every decision in the pipeline. Production observability, not test instrumentation.

```python
# server/observability/causal_log.py
class CausalLog:
    """Records the complete causal graph of a pipeline run.
    
    Every agent POST, parser invocation, handler decision, event append,
    GSA projection update, and bash execution is recorded.
    """
    
    def __init__(self, run_id: str, output_dir: str = "/var/log/pipeline"):
        self.run_id = run_id
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.file = open(self.output_dir / f"{run_id}.jsonl", "w")
        self.sequence = 0
    
    def emit(self, event_type: str, data: dict):
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
```

### 4. Observability Queries (Production Tooling)

Tools that read causal logs and produce human-readable analysis.

```python
# server/observability/causal_query.py
"""Query tools for causal logs. Production tooling for pipeline analysis."""

import json
from pathlib import Path

def timeline(run_id: str, log_dir: str = "/var/log/pipeline") -> str:
    """Generate human-readable timeline from causal log."""
    path = Path(log_dir) / f"{run_id}.jsonl"
    lines = path.read_text().strip().split("\n")
    parts = []
    for line in lines:
        e = json.loads(line)
        ts = e.get("ts", "?")
        typ = e.get("type", "?")
        if typ == "agent_output":
            parts.append(f"[{ts}] {e.get('agent', '?')} → emitted {e.get('text_length', 0)} chars")
        elif typ == "parser_result":
            parts.append(f"[{ts}] parser  → extracted {e.get('effects_count', 0)} effects, confidence={e.get('confidence', 0)}")
        elif typ == "event_appended":
            parts.append(f"[{ts}] handler → appended {e.get('effect_kind', '?')} @ seq={e.get('sequence', '?')}")
        elif typ == "gsa_update":
            parts.append(f"[{ts}] GSA     ← {e.get('event_kind', '?')} → updated {', '.join(e.get('projections_updated', []))}")
        elif typ == "http_wake":
            parts.append(f"[{ts}] {e.get('from', '?')} → POST {e.get('to', '?')}")
        elif typ == "bash_execute":
            parts.append(f"[{ts}] bash    → {e.get('command', '?')} (exit {e.get('exit_code', '?')})")
        else:
            parts.append(f"[{ts}] {typ}")
    return "\n".join(parts)


def divergence(run_id_a: str, run_id_b: str, log_dir: str = "/var/log/pipeline") -> str:
    """Compare two pipeline runs and report differences."""
    def load(run_id):
        path = Path(log_dir) / f"{run_id}.jsonl"
        return [json.loads(line) for line in path.read_text().strip().split("\n")]
    
    a = load(run_id_a)
    b = load(run_id_b)
    parts = [f"Comparing {run_id_a} ({len(a)} events) vs {run_id_b} ({len(b)} events)"]
    
    for i in range(min(len(a), len(b))):
        if a[i].get("type") != b[i].get("type"):
            parts.append(f"Divergence at seq={i}: {a[i]['type']} vs {b[i]['type']}")
            break
        if a[i].get("type") == "parser_result":
            a_conf = a[i].get("confidence", 0)
            b_conf = b[i].get("confidence", 0)
            if a_conf != b_conf:
                parts.append(f"Divergence at seq={i}: parser confidence {a_conf} vs {b_conf}")
                break
        if a[i].get("type") == "event_appended":
            if a[i].get("effect_kind") != b[i].get("effect_kind"):
                parts.append(f"Divergence at seq={i}: effect {a[i]['effect_kind']} vs {b[i]['effect_kind']}")
                break
    else:
        if len(a) != len(b):
            parts.append(f"Same events, different lengths: {len(a)} vs {len(b)}")
        else:
            parts.append("No divergence detected.")
    
    return "\n".join(parts)


def replay_projection(run_id: str, sequence: int, log_dir: str = "/var/log/pipeline") -> dict:
    """Reconstruct projection state at a specific sequence number."""
    path = Path(log_dir) / f"{run_id}.jsonl"
    lines = path.read_text().strip().split("\n")
    
    projections = {}
    for line in lines:
        e = json.loads(line)
        if e.get("type") == "gsa_update" and e.get("seq", 0) <= sequence:
            snapshots = e.get("projection_snapshots", {})
            projections.update(snapshots)
    
    return projections


def inspect_parser(run_id: str, agent: str, turn: int, log_dir: str = "/var/log/pipeline") -> dict:
    """Deep dive into a specific parser invocation."""
    path = Path(log_dir) / f"{run_id}.jsonl"
    lines = path.read_text().strip().split("\n")
    
    for line in lines:
        e = json.loads(line)
        if e.get("type") == "parser_result" and e.get("agent") == agent:
            turn -= 1
            if turn == 0:
                return {
                    "input_text_length": e.get("input_text_length"),
                    "system_prompt_hash": e.get("system_prompt_hash"),
                    "chain_of_thought": e.get("chain_of_thought"),
                    "effects": e.get("effects_extracted"),
                    "confidence": e.get("confidence"),
                    "model": e.get("model"),
                    "temperature": e.get("temperature"),
                    "latency_ms": e.get("latency_ms"),
                    "reask_count": e.get("reask_count", 0),
                }
    return {}
```

## Configuration

### Content Agent

```yaml
# server/content/agents/scenario.yaml
agent: scenario
port: 9001
turns:
  - text: |
      Starting pipeline for run {run_id}. Budget $10.00.
      Max attempts per block: 5. Output path: /tmp/final.mp4.
  - text: |
      Scene 1, V1 narration: "In 1924, the world stood at a crossroads."
      Duration: 30 seconds.
```

### Automation Rules

```yaml
# server/content/orchestrator/default.yaml
agents:
  scenario: http://localhost:8001
  audio: http://localhost:8002
  video: http://localhost:8003
  assembly: http://localhost:8005
  provisioner: http://localhost:8081
rules:
  - condition: "no PipelineStarted"
    action: "post scenario 'Start pipeline for topic 1920s economy'"
  - condition: "OTIO has unfilled A1 slots"
    action: "post scenario 'Continue script'"
  - condition: "OTIO has dirty audio blocks"
    action: "post audio 'Wake for reconciliation'"
  - condition: "All audio clean and V1 slots unfilled"
    action: "post video 'Wake for video production'"
  - condition: "All slots filled and no PipelineComplete"
    action: "post assembly 'Wake for final assembly'"
  - condition: "PipelineComplete exists"
    action: "terminate"
```

## Causal Log Events

| Event Type | Data | Purpose |
|---|---|---|
| `agent_output` | text, text_length, agent_name | What the agent said |
| `parser_result` | input_text_length, system_prompt_hash, chain_of_thought, effects, confidence, model, temperature, latency_ms | What the parser understood |
| `parser_retry` | attempt, validation_error, corrected_output | Why instructor reasked |
| `event_appended` | stream, effect_kind, sequence | What entered the event store |
| `event_rejected` | effect_kind, reason | Why the handler blocked it |
| `gsa_update` | event_kind, projections_updated, snapshots | How projections changed |
| `http_wake` | source, target, payload | Which agent woke which |
| `bash_execute` | command, stdout_preview, stderr_preview, exit_code | What the Provisioner ran |

## Observability Queries

| Query | Input | Output |
|---|---|---|
| `timeline(run_id)` | Causal log JSONL | Human-readable event sequence |
| `divergence(run_a, run_b)` | Two causal logs | Where two runs differ |
| `replay_projection(run_id, seq)` | Causal log + sequence number | Projection state at that point |
| `inspect_parser(run_id, agent, turn)` | Causal log + agent + turn | Full parser context |

## Running

```bash
# Unattended pipeline with content agents
python -m orchestrator.automation \
    --rules content/orchestrator/default.yaml \
    --run-id run-$(date +%s)

# Analyze a completed run
python -m observability.causal_query timeline --run-id run-123
python -m observability.causal_query divergence --run-a run-123 --run-b run-124
python -m observability.causal_query inspect_parser --run-id run-123 --agent audio --turn 3
```

## Production Code Changes

**Zero files modified.** All additions in new directories:

| New File | Lines | Purpose |
|---|---|---|
| `server/agents/content_agent.py` | ~150 | FastAPI service, reads authored content |
| `server/orchestrator/automation.py` | ~200 | Reads GSA, matches rules, wakes agents |
| `server/observability/causal_log.py` | ~100 | JSONL event capture |
| `server/observability/causal_query.py` | ~200 | Timeline, divergence, replay, parser inspector |
| `server/content/agents/*.yaml` | ~50 each | Authorable content files |
| `server/content/orchestrator/*.yaml` | ~50 each | Automation rules |
| `server/agent_base.py` (hooks) | ~30 | Startup instrumentation |
| **Total** | **~1,000** | |
