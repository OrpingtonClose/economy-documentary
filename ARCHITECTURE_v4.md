# Architecture v4 — Event-Sourced Documentary Pipeline

## Source of Truth

The **event log** (`events.jsonl`) is the ONLY source of truth.
OTIO, the job queue, and all other state are **read models** rebuilt from events.
Gaps in the read models drive the next actions.

## Agents

- Agents produce **free text only**. Never force structured output.
- Agents have **one tool: bash**. They use it freely to inspect state, run commands, provision VMs.
- Agent outputs are parsed by **instructor + DeepSeek v4-flash** into typed `Effect`.
- Agents have **persistent memory** via singleton pattern with `_message_history`.

## Effects (Event Types)

Effects are the ONLY things that can exist in the event log.

### Creative Effects (from domain agents)
- `UpdateScript` — narration_v1/v2/v3, visual_notes, dopamine_hook, pronunciation_hints, duration_sec
- `GenerateNarrationAudio` — voice (V1/V2/V3), text
- `RenderVideoSegment` — prompt, lora_id, duration_sec
- `MergeIntoOTIO` — audio_clips, video_clips

### VM Effects (from provisioner agent outcomes)
- `VMAllocated` — instance_id, offer_id, gpu_type, worker_url
- `VMDeallocated` — instance_id, reason
- `VMProvisionFailed` — offer_id, error_message

### Worker Effects (from worker execution)
- `JobQueued` — job_type, stage, scene_num, payload (implicit from GenerateNarrationAudio/RenderVideoSegment)
- `JobStarted` — job_id, worker_id, instance_id, stage
- `JobCompleted` — job_id, artifact_path, local_artifact_path, stage
- `JobFailed` — job_id, error_message, stage
- `JobQuestionReceived` — job_id, question, worker_url
- `JobQuestionAnswered` — job_id, answer

### QA Effects (from QA jury)
- `QAPassed` — job_id, artifact_path, verdict
- `QAFailed` — job_id, artifact_path, verdict, comments, suggested_fix
- `JobRequeued` — job_id, comments, suggested_fix

### Collaboration Effects
- `JobQuestionReceived` — worker asked a clarifying question before proceeding
- `JobQuestionAnswered` — pipeline (orchestrator or agent) provided an answer

### System Effects
- `NoOp` — reason

**REMOVED:** `ExecuteRawBash` — bash is what agents DO, not an effect. Effects record OUTCOMES.

## Read Models

### OTIO Timeline
- Rebuilt from events by `projection_handler.py`
- Metadata stores script, visual notes, dopamine hook, pronunciation hints
- Tracks contain narration audio and video clips
- Provenance stored for every field

### Job Queue
- **NOT a SQLite database**. Derived purely from events.
- `GenerateNarrationAudio` → pending audio job
- `RenderVideoSegment` → pending video job
- `JobStarted` → job running
- `JobCompleted` → job completed with artifact (local_artifact_path set after download)
- `JobFailed` → job failed (retry or permanent)
- `JobQuestionReceived` → job running but awaiting answer
- `JobQuestionAnswered` → answer ready, will be sent to VM on next dispatch
- `QAFailed` + `JobRequeued` → job back to pending with comments

## Pipeline Flow

1. **Startup cleanup**
   - Run bash to list orphan VMs: `vastai show instances --raw`
   - Run bash to destroy each orphan: `vastai destroy instance <id>`
   - Append `VMDeallocated` effect for each destroyed VM
   - Clear event log (fresh run)

2. **Cycle loop** (max 50 cycles)
   a. Read all events from event log
   b. Project OTIO timeline from events
   c. Project queue state from events
   d. **Converse with orchestrator agent** — send projected state, ask what to do next
   e. Orchestrator returns: which agent to run, what prompt to give them, any bash commands needed
   f. Call agent via DeepSeek API (free text)
   g. Parse agent text into effects via instructor
   h. **For VM effects**: execute corresponding bash command, then append effect to log
   i. **For creative effects**: append directly to log; queue state updates on next projection
   j. Answer any questions workers asked (`JobQuestionReceived` → orchestrator → `JobQuestionAnswered`)
   k. Dispatch pending jobs to workers (including sending answers to running jobs)
   l. Workers append `JobStarted`/`JobCompleted`/`JobFailed`/`JobQuestionReceived` effects to log
   m. Try assembly if all media ready
   n. Stop when complete or no progress

