---
{
  "title": "Data Flows",
  "section": "12",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

<- [[11 - VM Worker|VM Worker]] | [[00 - Index|Index]] | [[13 - Security Model|Security Model]] ->

# Data Flows


This chapter traces the four principal data flow patterns through the V7 pipeline: the agent activation cycle, the reconciliation loop with VM-mediated TTS, the script back-edge with partial re-reconciliation, and human intervention. Each flow is presented as a text-based sequence diagram showing actor interactions, followed by a step-by-step specification.

---

### 12.1 Agent Activation Cycle

#### 12.1.1 Agent HTTP service cycle with projection updates

```
  +--------------+     +--------------+     +--------------------+     +--------------+
  | Human Op. or |     | Agent POST   |     | Global State Agent |     |  Agent LLM   |
  | Caller       |     | Handler      |     | (port 8000)        |     | (deepseek-   |
  |              |     | (agent port) |     | GET / only         |     |  v4-flash)   |
  +----+---------+     +------+-------+     +---------+----------+     +------+-------+
       |                      |                      |                    |
       | 1. POST /            |                      |                    |
       | (instruction or      |                      |                    |
       |  poll trigger)       |                      |                    |
       |--------------------->|                      |                    |
       |                      | 2. GET /             |                    |
       |                      | (fetch projections)  |                    |
       |                      |--------------------->|                    |
       |                      |                      | 3. build narrative |
       |                      |                      |    from proj       |
       |                      |                      |------------------->|
       |                      |                      |                    | 4. LLM runs
       |                      |                      |                    |    produces text
       |                      |                      |<-------------------|
       |                      |                      | 5. parser extracts |
       |                      |                      |    effects         |
       |                      |<---------------------| 6. append_effect() |
       |                      | (to SQLite store)    |                    |
       |<---------------------|                      |                    |
       | 7. return 200        |                      |                    |
       |    (agent polls      |                      |                    |
       |     again later)     |                      |                    |
```

**Step-by-step specification:**

| Step | Actor | Action | Specification |
|---|---|---|---|
| 1 | Human operator / Caller | POST / | HTTP POST to agent's port with raw text content representing instruction/prompt |
| 2 | Agent Handler | Build prompt | Reads last 5 effects from SQLite store; lists skill filenames; constructs prompt |
| 3 | Agent LLM | Execute | `agent.run(user_prompt=prompt, deps=PipelineDeps)` via pydantic-deep. No timeout (§1.4) |
| 4 | Agent (internal) | Explore | Uses `bash_command` to curl GSA, read skills, run diagnostics as needed |
| 5 | Agent Handler | Parse effects | `parse_agent_text_multi()` via instructor extracts typed effects from agent's final text (§9.5) |
| 6 | Agent Handler | Append | Effects written to SQLite database `events.db` with `effect_id` deduplication |
| 7 | Agent Handler | Return | Returns `200 OK` with extracted effect kinds; returns effects to caller |

The cycle is **autonomous** — each agent polls the GSA independently. Agents run when:
- Their internal poll timer fires
- The human operator POSTs an instruction
- The Provisioner POSTs a job-completion notification
- The GSA polls DB files every second for new events (EventStoreDB provides native push subscriptions for distributed deployments)

---

### 12.2 Reconciliation Loop (Detailed)

The reconciliation loop spans four physical components — Audio Agent, SQLite Event Store, Provisioner, and VM Worker — and iterates until every narration block passes the tolerance check or exhausts its attempt budget.

#### 12.2.1 Audio Agent ↔ SQLite Event Store ↔ Provisioner ↔ VM Worker (TTS path)

```
Audio Agent (8002)          SQLite Store             Provisioner (8081)    VM Worker (9000)
      |                         |                        |                      |
      |-- QueueJob (TTS) ----->|                        |                      |
      |  job_type=tts           |                        |                      |
      |  block_id=A1:1:1       |                        |                      |
      |  text="In 1924..."     |                        |                      |
      |                         |<-- POST / -------------|                      |
      |                         |  Provisioner reads     |                      |
      |                         |  Jobs: 1               |                      |
      |                         |  pending (tts)         |                      |
      |                         |                        |-- offer matching     |
      |                         |                        |  (direct bash)       |
      |                         |                        |                      |
      |                         |<-- VMAllocated -------|                      |
      |                         |  instance_id=vast-42   |                      |
      |                         |                        |-- POST / (job) ----->|
      |                         |                        |  JobRequest JSON     |
      |                         |                        |  + callback_url      |
      |                         |                        |                      |
      |                         |                        |<-- 202 Accepted -----|
      |                         |                        |                      |
      |                         |                        |<-- POST / -----------|
      |                         |                        |   (job result)       |
      |                         |                        |  JobResult JSON      |
      |                         |                        |  measurements=[5.12, |
      |                         |                        |  5.08, 5.15]         |
      |                         |                        |                      |
      |                         |<-- JobCompleted ------|                      |
      |                         |  artifact=/tmp/...     |                      |
      |                         |  duration_median=5.12  |                      |
      |                         |  measurements=[5.12,  |                      |
      |                         |    5.08, 5.15]        |                        |
      |                         |                        |                      |
      |<-- poll cycle --------|                        |                      |
      | "new job completed"     |                        |                      |
      |                         |                        |                      |
      |-- AudioMeasured ------->|                        |                      |
      |   block=A1:1:1         |                        |                      |
      |   measurements=[5.12,  |                        |                      |
      |    5.08, 5.15]         |                        |                      |
      |   median=5.12           |                        |                      |
      |                         |                        |                      |
```

#### 12.2.2 3× WhisperX measurement flow

The VM Worker executes WhisperX three times sequentially (decision C2). Each run loads the WhisperX model, transcribes the generated WAV, and reports the end timestamp of the final segment. All three values are returned in `JobResult.measurements` as raw floats. The Audio Agent computes the median client-side.

```python
# Inside VM Worker (Section 11.2.3)
measurements: list[float] = []
for run in range(3):
    segments = await _whisperx_transcribe(audio_path, model="large-v3")
    measurements.append(segments[-1]["end"] if segments else 0.0)
# Returns: [5.12, 5.08, 5.15]

# Inside Audio Agent
import statistics
median_sec = statistics.median(job_result.measurements)  # 5.12
```

The three runs execute sequentially to avoid CPU contention on the shared WhisperX process. Runs are not parallelized across GPU — the model is CPU-bound for transcription.

#### 12.2.3 Within tolerance → DurationAdjusted

```
Audio Agent (8002)          SQLite Store             Provisioner (8081)    VM Worker (9000)
      |                         |                        |                      |
      | [ median=5.12,          |                        |                      |
      |   scripted=5.0,         |                        |                      |
      |   delta=+0.12s,         |                        |                      |
      |   tolerance=max(        |                        |                      |
      |     5.0*0.15=0.75,     |                        |                      |
      |     0.25)=0.75s ]       |                        |                      |
      |   delta < tolerance     |                        |                      |
      |   → PASS                |                        |                      |
      |                         |                        |                      |
      |-- DurationAdjusted ---->|                        |                      |
      |   block=A1:1:1         |                        |                      |
      |   measured=5.12         |                        |                      |
      |   scripted=5.0          |                        |                      |
      |   delta=+0.12           |                        |                      |
      |   within_tolerance=true |                        |                      |
      |                         |                        |                      |
      | [ Next activation:      |                        |                      |
      |   OTIO Projection merges|                        |                      |
      |   5.12s into slot       |                        |                      |
      |   A1:1:1. Block marked  |                        |                      |
      |   measured. ]           |                        |                      |
      |                         |                        |                      |
      |-- QueueJob (TTS) ----->|   [ proceed to next    |                      |
      |   block=A1:1:2         |     block A1:1:2 ]     |                      |
```

The tolerance rule (§7.3.3) is **max(15% of scripted duration, 0.25s)**. For a 5.0s target, tolerance = max(0.75, 0.25) = 0.75s. A measured 5.12s (delta +0.12s) passes. The `DurationAdjusted` effect updates the OTIO Projection, which on the next activation applies the measured duration to the corresponding slot.

#### 12.2.4 Outside tolerance → ReconciliationFailed → JobRequeued → retry

```
Audio Agent (8002)          SQLite Store             Provisioner (8081)    VM Worker (9000)
      |                         |                        |                      |
      | [ median=7.2,           |                        |                      |
      |   scripted=5.0,         |                        |                      |
      |   delta=+2.2s,          |                        |                      |
      |   tolerance=0.75s ]     |                        |                      |
      |   delta > tolerance     |                        |                      |
      |   → FAIL                |                        |                      |
      |   attempt=2/5           |                        |                      |
      |                         |                        |                      |
      |-- ReconciliationFailed >|                        |                      |
      |   block=A1:1:2         |                        |                      |
      |   measured=7.2          |                        |                      |
      |   scripted=5.0          |                        |                      |
      |   delta=+2.2            |                        |                      |
      |   failure_type=         |                        |                      |
      |     duration_mismatch   |                        |                      |
      |                         |                        |                      |
      |-- JobRequeued --------->|                        |                      |
      |   job_id=<old>          |                        |                      |
      |   reason="too long      |                        |                      |
      |     by 2.2s"            |                        |                      |
      |   adjusted_text=        |                        |                      |
      |     "In '24..."         |                        |                      |
      |   (shortened)           |                        |                      |
      |                         |                        |                      |
      |-- QueueJob (TTS v2) -->|                        |                      |
      |   block=A1:1:2          |                        |                      |
      |   text="In '24..."      |                        |                      |
      |   attempt=2             |                        |                      |
      |                         |                        |                      |
      | [ Next activation:      |                        |                      |
      |   Provisioner sees      |                        |                      |
      |   new QueueJob,         |                        |                      |
      |   allocates VM,         |                        |                      |
      |   loop repeats... ]     |                        |                      |
```

When the measured duration exceeds tolerance, the Audio Agent computes an adjusted text (shortening or splitting the phrase) and requeues. Each block has a maximum of **5 attempts** (§7.3.4). If attempts are exhausted, the parser extracts `ReconciliationFailed` from Audio Agent output with `failure_type="duration_unrecoverable"`, which triggers a back-edge to `SCRIPT` (the text is physically unachievable at the target duration).

#### 12.2.5 All pass → ReconciliationComplete

When every block in the narration has been measured and passes tolerance, the parser extracts `ReconciliationComplete` from Audio Agent output:

```
Audio Agent (8002)          SQLite Store
      |                         |
      | [ All blocks measured:   |
      |   A1:1:1=5.12s PASS     |
      |   A1:1:2=4.89s PASS     |
      |   ...                     |
      |   A1:3:5=3.01s PASS ]   |
      |                         |
      |-- ReconciliationComplete >|
      |   blocks_total=14       |
      |   blocks_passed=14      |
      |   blocks_failed=0       |
      |   otio_authoritative=   |
      |     true                |
      |                         |
      | [ Next activation:      |
      |   Video Agent checks    |
      |   projections, sees     |
      |   reconciliation_complete|
      |   and clean blocks →    |
      |   begins VIDEO_PRODUCTION]|
```

The `ReconciliationComplete` effect is the **gateway** from `AUDIO_RECONCILE` to `VIDEO_PRODUCTION`. Agents check for `ReconciliationComplete` and clean blocks to decide whether to begin video generation. The OTIO Projection's measured durations become **authoritative** — the Video Agent uses them as LAW for LTX-2.3 clip generation.

| Parameter | Value | Source |
|---|---|---|
| Tolerance | ±15% or ±0.25s (whichever is larger) | §7.3.3 |
| Max attempts per block | 5 | §7.3.4, `max_attempts_per_block` config |
| Max TTS budget | $2.00 USD | §7.3.4, `max_tts_budget_usd` config |
| WhisperX runs per measurement | 3 | §9.2.3, decision C2 |
| Median computation | Client-side (Audio Agent) | §7.3.2 |

---

### 12.3 Script Failure → Back-Edge with Partial Re-reconciliation

When a script revision invalidates only some blocks, V7 performs **partial re-reconciliation**: unchanged blocks keep their measured durations; only dirty blocks are re-processed. Dirty/clean marking is done by `Timeline._build_from_script()` — not by the Audio Agent. The `Jobs` syncs its `dirty_blocks`/`clean_blocks` sets from the `Timeline` on every `UpdateScript`.

#### 12.3.1 voice_mismatch in VIDEO_PRODUCTION → Transition to SCRIPT

```
Video Agent (8003)          SQLite Store            Scenario Agent (8001)
      |                         |                        |
      | [ Generates LTX-2.3     |                        |
      |   clip for scene 3.     |                        |
      |   LLM judges: "Voice    |                        |
      |   is baritone, script   |                        |
      |   says soprano." ]      |                        |
      |                         |                        |
      |-- ProductionFailed --->|                        |
      |   failure_type=         |                        |
      |     voice_mismatch      |                        |
      |   scene=3               |                        |
      |   detail="baritone      |                        |
      |     vs soprano"         |                        |
      |                         |                        |
      | [ Next activation:      |                        |
      |   Scenario Agent reads  |                        |
      |   production_failures   |                        |
      |   and sees voice_mismatch|                       |
      |   in SCRIPT_RESOLVABLE_ |                        |
      |   TYPES ]               |                        |
      |                         |                        |
      |                         |<-- poll cycle --------|
      |                         |   "check failures"     |
      |                         |                        |
      |                         |<-- UpdateScript -------|
      |                         |   blocks=[...,         |
      |                         |     {scene:3, voice:   |
      |                         |      "baritone", text: |
      |                         |      "In 1924..."} ]   |
```

The Scenario Agent checks for `ProductionFailed` effects with `failure_type in {"gap_unexpected", "voice_mismatch"}`. These are the only two failure types that trigger a back-edge to `SCRIPT`; all others either requeue in-place or halt with `ClarificationRequest`.

#### 12.3.2 Scenario Agent fixes script → UpdateScript → OTIO marks dirty/clean

```
Scenario Agent (8001)         SQLite Store            OTIO Projection
      |                           |                         |
      | [ Receives instruction     |                         |
      |   with voice_mismatch      |                         |
      |   context. LLM revises     |                         |
      |   scene 3: changes voice   |                         |
      |   tag to "baritone",       |                         |
      |   adjusts narration text   |                         |
      |   to match. ]              |                         |
      |                           |                         |
      |-- UpdateScript ----------> |                         |
      |   blocks=[...,             |                         |
      |     {scene:3, voice:       |                         |
      |      "baritone", text:     |                         |
      |      "In 1924..."} ]      |                         |
      |                           |                         |
      | [ Timeline upserts         |                         |
      |   blocks. Unchanged        |                         |
      |   blocks keep measured_sec |                         |
      |   and status. Changed      |                         |
      |   blocks marked dirty      |                         |
      |   (status="scripted"). ]   |                         |
      |                           |                         |
      | [ Jobs._sync_              |                         |
      |   from_otio() runs:        |                         |
      |   updated blocks → dirty   |                         |
      |   all others → clean. ]    |                         |
      |                           |                         |
      | [ Jobs removes             |                         |
      |   the voice_mismatch       |                         |
      |   failure from the list.   |                         |
      |   This prevents infinite   |                         |
      |   script-rewrite loops. ]  |                         |
```

The Scenario Agent's system prompt includes the failure context so the LLM understands what changed. The parser extracts `UpdateScript` containing the full revised scene list. The `Timeline` performs the dirty/clean marking automatically during `_build_from_script()`. The `Jobs` syncs its dirty/clean tracking via `_sync_from_otio()`.

#### 12.3.3 Audio Agent reads dirty/clean from GSA → queues only dirty blocks

```
Audio Agent (8002)            GSA / Projections      OTIO Projection
      |                           |                         |
      | [ Receives state summary   |                         |
      |   from GSA GET /.          |                         |
      |   OTIO.slots shows: ]      |                         |
      |                           |                         |
      |   Block A1:1:1: status=    |                         |
      |     "measured" → CLEAN     |                         |
      |     (keep 5.12s)           |                         |
      |   Block A1:1:2: status=    |                         |
      |     "measured" → CLEAN     |                         |
      |     (keep 4.89s)           |                         |
      |   Block A1:3:1: status=    |                         |
      |     "scripted" → DIRTY     |                         |
      |     (voice changed)        |                         |
      |   Block A1:3:2: status=    |                         |
      |     "scripted" → DIRTY     |                         |
      |     (text shortened)       |                         |
      |   Block A1:3:3: status=    |                         |
      |     "measured" → CLEAN     |                         |
      |     (keep 3.01s)           |                         |
      |                           |                         |
      | [ Jobs confirms:           |                         |
      |   dirty_blocks={A1:3:1,    |                         |
      |   A1:3:2}                  |                         |
      |   clean_blocks={A1:1:1,    |                         |
      |   A1:1:2, A1:3:3} ]       |                         |
```

The Audio Agent does NOT compute dirty/clean itself. It reads `Timeline.slots` status and `Jobs.dirty_blocks`/`clean_blocks` from the GSA. The `Timeline` has already done the dirty marking during `_build_from_script()`.

#### 12.3.4 Only dirty blocks re-reconciled; clean blocks remain authoritative

```
Audio Agent (8002)         SQLite Store          Provisioner (8081)    VM Worker (9000)
      |                        |                      |                   |
      | [ Loop starts: only    |                      |                   |
      |   dirty blocks queued  |                      |                   |
      |   for TTS. Clean       |                      |                   |
      |   blocks skipped. ]    |                      |                   |
      |                        |                      |                   |
      |-- QueueJob (TTS) ----->|                      |                   |
      |   block=A1:3:1         |                      |                   |
      |   text="In 1924..."    |                      |                   |
      |   voice="baritone"     |                      |                   |
      |   (was "soprano")      |                      |                   |
      |                        |                      |                   |
      |-- QueueJob (TTS) ----->|                      |                   |
      |   block=A1:3:2         |                      |                   |
      |   text="He then..."    |                      |                   |
      |   (shortened)          |                      |                   |
      |                        |                      |                   |
      | [ Block A1:1:1         |                      |                   |
      |   (clean, 5.12s) is    |                      |                   |
      |   NOT queued. Block    |                      |                   |
      |   A1:1:2 (clean,       |                      |                   |
      |   4.89s) is NOT        |                      |                   |
      |   queued. ]            |                      |                   |
      |                        |                      |                   |
      | [ Reconciliation proceeds|                     |                   |
      |   for A1:3:1 and       |                      |                   |
      |   A1:3:2 only.         |                      |                   |
      |   Clean blocks remain  |                      |                   |
      |   authoritative. ]     |                      |                   |
      |                        |                      |                   |
      |-- ReconciliationComplete>|                     |                   |
      |   (when all dirty pass) |                     |                   |
```

The parser extracts effects from the Audio Agent's text only for dirty blocks. Clean blocks are never re-measured — their `AudioMeasured` values from the previous reconciliation pass remain LAW. This avoids redundant TTS spend on unchanged content.

| Block | OTIO Status | Action | Previous Measurement |
|---|---|---|---|
| A1:1:1 | measured | Skipped, retained | 5.12s (authoritative) |
| A1:1:2 | measured | Skipped, retained | 4.89s (authoritative) |
| A1:3:1 | scripted (dirty) | Re-queued for TTS | Reset to `None` |
| A1:3:2 | scripted (dirty) | Re-queued for TTS | Reset to `None` |
| A1:3:3 | measured | Skipped, retained | 3.01s (authoritative) |

---

### 12.4 Human Intervention

Human operators interact with agents via direct HTTP GET/POST to each agent's endpoint. There is no dedicated dashboard and no intermediary routing service — the agent's own endpoints serve as the observation and control surface.

#### 12.4.1 GET agent status, POST instruction

```
Human Operator                              Audio Agent (8002)
      |                                          |
      |-- GET / -------------------------------->|
      |                                          |
      |<-- AgentStatus --------------------------|
      |   name="audio"                          |
      |   status="working"                       |
      |   current_task=                          |
      |     "reconcile block                     |
      |      A1:3:1, attempt                     |
      |      3/5"                                |
      |   last_error=null                        |
      |                                          |
      | [ Human decides text                     |
      |   is fine at 5.5s,                       |
      |   override tolerance. ]                  |
      |                                          |
      |-- POST / -------------------------------->|
      |   instruction: "Accept                   |
      |     the 5.5s duration                    |
      |     for block A1:3:1.                    |
      |     It is close enough."                 |
      |                                          |
      |<-- 200 OK -------------------------------|
      |                                          |
      | [ Next agent turn:                       |
      |   instruction appears                    |
      |   in prompt context.                     |
      |   LLM produces text. ]                   |
      |                                          |
      |-- DurationAdjusted --------------------->|
      |   block=A1:3:1                           |
      |   measured=5.5                           |
      |   slot_id="A1:3:1"                       |
      |   scripted_sec=5.0                       |
```

The `AgentStatus` response includes `status`, `current_task`, `last_error`, and `idle_since`. A human reading this can determine if the agent is stuck (e.g., "attempt 4/5 on same block") and issue corrective instructions.

#### 12.4.2 bash_command Tool Flow

The agent calls `bash_command` directly as its tool during the turn. The tool
executes the shell command and returns stdout+stderr to the agent. The agent
incorporates this output into its reasoning and final text. The parser extracts
effects from the agent's final text only; tool outputs are invisible to the
parser.

There is no approval flow for `bash_command`. The agent executes freely.
Security is enforced at the infrastructure level (§13): VM workers are ephemeral,
control plane agents run in restricted environments, and dangerous commands are
blocked by host-level guards.

```
Audio Agent (8002)          pydantic-deep        Handler           SQLite Store
      |                        |                      |                    |
      | [ LLM decides to      |                      |                    |
      |   run ffmpeg via      |                      |                    |
      |   bash_command ]      |                      |                    |
      |                        |                      |                    |
      |-- bash_command ------>|                      |                    |
      |   "ffmpeg -i          |                      |                    |
      |    /tmp/x.wav         |                      |                    |
      |    -af volume=1.5     |                      |                    |
      |    /tmp/x_loud.wav"   |                      |                    |
      |                        |                      |                    |
      |                        |-- (shell exec)       |                    |
      |                        |   returns stdout     |                    |
      |                        |                      |                    |
      |<-- stdout+stderr -----|                      |                    |
      |   "Output saved to     |                      |                    |
      |    /tmp/x_loud.wav"   |                      |                    |
      |                        |                      |                    |
      | [ LLM incorporates    |                      |                    |
      |   result into final   |                      |                    |
      |   text ]              |                      |                    |
      |                        |                      |                    |
      |-- final text -------->|                      |                    |
      |   "I ran ffmpeg to    |                      |                    |
      |    increase volume    |                      |                    |
      |    for block A1:3:1.  |                      |                    |
      |    Output saved to    |                      |                    |
      |    /tmp/x_loud.wav.   |                      |                    |
      |    Duration: 4.23s."  |                      |                    |
      |                        |                      |                    |
      |                        |-- parser extracts -->|                    |
      |                        |   DurationAdjusted   |                    |
      |                        |                      |                    |
      |                        |                      |-- append effect -->|
```

**No approval flow.** `bash_command` is the agent's tool. It executes during
the turn and returns output to the agent. The parser only sees the agent's
final text, not the tool execution. Security is at the infrastructure level
(§13): VM workers are ephemeral, control plane runs restricted.

| Step | Action | Actor | Meaning |
|---|---|---|---|
| 1 | `bash_command` | Agent (via tool) | Runs shell command during turn |
| 2 | stdout+stderr | System | Command output returned to agent |
| 3 | Final text | Agent | Natural language incorporating results |
| 4 | Effect extraction | Parser | Extracts effects from final text |
| 5 | Append | Handler | Writes effects to SQLite store |

#### 12.4.3 Budget override and emergency abort

```
Human Operator                              SQLite Store
      |                                          |
      | [ Human observes run                     |
      |   is approaching $10                     |
      |   budget via GET /.                      |
      |   Decides to increase. ]                 |
      |                                          |
      |-- POST / -------------------------------->|
      |   instruction: "Raise                    |
      |     budget to $25.00.                    |
      |     Reason: narration                    |
      |     is longer than                       |
      |     expected."                           |
      |                                          |
      |-- HumanInstruction --------------------->|
      |   action=                                |
      |     "budget_override"                    |
      |   action_params=                         |
      |     {"new_limit":25.00}                   |
      |                                          |
      |                                          |-- (next activation:
      |                                          |   _budget_exceeded
      |                                          |   reads new limit,
      |                                          |   guard False,
      |                                          |   run continues)
      |                                          |
      | [ Emergency abort: ]                     |
      |                                          |
      |-- POST / -------------------------------->|
      |   instruction: "ABORT                    |
      |     RUN IMMEDIATELY.                     |
      |     Reason: wrong                        |
      |     pipeline started."                   |
      |                                          |
      |-- HumanInstruction --------------------->|
      |   action=                                |
      |     "emergency_abort"                    |
      |                                          |
      |                                          |-- PipelineAborted >|
      |                                          |   reason=           |
      |                                          |     "human_request" |
      |                                          |                     |
      |                                          |-- (all VMs          |
      |                                          |   deallocated via   |
      |                                          |   VMDeallocated)    |
```

The `HumanInstruction` effect carries an `action` field that agents inspect on their next turn. Valid actions are `"budget_override"` (requires `new_limit` float in `action_params`), `"emergency_abort"`, and `"approve_command"` (for bash execution approval). An emergency abort triggers the parser to extract `PipelineAborted`, which agents detect and halt, followed by VM deallocation via the Provisioner's cleanup path (§10.2.5).

| Action Field | Required Params | Response |
|---|---|---|
| `budget_override` | `new_limit: float` | Updates `max_run_budget_usd` in config; budget check re-evaluates |
| `emergency_abort` | `reason: str` | Emits `PipelineAborted`; agents halt; Provisioner deallocates all VMs |
| `approve_command` | `command: str` | Clears pending `ClarificationRequest`; command is re-injected into agent prompt |


---

### 12.5 Startup Sequence

This section specifies the exact order of process startup, the first HTTP request that creates a run, and the initial agent autonomous execution sequence.

#### 12.5.1 Process startup order

| Order | Process | Port | Command | Dependency |
|---|---|---|---|---|
| 1 | SQLite Event Store | — | `EventStore(log_dir="/tmp/documentary-pipeline")` | None |
| 2 | Global State Agent | 8000 | `python global_state_agent.py` | SQLite Event Store |
| 3 | Scenario Agent | 8001 | `python -m agents.scenario` | Global State Agent |
| 4 | Audio Agent | 8002 | `python -m agents.audio` | Global State Agent |
| 5 | Video Agent | 8003 | `python -m agents.video` | Global State Agent |
| 6 | Assembly Agent | 8005 | `python -m agents.assembly` | Global State Agent |
| 7 | Provisioner | 8081 | `python -m provisioner.main` | SQLite Event Store |

Each agent is an independent ASGI process. There is no central coordinator process and no intermediary routing service. Agents discover each other via the `Config` read at import time (§14.1), which contains hardcoded host:port pairs. No service discovery, no health checks, no heartbeat mesh.

#### 12.5.2 Run creation

A run begins with a single HTTP POST from the human operator directly to the Scenario Agent:

A run begins with direct append of startup events to the `events.db` store by the orchestrator/operator script, followed by a raw POST trigger to the Scenario Agent:

1. The operator script appends a `PipelineStarted` effect (with the topic config details) and a `BudgetSet` effect to the `events.db` database.
2. The operator script boots GSA and all agent servers.
3. The operator script posts the raw plain text prompt trigger directly to the Scenario Agent:

```
POST http://localhost:8001/
Content-Type: text/plain

Generate a short 1-minute documentary about Lacan's notion of objet petit a (petit object a).
```

4. The Scenario Agent wakes up, loads the state, runs its model, and begins constructing the script.

No polling. No waiting. If Scenario Agent is not yet listening, the POST fails and is retried.

#### 12.5.3 Initial autonomous execution sequence

The Scenario Agent begins its first poll cycle, queries the Global State Agent via `GET /` for current state, constructs its narrative; the parser extracts `UpdateScript`. The Audio Agent polls independently and sees (port 8002). The parser extracts `QueueJob` (TTS) from Audio Agent output; the pipeline awaits `AudioMeasured`. Once all audio blocks pass tolerance, the Video Agent polls independently and sees (port 8003). The parser extracts `QueueJob` (video generation) from Video Agent output; the pipeline awaits `VideoMeasured`. Once all video blocks pass, the Assembly Agent polls independently and sees (port 8005). The parser extracts `PipelineComplete` from Assembly Agent output and the run is complete.

This is the **happy path**. Any agent may instead produce output from which the parser extracts `ProductionFailure`, halting the run, or the Scenario Agent may produce text from which the parser extracts `UpdateScript`, which rewinds to the Scenario Agent. The autonomous execution sequence is not hardcoded; each agent decides what to do next based on its own state and the effects extracted from its output.

---

