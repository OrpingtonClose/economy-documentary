> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Effect Parser Specification V5

> Architecture-only document. No code.
>
> Defines how agent free text is converted into typed Effects.

---

## 1. Core Rule: Never Regex

The parser **never uses regex** to extract effects from agent text. Instead it uses:

1. **String find** (`"kind: update_script" in text.lower()`) to detect candidate kinds
2. **`instructor`** + `deepseek-v4-flash` to map text to Pydantic models
3. **Category-conditioned extraction** — only attempt kinds relevant to the agent's current category

---

## 2. Category-Conditioned Extraction

### 2.1 Categories

| Category | Agent | Expected Kinds |
|---|---|---|
| `script` | Scenario | `update_script`, `delete_scene`, `reorder_scenes` |
| `jobs` | Audio, Video | `queue_job`, `job_approved`, `job_requeued` |
| `reconciliation` | Audio | `audio_generated`, `audio_measured`, `duration_adjusted`, `reconciliation_failed`, `reconciliation_complete` |
| `vm` | Provisioner | `vm_allocated`, `vm_deallocated`, `vm_provision_failed` |
| `otio` | Audio, Video | `merge_into_otio`, `delete_from_otio` |
| `pipeline` | State machine | `transition_state`, `pipeline_complete`, `pipeline_aborted` |
| `bash` | Any | `execute_raw_bash` |
| `human` | Parser, State machine | `human_instruction`, `clarification_request`, `agent_loop_detected` |

### 2.2 Flow

```
Agent produces free text
        │
        ▼
Parser receives (text, category)
        │
        ▼
Step 1: Extract candidate kinds (string find)
        For each known kind in category:
            if kind marker found in text → add to candidates
        │
        ▼
Step 2: For each candidate kind:
            model = KIND_TO_MODEL[kind]
            Try instructor extraction:
                messages = [
                    system: "Extract structured data from agent report",
                    user: f"Extract {kind} from:\n\n{text}"
                ]
                response_model = model
                max_retries = 2
            If success → add to effects
            If failure → log, continue
        │
        ▼
Step 3: If no effects extracted:
            → Emit NoOp(agent="parser", reason="no_effects_extracted")
            → Optionally emit ClarificationRequest
        │
        ▼
Return list[Effect]
```

---

## 3. Kind Markers

Agents are instructed to include kind markers in their output. The parser looks for these markers using simple string find.

### 3.1 Marker Format

```
Kind: update_script

or

Kind: update_script / delete_scene / reorder_scenes

or

I've written the script.
Kind: update_script
Scene: 3
Speaker: V1
...
```

### 3.2 Marker List

```
Kind: update_script
Kind: delete_scene
Kind: reorder_scenes
Kind: queue_job
Kind: job_completed
Kind: job_failed
Kind: job_requeued
Kind: job_approved
Kind: audio_generated
Kind: audio_measured
Kind: duration_adjusted
Kind: reconciliation_failed
Kind: reconciliation_complete
Kind: vm_allocated
Kind: vm_deallocated
Kind: vm_provision_failed
Kind: merge_into_otio
Kind: delete_from_otio
Kind: production_failed
Kind: transition_state
Kind: pipeline_started
Kind: pipeline_complete
Kind: pipeline_aborted
Kind: execute_raw_bash
Kind: human_instruction
Kind: clarification_request
Kind: agent_loop_detected
Kind: noop
```

---

## 4. Instructor Configuration

### 4.1 Client

```python
import instructor
from openai import AsyncOpenAI

client = instructor.from_openai(
    AsyncOpenAI(
        api_key=deepseek_api_key,
        base_url="https://api.deepseek.com/v1",
    )
)
```

### 4.2 Extraction Call

```python
async def extract_effect(text: str, kind: str, model: type[Effect]) -> Effect | None:
    try:
        effect = await client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an effect extractor. Read the agent's report "
                        "and extract the structured data for the requested kind. "
                        "Be conservative. If uncertain, return fewer fields. "
                        "Never hallucinate data not present in the text."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Extract '{kind}' effect from this agent report:\n\n{text}",
                },
            ],
            response_model=model,
            max_retries=2,
        )
        return effect
    except Exception:
        return None
```

### 4.3 Conservative Extraction

- If instructor returns a model with mostly default values → likely hallucination
- If required fields are missing after max_retries → skip this candidate
- If ALL candidates fail → `NoOp` + `ClarificationRequest`

---

## 5. Multi-Effect Extraction

An agent may produce multiple effects in one turn. The parser handles this by:

1. Finding all candidate kinds in the text
2. Extracting each independently
3. Returning all successful extractions

### 5.1 Example

```
Agent output:
    I've generated the TTS for scene 1.
    Kind: audio_generated
    Job: j1
    Block: scene_001_V1
    File: /tmp/scene1.wav

    WhisperX measured it at 5.2 seconds.
    Kind: audio_measured
    Job: j1
    Block: scene_001_V1
    Measured: 5.2
    Confidence: 0.95

    This is within tolerance of the scripted 5.0s.
    Kind: duration_adjusted
    Block: scene_001_V1
    Scripted: 5.0
    Measured: 5.2
    Delta: 0.2
    Tolerance: 0.25

Parser result:
    [AudioGenerated(...), AudioMeasured(...), DurationAdjusted(...)]
```

---

## 6. Failure Modes

### 6.1 No Kind Markers Found

```
Agent output: "I'm thinking about the next scene..."

→ No kind markers detected
→ NoOp(agent="parser", reason="no_kind_markers")
→ Agent will be re-prompted on next tick
```

### 6.2 Kind Marker But Extraction Fails

```
Agent output: "Kind: update_script\nScene: 3\n..."

→ Candidate: update_script
→ Instructor fails (max_retries exceeded)
→ Skip this candidate
→ If no other candidates: NoOp + ClarificationRequest
```

### 6.3 Ambiguous Kind

```
Agent output: "Kind: queue_job / job_approved"

→ Two candidates detected
→ Try extracting both
→ If both succeed: return both (agent intended multiple effects)
→ If one fails: return the successful one
```

### 6.4 Invalid Field Values

```
Agent output: "Kind: queue_job\nJob type: video"

→ Candidate: queue_job
→ Model requires job_type in {"tts", "ltx"}
→ "video" is invalid
→ Instructor validation fails
→ After max_retries: skip candidate
```

---

## 7. Performance

- **String find:** O(n) per kind, trivial
- **Instructor call:** ~500ms-2s per candidate (DeepSeek v4-flash)
- **Typical turn:** 1-3 effects → 1-3 instructor calls → 1-3 seconds total
- **No caching:** Every turn re-parses fresh. No parser state.

---

## 8. Agent Instructions for Parser

Agents are given this instruction in their system prompt:

```
Report your work with a kind marker. Use exactly one of these kind names:

Kind: update_script / delete_scene / reorder_scenes / queue_job / job_completed
      / job_failed / job_requeued / job_approved / audio_generated / audio_measured
      / duration_adjusted / reconciliation_failed / reconciliation_complete
      / vm_allocated / vm_deallocated / vm_provision_failed / merge_into_otio
      / delete_from_otio / production_failed / transition_state / pipeline_started
      / pipeline_complete / pipeline_aborted / execute_raw_bash / human_instruction
      / clarification_request / agent_loop_detected / noop

Describe what happened naturally. Include all relevant details in your description.
The parser will extract structured data from your text.
```

---

*Version: 2026-05-17 v5*
