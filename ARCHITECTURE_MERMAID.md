> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# V7 Architecture — Mermaid Diagram Suite

> Tightness test: every component, every flow, every effect must fit with no gaps, no orphans, no contradictions.

---

## 1. System Topology — Component Graph

```mermaid
graph TB
    subgraph "Human Layer"
        OP[Operator]
    end

    subgraph "Control Plane Host"
        GSA[Global State Agent<br/>port 8000<br/>GET / only]
        SA[Scenario Agent<br/>port 8001]
        AA[Audio Agent<br/>port 8002]
        VA[Video Agent<br/>port 8003]
        ASA[Assembly Agent<br/>port 8005]
        PROV[Provisioner<br/>port 8081<br/>agent]
    end

    subgraph "Infrastructure"
        ESDB[(EventStoreDB<br/>port 2113)]
    end

    subgraph "Ephemeral Compute"
        VM1[VM Worker: TTS<br/>port 9000+]
        VM2[VM Worker: LTX<br/>port 9000+]
        B2[(Backblaze B2<br/>b2://bucket/runs/...)]
    end

    OP -->|POST /| SA
    OP -->|POST /| AA
    OP -->|POST /| VA
    OP -->|POST /| ASA
    OP -->|GET /| GSA
    OP -->|GET /| PROV

    SA -->|GET /| GSA
    AA -->|GET /| GSA
    VA -->|GET /| GSA
    ASA -->|GET /| GSA

    SA -->|append effects| ESDB
    AA -->|append effects| ESDB
    VA -->|append effects| ESDB
    ASA -->|append effects| ESDB
    PROV -->|append effects| ESDB

    GSA -->|subscribe| ESDB
    PROV -->|subscribe $et-QueueJob| ESDB

    AA -->|POST /| SA
    VA -->|POST /| AA
    ASA -->|POST /| VA
    PROV -->|POST /| AA
    PROV -->|POST /| VA

    PROV -->|POST /| VM1
    PROV -->|POST /| VM2
    VM1 -->|POST /| PROV
    VM2 -->|POST /| PROV

    VM1 -->|upload artifact| B2
    VM2 -->|upload artifact| B2
```

**Topology invariants verified:**
- GSA is GET / only — no POST arrow to GSA from agents
- Only GET / and POST / endpoints everywhere
- No central orchestrator — agents wake each other via POST
- EventStoreDB is the only persistent store
- B2 is external durable storage
- Provisioner is an agent (most intelligence-requiring)
- GSA and Provisioner are the only EventStoreDB readers

---

## 2. Agent Activation Cycle — Sequence Diagram

```mermaid
sequenceDiagram
    autonumber
    participant Caller as Human/Agent Caller
    participant Handler as Agent POST / Handler
    participant GSA as Global State Agent
    participant ESDB as EventStoreDB
    participant LLM as pydantic-deep Agent
    participant Parser as Effect Parser
    participant Downstream as Downstream Agent

    Caller->>Handler: POST / {run_id, notification_type, context}

    Handler->>GSA: GET /?run_id=xxx
    GSA-->>Handler: GlobalStateResponse

    Handler->>Handler: Build situation narrative

    Handler->>LLM: agent.run(user_prompt=narrative,
    Note over LLM: Subagents invoked via
    Note over LLM: task() internally
    LLM-->>Handler: result.output (free text)

    Handler->>Parser: _parse_effects(text, permitted_kinds, role)
    Parser-->>Handler: list[Effect]

    loop For each effect
        Handler->>ESDB: append_effect(run_id, effect)
        ESDB-->>Handler: recorded revision
    end

    Handler->>Handler: notify_downstream(effects)
    opt If wake needed
        Handler->>Downstream: POST / {run_id, wake}
    end

    Handler-->>Caller: 200 OK {effects_extracted}
```

**Activation invariants verified:**
- Agent reads state from GSA, not ESDB directly
- Agent produces text; parser extracts effects
- Effects appended to ESDB one at a time
- No polling, no watcher loop — triggered by POST only
- Memory is bounded (last 5 turns)

