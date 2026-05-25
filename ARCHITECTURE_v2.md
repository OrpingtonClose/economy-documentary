# Architecture v2: Per-Unit State Machines + Instructor as Bridge

## Core Insight

**The pipeline is not one graph. It is 6 independent agents, each with their own internal state machine (graph/plan).**

- No central orchestrator graph
- No "routing logic" in code
- Each agent reads OTIO as "world state", decides what to do, and outputs effects
- The instructor parser is the bridge: it reads the agent's text, knows the world state, and extracts effects
- Effects are the ONLY way to modify OTIO

---

## Layer 0: The World (OTIO)

```
OTIO Timeline (read-only projection from event store)
├── A1_Narration track
├── V1_Video track  
└── Pipeline metadata
    ├── scenario_raw
    ├── visual_style
    ├── style_lock
    └── checkpoint state
```

OTIO is the "state of the world." All agents read it. No agent writes it directly.

---

## Layer 1: The Agent (Thinks Aloud, Does Bash)

```
Agent (pydantic-deep create_deep_agent)
├── System prompt: "You are the Audio Agent. You think aloud..."
├── Tools: bash_command, web_search, remember, recall
├── No knowledge of:
│   ├── Other agents
│   ├── Graph edges
│   ├── State machines
│   └── OTIO file format
└── Only knows:
    ├── What it reads from bash (e.g., cat timeline.otio)
    ├── What it remembers
    └── What feedback it receives from the instructor
```

The agent is **maximally free**. It can:
- Write bash commands to check files, run ffmpeg, curl workers
- Search the web for information
- Remember facts across invocations
- Say anything it wants

The agent is **maximally ignorant** of:
- How the pipeline works
- What other agents exist
- That effects exist
- That an instructor parses its output

---

## Layer 2: The Instructor (The Bridge)

```
Instructor Agent (operates independently per unit)
├── Reads: current OTIO state, agent's text output
├── Knows: the agent's state machine (what effects are valid now)
├── Does:
│   ├── Parses agent text → Effect
│   ├── Validates effect against state machine
│   ├── Appends valid effect to Event Store
│   ├── Triggers projection handler → rebuilds OTIO
│   └── Sends FEEDBACK to the agent
└── Feedback types:
    ├── "Effect accepted: UpdateScript for Scene 1"
    ├── "Effect rejected: RenderVideo requires audio first"
    ├── "What you need to do next: Generate narration for V2"
    └── "World state changed: A1_Narration now has 3 clips"
```

The instructor is the **only programmatic entity** that understands:
- The state machine
- The effect types
- The event store
- The projection handler

The agent never knows the instructor exists. To the agent, it just receives "feedback" after each turn.

---

## Layer 3: The Event Store & Projection

```
Event Store (append-only JSONL)
├── seq=1: UpdateScript (scenario)
├── seq=2: GenerateNarrationAudio (audio)
├── seq=3: GenerateNarrationAudio (audio)
└── seq=4: RenderVideoSegment (video)

Projection Handler
├── Listens to: new events
├── Does: apply_effect(timeline, event.effect)
└── Writes: updated OTIO file
```

---

## Per-Unit State Machines

### Scenario Agent State Machine

```
States: IDLE → HAS_SCENARIO

IDLE:
  Valid effects: UpdateScript
  Agent thinks: "I need to write a script"
  Agent outputs: "Scene 1 — The Anatomy..."
  Instructor parses: UpdateScript
  → Transition to HAS_SCENARIO

HAS_SCENARIO:
  Valid effects: UpdateScript (refinements)
  Agent thinks: "Script is done. I'll wait for feedback."
  Feedback: "Scenario accepted. Audio agent will generate narration."
  → Agent goes idle (pipeline continues via other agents)
```

### Audio Agent State Machine

