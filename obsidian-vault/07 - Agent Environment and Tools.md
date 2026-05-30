---
{
  "title": "Agent Environment & Tools",
  "section": "7",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

<- [[06 - Projections|Projections]] | [[00 - Index|Index]] | [[08 - Agent Architecture pydantic-deep|Agent Architecture — pydantic-deep]] ->

# Agent Environment & Tools

> **V7.1 redesign:** Agents have exactly one tool: `bash_command`. All interaction
> with the world — querying the GSA, reading skills, running diagnostics — happens
> through bash. The handler is minimal: it builds the prompt (system instructions +
> skill catalog + memory), runs the agent, parses output, appends effects. The agent
> is autonomous. It curls the GSA. It reads skills. It reasons from raw state.

---

## 7.1 Design Principle: One Tool

```python
AGENT_TOOLS = [
    {
        "name": "bash_command",
        "description": "Execute a bash command. Use this to query the GSA (curl), read skill files, run diagnostics, or interact with any system. Output is captured and returned to you.",
        "parameters": {
            "command": {"type": "string", "description": "The bash command to execute"}
        }
    }
]
```

That's it. No `query_gsa` tool, no `read_memory` tool, no custom abstractions.

If an agent needs state:

```bash
curl -s http://gsa:8000/
```

If it needs to parse JSON from the response:

```bash
curl -s http://gsa:8000/ | jq '.timeline.slots | length'
```

If it needs a skill:

```bash
cat skills/audio_measurement_procedure.md
```

If it needs to check the event store:

```bash
sqlite3 /data/events/events_run_123.db "SELECT * FROM events WHERE agent='audio' ORDER BY seq DESC LIMIT 5;"
```

The agent uses bash like a human operator would.

---

## 7.2 System Prompt Structure

Every agent's system prompt (`instructions` passed to `create_deep_agent`) is
structured into protected sections. The compaction hook (§8.4) guarantees that
`=== BASE KNOWLEDGE ===` and `=== SKILL CATALOG ===` are **never removed** during
context window compression.

### 7.2.1 Prompt Sections

```
=== YOUR ROLE ===
Persona description. Who the agent is. What it values. How it approaches problems.

=== BASE KNOWLEDGE (NEVER FORGET) ===
Domain facts, procedures, formulas, thresholds, constraints.
This section survives every context window squeeze. It is the agent's
long-term memory within a run.

=== SKILL CATALOG ===
List of available skill files. The agent reads them on demand via bash_command.
This section also survives compression.

=== COMMUNICATION STYLE ===
How the agent writes its output. Critical for parseability.
See §9.1.1 for the full text copied into every agent.

=== PERMITTED EFFECTS ===
Which effect kinds the parser will extract from the agent's output.

=== WORKFLOW ===
Step-by-step guidance for typical operation.
```

### 7.2.2 Handler Appends Context

The handler adds to the user prompt (not the system prompt):

```
=== CURRENT CONTEXT ===
Run ID: {run_id}
GSA URL: {gsa_url}
Available Skills:
{skill_filenames}

=== RECENT HISTORY ===
{last_5_effects_by_this_agent}
```

The agent sees this context, then decides what to do. It curls the GSA. It
reads skills. It produces natural language. The parser extracts effects.

---

## 7.3 Context Compaction

As turns accumulate, the agent's message history grows. When it approaches the
context limit, the `on_before_compress` callback (§8.4) contracts old turns into
a compact summary before the next model call.

### 7.3.1 Protected Sections

The compaction hook walks the message history and **preserves** any message
containing:

- `=== BASE KNOWLEDGE (NEVER FORGET) ===`
- `=== SKILL CATALOG ===`

These sections are never compacted. They survive every squeeze. All other
messages (agent outputs, bash results, GSA responses) are eligible for
compression.

### 7.3.2 What Gets Compressed

The compaction LLM preserves:
- Active block addresses and their current status
- Attempt counts and failure reasons
- Budget spent so far
- VM instance IDs and their health
- Recent decisions and outcomes

The compaction LLM discards:
- Full bash command outputs (agent can re-run if needed)
- Verbatim narration text from completed blocks
- Redundant NoOp turns
- Old success details for clean blocks

---

## 7.4 Skills: On-Demand Knowledge

A **skill** is a domain-knowledge text file in the `skills/` directory. Skills
are Markdown files. No DSL, no schema, no registry service.

### 7.4.1 Skill Files

```
skills/
├── narrative_structure.md
├── voice_guidelines.md
├── duration_pacing.md
├── revision_handling.md
├── tts_job_creation.md
├── whisperx_measurement.md
├── tolerance_math.md
├── failure_recovery.md
├── batching_strategy.md
├── vm_health_check.md
├── ltx_prompt_engineering.md
├── visual_coherence_judging.md
├── audio_sync_verification.md
├── ffmpeg_muxing.md
├── otio_validation.md
├── output_verification.md
├── provision_vm.md
├── dispatch_job.md
├── cost_optimization.md
└── exa_research.md
```

### 7.4.2 Skill Format

Each skill file contains:

```markdown
# Skill: Tolerance Math

## When to use
You are judging measured audio blocks (status=measured).

## Procedure
1. For each block, compute delta = |measured_sec - scripted_sec|
2. Compute tolerance = max(scripted_sec * 0.15, 0.25)
3. If delta <= tolerance: the block passes
4. If delta > tolerance: the block fails

## Example
Block A1:3:1: scripted=4.00s, measured=4.23s
Delta = 0.23s. Tolerance = max(4.00*0.15, 0.25) = 0.60s.
0.23 <= 0.60 → PASS.
```

### 7.4.3 How Agents Use Skills

The agent's system prompt lists all skill filenames in `=== SKILL CATALOG ===`.
When the agent encounters a situation it needs help with, it reads the skill:

```bash
bash_command("cat skills/tolerance_math.md")
```

The skill text enters the message history. The agent reasons with it. On the
next turn, if the skill is no longer needed, the compaction hook may compress
it away. The skill catalog in the system prompt remains, so the agent can
re-read it anytime.

**Why not handler-injected?** The handler does not know what the agent needs.
Only the agent knows what it doesn't know. The agent decides when to read a
skill, just like a human developer decides when to read documentation.

---

## 7.5 Role Instructions

Role instructions are the `=== YOUR ROLE ===` section of the system prompt.
They are static — the same every turn. They establish persona and domain
context.

```python
ROLE_INSTRUCTIONS: dict[str, str] = {
    "scenario": (
        "You are the Scenario Agent. You write and revise narration scripts "
        "for documentary films. You are a creative writer who understands "
        "pacing, tone, narrative structure, and audio-visual constraints."
    ),

    "audio": (
        "You are the Audio Agent. You own the entire audio pipeline from "
        "script to measured audio. You are methodical, resourceful, and "
        "strategic. You plan across multiple turns and escalate only after "
        "exhausting reasonable options."
    ),

    "video": (
        "You are the Video Agent. You generate visual clips using LTX-2.3. "
        "Measured audio duration is LAW — every video must match its audio exactly."
    ),

    "assembly": (
        "You are the Assembly agent. You compose the final documentary from "
        "approved audio and video clips. You validate everything before assembly "
        "and verify output after."
    ),

    "provisioner": (
        "You are the Provisioner Agent. You are the ONLY entity that provisions "
        "GPU VMs and dispatches jobs. You manage infrastructure with precision "
        "and learn from experience. You never troubleshoot — you follow what worked."
    ),
}
```

The full system prompt for each agent concatenates:
`ROLE_INSTRUCTIONS[role]` + `BASE KNOWLEDGE` + `SKILL CATALOG` +
`COMMUNICATION STYLE` + `PERMITTED EFFECTS` + `WORKFLOW`.

See §9 for the complete system prompts for each agent.

---

## 7.6 What bash_command Gives the Agent

| Need | Bash Command |
|---|---|
| Query GSA | `curl -s http://gsa:8000/` |
| Parse GSA JSON | `curl -s http://gsa:8000/ | jq '.timeline.slots'` |
| Read skill | `cat skills/tolerance_math.md` |
| List skills | `ls skills/` |
| Check event store | `sqlite3 /data/events/events_{run_id}.db "SELECT ..."` |
| Run Vast.ai | `vastai search offers --type on-demand --raw` |
| Health check worker | `curl -s http://worker-ip:8880/` |
| Dispatch job to worker | `curl -s -X POST http://worker-ip:8880/ -d '{payload}'` |
| Run ffmpeg | `ffmpeg -i input.wav -i input.mp4 -c copy output.mp4` |
| File operations | `cat`, `ls`, `wc`, `head`, `tail` |
| jq JSON parsing | `jq '.jobs.pending | length'` |

---

## 7.7 jq for JSON Parsing

Agents use `jq` inside `bash_command` to parse structured GSA responses. This
is more reliable than asking the LLM to parse JSON mentally.

```bash
# Count pending jobs
bash_command('curl -s http://gsa:8000/ | jq ".jobs.pending | length"')

# Extract first pending job ID
bash_command('curl -s http://gsa:8000/ | jq -r ".jobs.pending[0].job_id"')

# Check if any VM is unhealthy
bash_command('curl -s http://gsa:8000/ | jq ".vms.active[] | select(.status != \"ready\")"')

# List dirty block addresses
bash_command('curl -s http://gsa:8000/ | jq -r ".timeline.slots | to_entries[] | select(.value.status == \"scripted\") | .key"')
```

---

## 7.8 Test Agents

Test agents are **full deepagents** with `bash_command`, skills, and reasoning.
They are not restricted. They validate the architecture by driving the pipeline
end-to-end and asserting correctness.

See §9.6 for test agent specifications.

---