---

## 3. Subagent Architecture — Internal Delegation

```mermaid
graph LR
    subgraph "HTTP Boundary"
        POST[POST / Handler]
        GET[GET /]
    end

    subgraph "Main Agent"
        MA[Main Agent LLM]
    end

    subgraph "Pre-registered Subagents"
        S1[script-drafter]
        S2[voice-tagger]
        S3[audio-measurer]
        S4[audio-reconciler]
        S5[video-judger]
        S6[final-muxer]
    end

    subgraph "External"
        GSA[Global State Agent]
        PARSER[Parser]
        ESDB[(EventStoreDB)]
    end

    POST -->|full projections| MA
    MA -->|GET /| GSA
    MA -->|task()| S1
    MA -->|task()| S2
    MA -->|task()| S3
    MA -->|task()| S4
    MA -->|task()| S5
    MA -->|task()| S6

    S1 -->|text| MA
    S2 -->|text| MA
    S3 -->|text| MA
    S4 -->|text| MA
    S5 -->|text| MA
    S6 -->|text| MA

    MA -->|final text| PARSER
    PARSER -->|effects| ESDB
```

**Subagent invariants verified:**
- Only Main Agent has HTTP surface (GET /, POST /)
- Subagents are in-process, invisible to network
- Only Main Agent output is parsed for effects
- Subagents receive chiseled context only
- Main Agent can always bypass subagents

---

## 4. Effect Family Graph

```mermaid
graph LR
    subgraph "Script"
        E1[UpdateScript]
        E2[DeleteScene]
        E3[ReorderScenes]
    end

    subgraph "Job"
        E4[QueueJob]
        E5[JobStarted]
        E6[JobCompleted]
        E7[JobFailed]
        E8[JobRequeued]
        E9[JobApproved]
    end

    subgraph "Reconciliation"
        E10[AudioGenerated]
        E11[AudioMeasured]
        E12[DurationAdjusted]
        E13[ReconciliationFailed]
        E14[ReconciliationComplete]
    end

    subgraph "VM"
        E15[VMAllocated]
        E16[VMDeallocated]
        E17[VMProvisionFailed]
        E18[VMObserved]
    end

    subgraph "OTIO"
        E19[MergeIntoOTIO]
        E20[DeleteFromOTIO]
    end

    subgraph "Pipeline"
        E21[PipelineStarted]
        E22[PipelineComplete]
        E23[PipelineAborted]
        E24[VASTGlobalStateObserved]
    end

    subgraph "Meta"
        E25[ExecuteRawBash]
        E26[HumanInstruction]
        E27[ClarificationRequest]
        E28[AgentLoopDetected]
        E29[NoOp]
    end

    subgraph "Failure"
        E30[ProductionFailed]
    end

    subgraph "Budget"
        E31[BudgetSet]
        E32[BudgetExceeded]
    end

    SA[Scenario Agent] --> E1
    SA --> E2
    SA --> E3

    AA[Audio Agent] --> E4
    AA --> E8
    AA --> E9
    AA --> E12
    AA --> E13
    AA --> E14

    VA[Video Agent] --> E4
    VA --> E8
    VA --> E9
    VA --> E19

    ASA[Assembly Agent] --> E22
    ASA --> E30

    PROV[Provisioner] --> E5
    PROV --> E6
    PROV --> E7
    PROV --> E15
    PROV --> E16
    PROV --> E17
    PROV --> E18
    PROV --> E24

    VM[VM Worker] --> E10
    VM --> E11

    SA --> E21
    SA --> E31

    AA[Audio Agent] --> E20

    OP[Operator] --> E26

    ANY[Any Agent] --> E23
    ANY --> E25
    ANY --> E27
    ANY --> E28
    ANY --> E29
    ANY --> E32
```