```
States: IDLE → PENDING_AUDIO → HAS_AUDIO

IDLE:
  Valid effects: GenerateNarrationAudio
  Agent reads OTIO: "A1_Narration is empty. I need to generate audio."
  Agent outputs: "Generate narration for V1: Every rainbow..."
  Instructor parses: GenerateNarrationAudio
  → Transition to PENDING_AUDIO

PENDING_AUDIO:
  Valid effects: GenerateNarrationAudio (more clips)
  Agent reads OTIO: "A1 is still empty. Provisioner is working."
  Agent outputs: "Still waiting. No action needed."
  Instructor parses: NoOp
  Feedback: "Jobs pending. Waiting for provisioner."
  → Stay in PENDING_AUDIO

HAS_AUDIO:
  Valid effects: none (audio stage complete)
  Agent reads OTIO: "A1 has 3 clips. Done."
  Feedback: "Audio complete. Video agent will generate clips."
  → Agent goes idle
```

### Video Agent State Machine

```
States: IDLE → PENDING_VIDEO → HAS_VIDEO

IDLE:
  Valid effects: RenderVideoSegment
  Agent reads OTIO: "V1_Video is empty but A1 has clips."
  Agent outputs: "Render video for Scene 1: cinematic rainbow..."
  Instructor parses: RenderVideoSegment
  → Transition to PENDING_VIDEO

PENDING_VIDEO:
  Valid effects: RenderVideoSegment
  Agent reads OTIO: "V1 is still empty. Waiting."
  Instructor parses: NoOp
  Feedback: "Jobs pending. Waiting for provisioner."
  → Stay in PENDING_VIDEO

HAS_VIDEO:
  Valid effects: none
  Feedback: "Video complete. Assembly agent will merge."
  → Agent goes idle
```

### Assembly Agent State Machine

```
States: IDLE → MERGING → COMPLETE

IDLE:
  Valid effects: MergeIntoOTIO, ExecuteRawBash
  Agent reads OTIO: "A1 and V1 both have clips."
  Agent outputs: "Merge clips into timeline."
  Instructor parses: MergeIntoOTIO
  → Transition to MERGING

MERGING:
  Valid effects: ExecuteRawBash (ffmpeg commands)
  Agent outputs: "Execute bash: ffmpeg -i ... -c copy output.mp4"
  Instructor parses: ExecuteRawBash
  → Transition to COMPLETE

COMPLETE:
  Valid effects: none
  Feedback: "Pipeline complete."
  → Agent goes idle
```

### Provisioner Agent State Machine

```
States: IDLE → PROVISIONING → EXECUTING → IDLE

IDLE:
  Valid effects: ExecuteRawBash (VM operations)
  Agent reads queue: "2 pending audio jobs."
  Agent outputs: "bash: vastai search offers..."
  Instructor parses: ExecuteRawBash
  → Transition to PROVISIONING

PROVISIONING:
  Valid effects: ExecuteRawBash
  Agent outputs: "bash: vastai create instance..."
  Instructor parses: ExecuteRawBash
  → Transition to EXECUTING

EXECUTING:
  Valid effects: ExecuteRawBash
  Agent outputs: "bash: curl -X POST ... > output.wav"
  Instructor parses: ExecuteRawBash
  Feedback: "Job completed. Marking done."
  → Transition to IDLE
```

---

## Communication Flow

```
┌─────────────────────────────────────────────────────────────┐
│  Agent (thinks aloud, does bash)                            │
│  "I need to generate audio for V1. Bash: curl ..."          │
└──────────────────────┬──────────────────────────────────────┘
                       │ HTTP POST plain text
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  Instructor Agent                                           │
│  1. Reads OTIO world state                                  │
│  2. Checks agent's state machine                            │
│  3. Parses text with instructor (model shifts by state)     │
│  4. Validates extracted effect                              │
│  5. Appends to event store                                  │
│  6. Triggers projection handler                             │
│  7. Sends feedback to agent                                 │
└──────────────────────┬──────────────────────────────────────┘
                       │ HTTP Response (plain text feedback)
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  Agent receives feedback                                    │
│  "Effect accepted: GenerateNarrationAudio for V1.           │
│   Next: generate V2 audio."                                 │
│  Agent thinks: "OK, I'll generate V2 next..."               │
└─────────────────────────────────────────────────────────────┘
```