## Orchestrator Agent

The orchestrator is a special agent that maintains global state awareness.
- It reads the projected OTIO state and queue state
- Other agents do NOT read state directly — they receive state summaries from the orchestrator
- The orchestrator decides which agent runs next based on gaps
- The orchestrator can be asked: "What should I do next?" and responds with agent selection + prompt guidance
- The orchestrator output is parsed for orchestration decisions, not stored as effects

## VM Agent Pattern

- VMs are provisioned via bash commands driven by `VMAllocated` effects
- VMs run a self-monitoring agent that destroys the VM if idle for too long
- VM self-destruction is recorded as `VMDeallocated` effect (sent via HTTP callback or checked on next pipeline run)
- The pipeline destroys orphan VMs at startup as a safety net
- **VM agents are collaborators, not script runners.** They receive goal-oriented instructions, ask clarifying questions when needed, troubleshoot failures, and report outcomes using markers:
  - `RESULT:` — task complete, artifact ready
  - `QUESTION:` — need clarification before proceeding
  - `ERROR:` — unrecoverable failure after troubleshooting
- The pipeline parses these markers and routes questions back to the orchestrator for answers
- VM agents have persistent memory across HTTP POSTs (same agent instance handles all messages)

## Bash Usage

- Agents freely output bash commands in their text
- The pipeline extracts and executes bash commands BEFORE creating effects
- Effects record the OUTCOME of bash execution, not the command itself
- Example: agent says "I'll provision offer 12345" → pipeline runs `vastai create instance 12345 ...` → if success, creates `VMAllocated` effect; if failure, creates `VMProvisionFailed` effect

## No Timeouts

- **NO timeout parameters ANYWHERE in pipeline code**
- Subprocess calls have no timeout
- HTTP calls have no timeout
- Async operations have no timeout
- If anything hangs, the operator intervenes manually
- Kimi CLI hook (`~/.kimi/hooks/no_timeouts.py`) blocks any WriteFile/StrReplaceFile containing timeout mechanisms, using DeepSeek v4-flash agentic audit

## No Mocks

- All tests deleted
- Real engines only (Qwen3-TTS, LTX-Video)
- Local worker simulation only for fast testing, clearly marked

## No pydantic-graph

- Plain async loop for orchestration
- OTIO-derived graph for deciding which agent runs next
- No graph framework for pipeline control flow

## Instructor Prompting

- Chain-of-thought reasoning in system prompt
- Few-shot examples for each effect type
- Reask validation with max_retries=2
- Conservative extraction — low confidence → NoOp

## File Structure

```
server/
  run_pipeline_v4.py       # Main orchestrator
  effects.py               # All effect types
  effect_parser.py         # Instructor-based parsing
  structured_extract.py    # Shared DeepSeek instructor client
  event_store.py           # Append-only JSONL event log
  projection_handler.py    # OTIO projection from events
  queue_projection.py      # Job queue projection from events
  otio_orchestrator.py     # Compute next needed effects from OTIO state
  unit_state_machines.py   # Per-agent state machines
  scripts/vm_agent.py      # GPU worker agent (pydantic-ai + DeepSeek)
  scripts/vm_onstart_tts.sh # TTS VM bootstrap
  scripts/vm_onstart_ltx.sh # Video VM bootstrap
```

## Guardrails

1. **Kimi CLI hook** — `PreToolUse` hook on WriteFile/StrReplaceFile. Agentic DeepSeek audit blocks timeout usage.
2. **Architecture guard rules** — Block PreToolUse for timeout patterns.
3. **AGENTS.md rule** — No timeouts. Ever.

## Model Format

- pydantic-deep agents: `deepseek:deepseek-v4-flash`
- Instructor client: `deepseek-v4-flash`