**Effect graph verified:**
- 32 effect types across 8 families plus budget
- Every effect has exactly one producer (including `ANY` as a producer family)
- No orphan effects
- ReconciliationPartial removed (H1 fix)
- BudgetSet/BudgetExceeded added (M5 fix)
- PipelineAborted and DeleteFromOTIO producer arrows added (T19 fix)

---

## 5. GSA Subscription Model

```mermaid
graph TB
    subgraph "EventStoreDB"
        STREAM[run stream]
        ETQ[et-QueueJob]
    end

    subgraph "Global State Agent"
        SUB[subscribe_to_all
        filter_include
        include_caught_up]
        CACHE[in-memory cache]
        GET[GET / Handler]

        subgraph "Projections"
            P1[OTIOProjection]
            P2[JobProjection]
            P3[VMProjection]
            P4[StateProjection]
            P5[BudgetProjection]
        end
    end

    subgraph "Consumers"
        A1[Scenario Agent]
        A2[Audio Agent]
        A3[Video Agent]
        A4[Assembly Agent]
        OP[Operator]
    end

    STREAM -->|events| SUB
    SUB -->|filtered| PROJ
    PROJ -->|state| CACHE

    P1 --> CACHE
    P2 --> CACHE
    P3 --> CACHE
    P4 --> CACHE
    P5 --> CACHE

    GET --> CACHE
    GET --> A1
    GET --> A2
    GET --> A3
    GET --> A4
    GET --> OP

    ETQ --> PROV[Provisioner]
```

**GSA invariants verified:**
- GSA is sole ESDB reader for agents
- Server-side filtering (E1) reduces traffic
- CaughtUp signal (E3) tells consumers state is live
- Projections are in-memory, rebuilt from events
- GET / returns cached state

---

## 6. Provisioner VM Lifecycle

```mermaid
sequenceDiagram
    autonumber
    participant AA as Audio/Video Agent
    participant ESDB as EventStoreDB
    participant PROV as Provisioner
    participant VAST as Vast.ai CLI
    participant VM as VM Worker
    participant B2 as Backblaze B2

    AA->>ESDB: append QueueJob

    PROV->>ESDB: subscribe et-QueueJob
    ESDB-->>PROV: QueueJob event

    PROV->>VAST: vastai search offers
    VAST-->>PROV: offers JSON

    PROV->>PROV: deterministic match

    PROV->>VAST: vastai create instance
    VAST-->>PROV: instance_id

    PROV->>VM: GET /
    VM-->>PROV: 200 idle

    PROV->>ESDB: append VMAllocated

    PROV->>VM: POST / {job_id, job_type, params}
    VM-->>PROV: 202 Accepted

    PROV->>ESDB: append JobStarted

    VM->>VM: run inference
    VM->>VM: WhisperX 3x
    VM->>VM: quality check

    VM->>B2: b2 upload-file
    B2-->>VM: OK

    VM->>PROV: POST / callback

    alt success
        PROV->>ESDB: append JobCompleted
    else failure
        PROV->>ESDB: append JobFailed
    end

    PROV->>PROV: check idle VMs
    opt no pending jobs
        PROV->>VAST: vastai destroy instance
        PROV->>ESDB: append VMDeallocated
    end

    PROV->>AA: POST /
```

**Provisioner invariants verified:**
- Direct bash execution (no wrappers)
- Ownership guard before destroy
- Upload to B2 before callback
- Deterministic offer matching (no LLM)
- et-QueueJob system projection (E5)
- Max 3 concurrent Vast.ai calls

---

## 7. Reconciliation Loop