---

## Instructor Parsing Model Shifting

The instructor uses **different parsing models** depending on the agent's current state:

```python
def parse_for_state(agent_id: str, state: str, text: str) -> Effect:
    """Shift parsing based on agent's state machine state."""
    
    parsers = {
        ("scenario", "IDLE"): _parse_scenario_idle,
        ("scenario", "HAS_SCENARIO"): _parse_scenario_refinement,
        ("audio", "IDLE"): _parse_audio_idle,
        ("audio", "PENDING_AUDIO"): _parse_audio_pending,
        ("audio", "HAS_AUDIO"): _parse_audio_complete,
        ("video", "IDLE"): _parse_video_idle,
        ("video", "PENDING_VIDEO"): _parse_video_pending,
        ("assembly", "IDLE"): _parse_assembly_idle,
        ("assembly", "MERGING"): _parse_assembly_merge,
        ("provisioner", "IDLE"): _parse_provisioner_idle,
    }
    
    parser = parsers.get((agent_id, state), _parse_generic)
    return parser(text)
```

Each parser knows:
- What effects are valid in this state
- What fields to expect
- What to reject

---

## Feedback to Agent

After each turn, the instructor sends feedback:

```
FEEDBACK:
- Your last message was parsed as: {effect_type}
- Status: {accepted / rejected}
- Reason: {why}
- World state: {OTIO summary}
- What you should do next: {suggestion}
- Valid actions now: {list of effect types}
```

Example:
```
FEEDBACK:
- Parsed as: GenerateNarrationAudio for V1
- Status: ACCEPTED
- Reason: Audio stage is active, V1 is valid
- World state: A1_Narration has 0 clips
- Suggestion: Generate V2 and V3 audio next
- Valid actions: GenerateNarrationAudio, NoOp
```

---

## Key Principle: Agent is Maximally Free, Instructor is Maximally Constrained

| Aspect | Agent | Instructor |
|--------|-------|------------|
| Knows state machine? | NO | YES |
| Knows effect types? | NO | YES |
| Knows event store? | NO | YES |
| Knows other agents? | NO | YES |
| Can do bash? | YES | NO |
| Can search web? | YES | NO |
| Can remember? | YES | NO |
| Output format | Plain text | Typed effects |
| Receives feedback | YES | NO |

The agent is a **free-thinking entity** that operates in a constrained world.
The instructor is the **gatekeeper** that translates free thought into constrained effects.

---

## Files (New Architecture)

```
server/
├── effects.py                    # Algebraic effect types
├── event_store.py                # Append-only event log
├── projection_handler.py         # Apply effects to OTIO
├── instructor.py                 # Per-unit parser + state machine + feedback
├── unit_state_machines.py        # State machine definitions per unit
├── pydantic_deep_agents/         # HTTP services (6 agents)
│   ├── scenario_agent.py
│   ├── audio_agent.py
│   ├── video_agent.py
│   ├── assembly_agent.py
│   ├── provisioner_agent.py
│   └── otio_gate_agent.py       # Not needed — instructor replaces gate
├── run_pipeline.py               # Entry point
└── test_pipeline.py              # Dry-run test
```

## What Changes from v1

1. **No central orchestrator graph** — each unit has its own state machine
2. **No otio_gate agent** — instructor replaces it
3. **Agent system prompts simplified** — no routing instructions, no format requirements
4. **Feedback loop added** — instructor sends feedback after each turn
5. **State machine per unit** — instructor tracks and enforces
6. **Parsing shifts by state** — different models for different states