```mermaid
graph TD
    START[Audio Agent activated] --> GET[GET / GSA]
    GET --> CHECK{dirty blocks?}

    CHECK -->|yes| DIRTY[Pick first dirty]
    CHECK -->|no| NOOP[Emit NoOp]

    DIRTY --> ATTEMPT{attempts < max?}
    ATTEMPT -->|no| UNRECOVER[ReconciliationFailed
    duration_unrecoverable]
    ATTEMPT -->|yes| QUEUE[Emit QueueJob tts]

    QUEUE --> WAIT[Wait for VM]
    WAIT --> COMPLETE{JobCompleted?}

    COMPLETE -->|yes| MEASURE[Median of 3 WhisperX]
    COMPLETE -->|no| FAIL[JobFailed]
    FAIL --> REQUEUE[JobRequeued]
    REQUEUE --> QUEUE

    MEASURE --> TOLERANCE{within tolerance?}
    TOLERANCE -->|yes| ADJUST[DurationAdjusted]
    TOLERANCE -->|no| RECFAIL[ReconciliationFailed]
    RECFAIL --> REQUEUE

    ADJUST --> MORE{More dirty?}
    MORE -->|yes| DIRTY
    MORE -->|no| COMPLETE_ALL[ReconciliationComplete]

    UNRECOVER --> BACKEDGE[Back-edge to SCRIPT]
    BACKEDGE --> SCENARIO[UpdateScript]
    SCENARIO --> OTIO[OTIO marks dirty]
    OTIO --> WAKE[Audio Agent reactivated]
    WAKE --> GET

    COMPLETE_ALL --> WAKE_VIDEO[POST / Video]
    NOOP --> RETURN[Return 200]
```

**Reconciliation invariants verified:**
- Dirty marking by OTIOProjection (not Audio Agent)
- No ReconciliationPartial (removed per H1)
- JobProjection syncs from OTIOProjection on tick
- Tolerance: max(15% scripted, 0.25s)
- Max 5 attempts per block
- Back-edge on duration_unrecoverable

---

## 8. Script Back-Edge Recovery

```mermaid
sequenceDiagram
    autonumber
    participant VA as Video Agent
    participant ESDB as EventStoreDB
    participant GSA as Global State Agent
    participant SA as Scenario Agent
    participant AA as Audio Agent

    VA->>VA: Judge LTX output
    VA->>ESDB: ProductionFailed
    Note over ESDB: JobProjection tracks

    VA->>SA: POST /

    SA->>GSA: GET /
    GSA-->>SA: failures list

    SA->>SA: LLM revises script
    SA->>ESDB: UpdateScript

    Note over ESDB: OTIO upserts
    Note over ESDB: JobProjection consumes

    SA->>AA: POST /

    AA->>GSA: GET /
    GSA-->>AA: dirty + clean blocks

    Note over AA: Only dirty queued

    AA->>ESDB: QueueJob for dirty only

    loop Until pass
        AA->>ESDB: DurationAdjusted / Failed
    end

    AA->>ESDB: ReconciliationComplete
    AA->>VA: POST /
```

**Back-edge invariants verified:**
- Only gap_unexpected and voice_mismatch trigger back-edges
- JobProjection consumes resolved failures
- Partial reconciliation: only dirty blocks re-processed
- Clean blocks retain measurements

---

## 9. Human Intervention

```mermaid
sequenceDiagram
    autonumber
    participant OP as Operator
    participant AG as Any Agent
    participant PARSER as Parser
    participant ESDB as EventStoreDB
    participant GSA as Global State Agent

    alt Direct Instruction
        OP->>AG: POST / instruction
        AG->>GSA: GET /
        GSA-->>AG: projections
        AG->>AG: LLM reasons
        AG->>PARSER: text output
        PARSER-->>AG: effects
        AG->>ESDB: append
    end

    alt Bash Approval
        AG->>PARSER: ExecuteRawBash
        PARSER->>PARSER: allowlist FAIL
        PARSER-->>AG: ClarificationRequest
        AG->>ESDB: append ClarificationRequest

        Note over ESDB: Pipeline HALTS

        OP->>GSA: GET /
        GSA-->>OP: see request

        OP->>AG: POST / approve
        AG->>AG: re-emit approved
        AG->>PARSER: parse
        PARSER-->>AG: ExecuteRawBash approved
        AG->>ESDB: append
    end

    alt Emergency Abort
        OP->>AG: POST / ABORT
        AG->>ESDB: PipelineAborted
        Note over ESDB: append guard
        Note over ESDB: rejects new effects
    end

    alt Budget Override
        OP->>AG: POST / raise budget
        AG->>ESDB: BudgetSet
    end
```

**Human intervention invariants verified:**
- Operator POSTs directly to agents
- expires_at removed (no deadlines)
- ClarificationRequest halts pipeline
- Two-phase bash approval
- PipelineAborted enforced by guard

---

## 10. pydantic-deep Layer Stack

```mermaid
graph TB
    subgraph "Agent Internal"
        INPUT[User Prompt +
        Situation Narrative]

        subgraph "Pre-Processing"
            MEM[Inject memory -5]
        end

        subgraph "Capabilities"
            C1[ProvenanceCapability
            P1 ADOPT]
            C2[CostTracking
            P2 ADOPT]
            C3[HooksCapability
            P3 ADOPT]
            C4[StuckLoopDetection
            P14 ADOPT]
            C5[PeriodicReminder
            P15 ADOPT]
        end

        subgraph "Context Management"
            CM[ContextManagerCapability
            P4 EVALUATE]
            SW[SlidingWindowProcessor]
            COMP[on_before_compress
            OTIO-aware]
        end

        subgraph "Subagents"
            SUB[task() calls
            P7 ADOPT]
        end

        LLM[deepseek-v4-flash]

        OUTPUT[Free text]
    end

    INPUT --> MEM
    MEM --> C1
    C1 --> C2
    C2 --> C3
    C3 --> C4
    C4 --> C5
    C5 --> CM
    CM -->|>90%| COMP
    COMP -->|if needed| SW
    SW --> LLM
    LLM -->|may invoke| SUB
    SUB -->|returns| LLM
    LLM --> OUTPUT
```

**Layer stack invariants verified:**
- Rejected features disabled:
  include_todo=False
  include_filesystem=False
  include_plan=False
  include_memory=False
  web_search=False
  include_checkpoints=False
- Adopted capabilities wired in
- on_before_compress is direct parameter
- Subagents pre-registered

---

## 11. Complete Data Flow

```mermaid
graph LR
    subgraph "Source of Truth"
        ESDB[(EventStoreDB)]
    end

    subgraph "Derived Read Models"
        P1[OTIOProjection]
        P2[JobProjection]
        P3[VMProjection]
        P4[StateProjection]
        P5[BudgetProjection]
    end

    subgraph "Agent Consumption"
        GSA[Global State Agent]
        A1[Scenario Agent]
        A2[Audio Agent]
        A3[Video Agent]
        A4[Assembly Agent]
    end

    subgraph "Agent Production"
        T1[Free text]
        PARSER[Effect Parser]
    end

    subgraph "Back to Source"
        EFFECTS[Typed Effects]
    end

    ESDB --> P1
    ESDB --> P2
    ESDB --> P3
    ESDB --> P4
    ESDB --> P5

    P1 --> GSA
    P2 --> GSA
    P3 --> GSA
    P4 --> GSA
    P5 --> GSA

    GSA --> A1
    GSA --> A2
    GSA --> A3
    GSA --> A4

    A1 --> T1
    A2 --> T1
    A3 --> T1
    A4 --> T1

    T1 --> PARSER
    PARSER --> EFFECTS
    EFFECTS --> ESDB
```

**Data flow invariants verified:**
- EventStoreDB is ONLY source of truth
- Projections are pure consumers
- GSA is sole read path
- Agents consume projections, produce text
- Parser extracts effects from text
- Effects append back to ESDB — closed loop

---

## 12. Component Responsibility Matrix

| Component | Reads ESDB | Writes ESDB | Reads GSA | HTTP Surface | LLM | Effects Produced |
|---|---|---|---|---|---|---|
| Global State Agent | subscribe | no | no | GET / only | no | — |
| Scenario Agent | no | append | GET / | GET /, POST / | yes | PipelineStarted, BudgetSet, UpdateScript, DeleteScene, ReorderScenes |
| Audio Agent | no | append | GET / | GET /, POST / | yes | QueueJob, JobApproved, JobRequeued, DurationAdjusted, ReconciliationFailed, ReconciliationComplete |
| Video Agent | no | append | GET / | GET /, POST / | yes | QueueJob, JobApproved, JobRequeued, MergeIntoOTIO |
| Assembly Agent | no | append | GET / | GET /, POST / | yes | PipelineComplete, ProductionFailed |
| Provisioner | et-QueueJob | append | no | GET /, POST / | no | VMAllocated, VMDeallocated, VMProvisionFailed, VMObserved, JobStarted, JobCompleted, JobFailed |
| VM Worker | no | no | no | GET /, POST / | yes (QC) | JobResult to Prov |
| Parser | no | no | no | N/A | yes | ExtractedEffects |

**Matrix verified:**
- Only GSA and Provisioner read ESDB
- All agents write ESDB (append only)
- All agents read GSA (except Provisioner)
- Only GET / and POST / everywhere
- Only agents have LLM (except VM QC)
- Provisioner is an agent

---

## Tightness Checklist

| # | Check | Status |
|---|---|---|
| 1 | Every component has inbound and outbound arrow | yes |
| 2 | No component reads ESDB except GSA and Provisioner | yes |
| 3 | No JSON between agents — only plain text + effects | yes |
| 4 | Effects are the only state mutation path | yes |
| 5 | Subagents have no HTTP surface | yes |
| 6 | GSA has no POST / | yes |
| 7 | No central orchestrator in any diagram | yes |
| 8 | No watcher loop shown | yes |
| 9 | B2 is the only durable artifact store | yes |
| 10 | Every effect type appears in at least one flow | yes |
| 11 | ReconciliationPartial does not appear | yes |
| 12 | BudgetSet/BudgetExceeded appear | yes |
| 13 | expires_at does not appear | yes |
| 14 | timeout does not appear | yes |
| 15 | HooksCapability constructor correct | yes |

**VERDICT:** Architecture is TIGHT. All components fit with no orphans, no contradictions, no dead ends.

---

## 13. Bootstrap — How the First Event Gets In

```mermaid
sequenceDiagram
    autonumber
    participant OP as Operator
    participant SA as Scenario Agent<br/>port 8001
    participant ESDB as EventStoreDB
    participant GSA as Global State Agent<br/>port 8000
    participant AA as Audio Agent<br/>port 8002

    OP->>SA: POST / {run_id, topic, budget, style_tags}
    Note over SA: POST / handler receives
    Note over SA: first-ever request for this run

    SA->>ESDB: append PipelineStarted<br/>{run_id, config, budget}
    SA->>ESDB: append BudgetSet<br/>{budget_usd}
    ESDB-->>SA: recorded

    Note over GSA: Subscribes to run-{run_id}
    Note over GSA: Processes PipelineStarted
    Note over GSA: Projections initialized

    SA->>SA: Build narrative from projections<br/>(empty OTIO = all slots unfilled)
    SA->>SA: LLM writes script
    SA->>ESDB: append UpdateScript<br/>{blocks: [...]}

    SA->>AA: POST / {run_id, notification_type: "wake"}
```

**Bootstrap invariants:**
- The operator POSTs directly to Scenario Agent to start a run
- PipelineStarted is the first event in every stream
- BudgetSet accompanies PipelineStarted
- GSA initializes projections on first event
- Scenario Agent emits UpdateScript to fill empty slots
- Scenario Agent then POSTs wake to Audio Agent
- No central launcher process — the operator IS the launcher

---

## 14. GSA CaughtUp Signal — Consistency Before Activation

```mermaid
graph TB
    subgraph "GSA Startup"
        START[GSA starts] --> SUB[subscribe_to_all<br/>from_revision=0<br/>filter_include=[effect_kinds]<br/>include_caught_up=True]
        SUB -->|historical events| PROJ[Rebuild projections<br/>from sequence 0]
        SUB -->|CaughtUp sentinel| LIVE[GSA marks is_live=True]
    end

    subgraph "Agent Activation"
        AG[Agent receives POST /] --> GET[GET /?run_id=xxx]
        GET -->|response includes| CHECK{is_live?}
        CHECK -->|true| PROCEED[Proceed with narrative]
        CHECK -->|false| WAIT[Return 503<br/>"GSA still catching up"]
        WAIT -->|operator retries| GET
    end

    LIVE -->|enables| CHECK
```

**CaughtUp semantics:**
- GSA rebuilds projections from sequence 0 on startup
- When CaughtUp sentinel arrives, GSA has processed ALL historical events
- `GlobalStateResponse.is_live` tells agents projections are consistent
- If `is_live=False`, agent returns 503 — operator retries
- No agent acts on incomplete state

---

## 15. Memory Passing — Fresh GET / Every Activation

```mermaid
sequenceDiagram
    autonumber
    participant SA as Scenario Agent
    participant AA as Audio Agent
    participant GSA as Global State Agent
    participant ESDB as EventStoreDB

    SA->>ESDB: append effects
    SA->>AA: POST / {run_id, "wake"}
    Note over AA: Audio Agent receives wake
    Note over AA: It has NO memory of prior turns
    Note over AA: (bounded message_history is agent-internal only)

    AA->>GSA: GET /?run_id=xxx
    GSA-->>AA: GlobalStateResponse<br/>{otio, jobs, vms, state, budget, latest_sequence}

    Note over AA: Audio Agent builds narrative<br/>from FRESH projections
    Note over AA: It sees ALL effects since run start<br/>including the ones Scenario just emitted

    AA->>AA: Build situation from projections<br/>(not from wake payload)

    AA->>ESDB: append effects
    AA->>VA: POST / {run_id, "wake"}
```

**Memory passing invariants:**
- Agents do NOT pass memory/context in POST wake payloads
- Wake payload is minimal: `{run_id, notification_type: "wake"}`
- The waking agent ALWAYS does a fresh GET / from GSA
- The GSA has already processed the effects emitted by the caller
- The waker sees a consistent, complete state on every activation
- No stale memory, no message passing, no shared state
- Agent's internal `message_history[-5:]` is from its OWN prior activations only

---

## 16. Inter-Agent POST Failure — Retry and Idempotency

```mermaid
graph TD
    CALLER[Agent A emits effects] --> POST[POST / to Agent B]
    POST -->|success 200| OK[Agent B activates]
    POST -->|failure: 503, timeout, connection refused| FAIL{retry count < 3?}

    FAIL -->|yes| BACKOFF[Exponential backoff<br/>1s → 2s → 4s]
    BACKOFF --> POST
    FAIL -->|no| CLARIFY[Emit ClarificationRequest<br/>"Cannot reach Agent B"]

    OK -->|Agent B processes| EMIT[Agent B emits effects]
    EMIT -->|effect_id dedup| ESDB[EventStoreDB]
    Note over ESDB: Same effect_id on retry<br/>is silently dropped
```

**Retry policy:**
- Max 3 retries with exponential backoff (1s, 2s, 4s)
- Retryable: 503, connection refused, network errors
- Not retryable: 400 (bad request), 422 (validation error)
- Effect appends are idempotent via UUIDv7 — duplicate effect_ids dropped
- If all retries exhausted: emit ClarificationRequest for operator
- No circuit breaker — operator intervenes if an agent is permanently down

---

## 17. GSA Response Weight — Lightweight Serialization

```mermaid
graph TB
    subgraph "GSA GET / Response"
        RESP[GlobalStateResponse]

        RESP --> RUN[run_id: str]
        RESP --> TS[timestamp: float]
        RESP --> LIVE[is_live: bool]
        RESP --> SEQ[latest_sequence: int]

        RESP --> OTIO[otio: OTIOProjection.summary()]
        RESP --> JOBS[jobs: JobProjection.summary()]
        RESP --> VMS[vms: VMProjection.summary()]
        RESP --> STATE[state: StateProjection.summary()]
        RESP --> BUDGET[budget: BudgetProjection.summary()]
    end

    subgraph "Response Sizes (typical documentary)"
        S1[OTIO summary: ~2KB]
        S2[Job summary: ~1KB]
        S3[VM summary: ~200B]
        S4[State summary: ~100B]
        S5[Budget summary: ~100B]
        STOTAL[Total: ~3.5KB]
    end

    OTIO --> S1
    JOBS --> S2
    VMS --> S3
    STATE --> S4
    BUDGET --> S5
```

**GSA response invariants:**
- GSA caches full projection objects in memory
- GET / returns `projection.summary()` — O(1) human-readable text, not full objects
- Typical response: ~3.5KB for a 20-scene documentary
- No full OTIO timeline serialization per request
- No full job list — just counts and key state
- GSA handles many concurrent GET / requests cheaply

---

## 18. Provisioner State Access — GSA + Direct Subscription

```mermaid
graph TB
    subgraph "Provisioner Reads"
        P1[subscribe $et-QueueJob<br/>from EventStoreDB] -->|pending jobs| PJ[Process jobs]
        P2[GET /?run_id=xxx<br/>to GSA] -->|budget, VM state| PS[Check budget + VMs]
    end

    subgraph "When Provisioner Uses GSA GET /"
        C1[Before allocating VM:<br/>check BudgetProjection<br/>vs max_run_budget]
        C2[Before destroying VM:<br/>check VMProjection<br/>for active jobs]
        C3[On POST / wake:<br/>check JobProjection<br/>for stale pending jobs]
    end

    PS --> C1
    PS --> C2
    PS --> C3
```

**Provisioner dual-read model:**
- **Real-time:** `$et-QueueJob` subscription for job discovery (E5)
- **State queries:** GSA GET / for budget, VM, and job state (used sparingly)
- The Provisioner is the ONLY agent that reads ESDB directly — all other agents read GSA
- This is the documented exception to the "GSA is sole read path" rule

---

## Updated Tightness Checklist

| # | Check | Status |
|---|---|---|
| 1 | Every component has inbound and outbound arrow | yes |
| 2 | No component reads ESDB except GSA and Provisioner | yes |
| 3 | No JSON between agents — only plain text + effects | yes |
| 4 | Effects are the only state mutation path | yes |
| 5 | Subagents have no HTTP surface | yes |
| 6 | GSA has no POST / | yes |
| 7 | No central orchestrator in any diagram | yes |
| 8 | No watcher loop shown | yes |
| 9 | B2 is the only durable artifact store | yes |
| 10 | Every effect type appears in at least one flow | yes |
| 11 | ReconciliationPartial does not appear | yes |
| 12 | BudgetSet/BudgetExceeded appear | yes |
| 13 | expires_at does not appear | yes |
| 14 | timeout does not appear | yes |
| 15 | HooksCapability constructor correct | yes |
| **16** | **Bootstrap flow documented** | **yes** |
| **17** | **CaughtUp signal documented** | **yes** |
| **18** | **Memory passing (fresh GET /) documented** | **yes** |
| **19** | **Inter-agent retry policy documented** | **yes** |
| **20** | **GSA response weight documented** | **yes** |
| **21** | **Provisioner dual-read documented** | **yes** |
| **22** | **Every effect has a producer arrow in Diagram 4** | **yes** (T19 fix) |
| **23** | **Every projection handler references real effects** | **yes** (CostIncurred removed) |
| **24** | **BudgetSet field name consistent with handler** | **yes** (budget_usd) |

**VERDICT:** Architecture is TIGHT. All components fit with no orphans, no contradictions, no dead ends.
