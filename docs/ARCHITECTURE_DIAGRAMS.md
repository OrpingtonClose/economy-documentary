> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Documentary Pipeline — Architecture Flow Diagrams

This document is the companion to [ARCHITECTURE.md](./ARCHITECTURE.md). It specifies the correct (intended) shape of the pipeline as a set of eleven Mermaid diagrams. Each diagram carries its rationale as inline `%%` comments so the specification travels with the diagram source.

GitHub renders Mermaid fenced blocks natively — open any section to see the rendered flowchart.


## 1. Top-level flow (entry → stages → final render)

The OTIO shown here is the authoritative timeline, crystallised only after narration reconciliation (see diagram 2). Everything before that is draft.

```mermaid
%% Top-level flow: prompt -> stages -> final render.
%% The OTIO shown here is the AUTHORITATIVE timeline. It is NOT born here;
%% it crystallises only at the end of Stage Two after narration reconciliation
%% (see diagram 2). Everything before that is DRAFT.
%% Once crystallised, the authoritative OTIO is THE LAW above all downstream stages.
%% Parallelism lives inside each stage; the stages themselves are nominally sequential.
%% CRITICAL INVARIANT -- universal back-edges:
%% the master pipeline must assume that ANY moment of the flow may return
%% to ANY earlier block. Every stage carries the Preference Ledger revision
%% it was derived against. Only L4 (human intervention, see diagram 2a)
%% writes to the ledger -- through a Preference Interpreter that parses
%% each free-form directive into one or more scoped preference records.
%% L0-L3 resolve inside the ladder and never touch the ledger.
%% When the ledger gains new records, the consistency checker detects the
%% new revision; the impact analyzer scope-matches the new records against
%% existing artifacts and may invalidate artifacts belonging to any earlier
%% stage, triggering surgical re-manifestation. There is no point past
%% which the pipeline is immune to upstream rework.
flowchart TD
    U["User submits prompt"] --> UI["Next.js dashboard"]
    UI -->|AG-UI SSE| API["FastAPI server"]
    API --> MA["Master SequentialAgent"]

    subgraph MASTER["Master pipeline"]
        direction TB
        S1["Stage One — Scenario Director<br/>EvaluatorOptimizer loop"]
        S2["Stage Two — Narration Reconciliation<br/>TTS + WhisperX + slip reconciliation<br/>see diagram 2"]
        S3["Stage Three — Visual Director<br/>LoopAgent x 3"]
        S4["Stage Four — Production Supervisor<br/>GPU orchestration"]
        S5["Stage Five — Assembler<br/>ffmpeg"]
        S1 --> G1{{"Gate One<br/>scenario"}}
        G1 --> S2
        S2 --> G2{{"Gate Two<br/>narration + authoritative OTIO"}}
        G2 --> S3
        S3 --> G3{{"Gate Three<br/>visual plan"}}
        G3 --> S4
        S4 --> G4{{"Gate Four<br/>clips"}}
        G4 --> S5
    end

    MA --> MASTER
    MASTER --> MP4["Final MP4 in B2"]

    BB[("Blackboard<br/>shared state")]
    OTIO[("Authoritative OTIO<br/>THE LAW<br/>born at end of Stage Two")]
    TG["Timeline Guardian<br/>after each stage"]
    PROMPT[("Preference Ledger<br/>scoped records, intent SSOT<br/>see diagram 2a")]

    S1 -.draft scenario.-> BB
    S2 -.writes.-> BB
    S3 -.writes.-> BB
    S4 -.writes.-> BB
    S2 ==crystallises==> OTIO
    S3 -.binds to.-> OTIO
    S4 -.binds to.-> OTIO
    S5 ==reads as law==> OTIO
    MASTER -.validated by.-> TG
    MASTER -.derived from.-> PROMPT

    %% Universal back-edges: any stage may drive re-manifestation
    %% of any earlier stage via prompt revision (diagram 2a).
    S2 -.re-manifest.-> S1
    S3 -.re-manifest.-> S2
    S3 -.re-manifest.-> S1
    S4 -.re-manifest.-> S3
    S4 -.re-manifest.-> S2
    S4 -.re-manifest.-> S1
    S5 -.re-manifest.-> S4
    S5 -.re-manifest.-> S3
    S5 -.re-manifest.-> S2
    S5 -.re-manifest.-> S1
```


## 2. Narration reconciliation — how the authoritative OTIO is born

Scripted durations never survive contact with TTS. WhisperX measures what actually came out. The pipeline reconciles the slip through an L0–L4 ladder calibrated for cheap, fast audio — low tiers are deliberately permissive with wide retry budgets because reconciliation is the mechanism by which the OTIO is born, not a failure mode. Every block must also pass stylistic QA invariants (uniform LUFS across narration, voice continuity between adjacent blocks, character voice consistency, no clicks, no truncated plosives) before it exits the ladder; stylistic invariants can be overridden only by an explicit Preference Ledger record for a specific scope. The OTIO that emerges at the end is THE LAW for every downstream stage. Trimming, stretching, and frozen frames remain forbidden by the Media Immutability Invariant — retries are always regenerate, never edit.

```mermaid
%% Narration reconciliation -- how the authoritative OTIO is born.
%% Scripted durations never survive contact with TTS. WhisperX measures what
%% actually came out. The pipeline reconciles the slip through an L0-L4
%% ladder that is PERMISSIVE at the low tiers because audio reconciliation
%% IS the process by which the authoritative OTIO is born; escalation is
%% normal operating mode, not an exception. Abandoning L0 early would
%% starve the OTIO of the narration it needs.
%% Audio retry budgets (wide at bottom, narrow at top):
%%   L0 WIDE -- many attempts per block, varying seed, reference sample,
%%     breath phrasing, micro-script rewrites. Cheap and fast.
%%   L1 GENEROUS -- multi-shot audio-understanding consultation; converse
%%     across many models and perturbations.
%%   L2 NARROW-MULTI -- multi-shot, bounded exploration of voices and
%%     TTS providers within the speaker-role frame.
%%   L3 BOUNDED -- small budget; coordination cost is real.
%%   L4 HUMAN -- single decision, possibly re-entered after a directive.
%% Stylistic QA invariants (enforced on EVERY output regardless of tier):
%%   - uniform loudness target (LUFS) across all narration blocks
%%   - voice continuity: no jarring register shifts between adjacent blocks
%%     of the same speaker role
%%   - character voice consistency: same speaker role maps to same voice
%%     identity across the whole film
%%   - peak-limiter compliance; no clicks, no truncated plosives, no
%%     hiss-floor discontinuities
%% A Preference Ledger record with scope covering a block can DELIBERATELY
%% override a stylistic invariant for that block only (e.g. "Cassandra
%% louder in scene 3"); invariants are checked against the ledger, not
%% hard-coded.
%% A block that passes timing but fails a stylistic invariant does NOT
%% exit L0 as a pass; it re-enters the ladder with the invariant violation
%% as the failure signal.
%% Media Immutability Invariant applies: no trimming, no stretching, no
%% frozen frames. Retries are always REGENERATE, never edit.
flowchart TD
    IN["Approved scenario<br/>draft voice blocks<br/>target durations"]
    IN --> DRAFT[("Draft OTIO<br/>placeholder slots")]
    DRAFT --> TTS["TTS synthesis"]
    TTS --> WAV["Narration WAV<br/>newly-generated, immutable"]
    WAV --> WX["WhisperX<br/>word-level alignment"]
    WX --> MEAS["Measured durations<br/>per voice block"]

    MEAS --> TIMING{"Timing vs<br/>scenario pacing"}
    TIMING -->|within tolerance| STYLE["Stylistic QA invariants<br/>uniform LUFS across all narration<br/>voice continuity adjacent blocks<br/>character voice consistency<br/>no clicks, no truncated plosives<br/>checked against Preference Ledger<br/>for scoped deliberate overrides"]
    TIMING -->|slip detected| LAD["Reconciliation ladder"]
    STYLE -->|pass| CRYS["Crystallise authoritative OTIO"]
    STYLE -->|fail| LAD

    subgraph LADDER["Narration reconciliation ladder (permissive)"]
        direction TB
        L0["L0 FIX — WIDE retry budget<br/>many attempts per block<br/>reseed TTS, alternate reference sample<br/>rephrase shorter or longer<br/>insert or remove breath phrase<br/>split or merge blocks<br/>adjust inter-block silence within pacing slack"]
        L1["L1 RETRY — GENEROUS budget<br/>audio-understanding consultation<br/>converse across many audio models<br/>diagnose what changed vs target<br/>multi-shot bounded param perturbations<br/>script and model family unchanged"]
        L2["L2 CREATIVE — NARROW but MULTI-SHOT<br/>bounded exploration inside<br/>Scenario + draft OTIO frame<br/>alternative voices, alternative TTS providers<br/>external MCPs and LLM APIs as consultants<br/>the frame is sacred"]
        L3["L3 COLLABORATIVE — BOUNDED<br/>frame becomes negotiable<br/>Scenario, Visual, Audio, Timeline Guardian<br/>upstream artifacts may be altered<br/>no media-gen model swaps (too radical)<br/>coordination cost is real, so budget is small"]
        L4["L4 HUMAN — single decision<br/>re-enterable after directive<br/>see diagram 2a"]
        L0 -->|budget exhausted| L1
        L1 -->|budget exhausted| L2
        L2 -->|budget exhausted| L3
        L3 -->|budget exhausted| L4
    end

    LAD --> L0
    L0 -->|resolved| TTS2["Re-run TTS<br/>only on changed blocks"]
    L1 -->|resolved| TTS2
    L2 -->|resolved| TTS2
    L3 -->|resolved| TTS2
    TTS2 --> WX

    CRYS --> LAW[("Authoritative OTIO<br/>THE LAW<br/>immutable baseline for all downstream work")]
    LAW --> OUT["Stage Two complete<br/>→ Gate Two"]
```


## 2a. Preference Ledger as intent SSOT and pipeline re-manifestation

The pipeline is a running manifestation of a scoped Preference Ledger, not of a flat prompt. Each entry is a preference record with scope, polarity, subject, content, and origin. The original user prompt is parsed into records at run start and every L4 directive is parsed by a Preference Interpreter into one or more additional records. Stages do not read a prompt; they assemble a virtual brief by collecting the records whose scope matches, sorted by specificity and recency. Scope is what makes re-manifestation surgical: a narrow preference invalidates narrow artifacts, a global preference propagates widely. L0–L3 never touch the ledger; only L4 does.

```mermaid
%% Preference Ledger as intent SSOT and pipeline re-manifestation.
%% The pipeline is a running MANIFESTATION of a scoped Preference Ledger,
%% NOT a flat prompt. Each ledger entry is a preference record with five
%% fields: scope, polarity, subject, content, origin.
%% Scope is hierarchical: global, stage, scene, voice block, artifact type,
%% element. Polarity: prefer, avoid, require, forbid. Subject: tone, voice,
%% pacing, visual style, structure, duration, music. Content: free-form.
%% Origin: which event added it (R0 seed, L4 directive #N).
%% The original user prompt is parsed into records at run start -- there is
%% no special-cased "prompt" artifact. R0 is simply the first batch.
%% L4 is the ONLY trigger that writes to the ledger. A Preference Interpreter
%% parses each human directive into one or more scoped records; one directive
%% may yield several records (a scene-scoped fix plus a global preference).
%% L0-L3 never write to the ledger.
%% Stages never read a "prompt". They assemble a virtual brief by collecting
%% ledger records whose scope matches their current work, sorted by
%% specificity (global < stage < scene < block < element) and recency.
%% The consistency checker fires on every stage boundary, gate poll, and
%% tool call, comparing the ledger revision the stage was derived against
%% to the current ledger revision.
%% Re-manifestation is surgical because scope is first-class: a narrow
%% record invalidates narrow artifacts, a global record propagates widely.
%% Conflict resolution: more specific scope wins; more recent within scope
%% wins; hard polarities dominate soft. Two hard records that contradict
%% each other are an unresolvable conflict and re-escalate to the human.
flowchart TD
    subgraph LEDGER["Preference Ledger (intent SSOT)"]
        direction TB
        R0[("R0 records<br/>parsed from original brief<br/>global tone, length, style")]
        R1[("Scene-scoped record<br/>rewrite scene 3 tighter")]
        R2[("Speaker-scoped record<br/>avoid Cassandra lower register")]
        R3[("Global record<br/>prefer shorter narration")]
        R0 -.append-only.-> R1
        R1 -.append-only.-> R2
        R2 -.append-only.-> R3
    end

    L4["L4 HUMAN directive<br/>free-form natural language"]
    L03["L0 FIX / L1 RETRY / L2 CREATIVE / L3 COLLABORATIVE<br/>resolve inside the ladder<br/>DO NOT write to the ledger"]

    PI["Preference Interpreter agent<br/>parses directive into scoped records<br/>infers scope, polarity, subject<br/>may emit 1..N records per directive"]

    L4 --> PI
    PI --> WRITE["Append records to ledger<br/>bump ledger revision"]
    WRITE --> LEDGER
    L03 -.no-op on ledger.-> NOOP["no ledger change"]

    SEED["Run start<br/>parse original brief"]
    SEED --> PI

    LEDGER --> ASM["Context assembly<br/>per stage, per scope<br/>filter by matching scope<br/>sort by specificity then recency<br/>apply conflict resolution"]
    ASM --> BRIEF["Virtual brief for this stage<br/>merged, scope-filtered view"]
    BRIEF --> STAGE["Stage runs against virtual brief"]

    STAGE --> CHECK["Consistency checker<br/>on every stage boundary,<br/>gate poll, and tool call"]
    CHECK --> CMP{"Stage ledger revision<br/>vs current ledger revision"}
    CMP -->|match| CONTINUE["Stage continues normally"]
    CMP -->|drift| IMP["Impact analyzer<br/>walks the ledger diff<br/>scope-matches new records<br/>against existing artifacts"]

    IMP --> PLAN["Re-manifestation planner<br/>minimal DAG of regeneration tasks<br/>scope determines breadth<br/>preserves anything unmatched"]
    PLAN --> VAL["Plan validator<br/>check vs all pipeline invariants<br/>Media Immutability, OTIO, budget,<br/>single-writer, stage-boundary,<br/>no-conflicting-hard-records"]
    VAL -->|violation| PLAN
    VAL -->|unresolvable conflict| RESC["Re-escalate to human"]
    VAL -->|valid| EXEC["Executor re-enters impacted stages<br/>each runs its own L0–L4 ladder<br/>against the new virtual brief"]

    EXEC --> BG["Budget guard"]
    BG -->|over budget| RESC
    EXEC -->|sub-task exhausts to L4| RESC
    RESC --> L4

    EXEC -->|settled| RESUME["Resume stage at new revision"]
    RESUME --> CONTINUE
```


## 3. Production stage (clip generation detail)

Video is expensive. Unlike the audio ladder (diagram 2), the video content ladder is strict: **one failure per level only**. Each tier gets exactly one attempt; a failure escalates immediately to the next tier. A second failure at the same tier is not permitted — escalating gives the next-higher-authority agent a genuinely different strategy space, which is more likely to help than another roll of the same dice. Infra failures (diagram 8) are classified separately and consume the infra ladder's budget, not this one.

```mermaid
%% Production stage -- clip generation detail with STRICT content ladder.
%% Runs against the AUTHORITATIVE OTIO (already crystallised in Stage Two).
%% OTIO slot durations are LAW; no downstream regeneration can change them.
%% ProductionOrchestrator runs a three-phase loop: plan assigns clips to
%% workers, execute dispatches in parallel, replan reshuffles on failure.
%% Every returned clip flows through Bearnaise two-pass QA (structural +
%% semantic) PLUS classifier (diagram 8) that splits content vs infra.
%% The batch is not blocked on individual recoveries until replan iterates.
%% ASYMMETRY WITH AUDIO LADDER (diagram 2):
%% Video generation is expensive (LTX-2.3 on high-VRAM GPUs, multi-minute
%% per clip). Permissive retries at any tier would blow cost budgets
%% without improving quality signal -- marginal win-rate per additional
%% retry at the same tier drops quickly.
%% STRICT RULE: one failure per level only. Each tier gets exactly one
%% attempt. Failure escalates immediately to the next tier. A second
%% failure at the same tier is not permitted. This concentrates compute
%% where it pays: moving to the next tier gives a genuinely different
%% strategy space, not another roll of the same dice.
%%   Video L0 FIX -- one attempt, domain-informed prompt rewrite.
%%   Video L1 RETRY -- one attempt, different generation strategy or
%%     model variant.
%%   Video L2 CREATIVE -- one attempt, alternative approach.
%%   Video L3 COLLABORATIVE -- one coordinated attempt that may reshape
%%     the clip's plan (duration-preserving, OTIO is law).
%%   Video L4 HUMAN -- dashboard gate.
%% Infra failures (diagram 8) are classified separately by the diagnostic
%% classifier and consume the INFRA ladder's budget, not this one. An OOM
%% on worker A that succeeds on worker B is NOT a content L0 failure.
flowchart TD
    IN["Authoritative OTIO<br/>+ approved visual plan"]
    IN --> PO["ProductionOrchestrator"]

    PO --> PLAN["Plan phase<br/>assign clips to workers"]
    PLAN --> EXEC["Execute phase<br/>parallel dispatch"]
    EXEC --> REP["Replan phase<br/>reshuffle on failure"]
    REP -->|work remaining| PLAN
    REP -->|done| DONE["All clips ready"]

    subgraph FLEET["GPU worker fleet"]
        direction LR
        W1["Worker one<br/>LTX-2.3"]
        W2["Worker two<br/>LTX-2.3"]
        W3["Worker N<br/>LTX-2.3"]
    end

    EXEC --> W1
    EXEC --> W2
    EXEC --> W3

    W1 --> QA["Bearnaise QA<br/>structural + semantic"]
    W2 --> QA
    W3 --> QA

    QA -->|pass| B2["Upload to B2"]
    QA -->|fail| CLS["Diagnostic classifier<br/>content vs infra<br/>see diagram 8"]
    CLS -->|infra fail| INFRA["Infra ladder (diagram 8)<br/>own budget"]
    CLS -->|content fail| CLAD["Video content ladder<br/>STRICT one-shot per level"]

    subgraph VLADDER["Video content ladder (strict, 1 attempt per tier)"]
        direction TB
        VL0["Video L0 FIX — one attempt<br/>domain-informed prompt rewrite"]
        VL1["Video L1 RETRY — one attempt<br/>different generation strategy<br/>or model variant"]
        VL2["Video L2 CREATIVE — one attempt<br/>alternative approach"]
        VL3["Video L3 COLLABORATIVE — one coordinated attempt<br/>may reshape clip's plan<br/>duration-preserving, OTIO is law"]
        VL4["Video L4 HUMAN — dashboard gate<br/>see diagram 10"]
        VL0 -->|fail escalates immediately| VL1
        VL1 -->|fail escalates immediately| VL2
        VL2 -->|fail escalates immediately| VL3
        VL3 -->|fail escalates immediately| VL4
    end

    CLAD --> VL0
    VL0 -->|resolved| EXEC
    VL1 -->|resolved| EXEC
    VL2 -->|resolved| EXEC
    VL3 -->|resolved| EXEC
    VL4 --> DASH["Dashboard L4 gate"]

    INFRA -->|resolved| EXEC
    INFRA -->|unresolved| DASH

    B2 --> OTIO[("Authoritative OTIO<br/>V1 video track")]
    OTIO --> DONE
```


## 4. Escalation ladder (intended design)

The ladder shape is shared, but per-tier **retry budgets are asymmetric by medium**. Audio (diagram 2) is cheap and its reconciliation is the mechanism that produces the authoritative OTIO — tiers L0 and L1 are permissive with wide retry budgets. Video (diagram 3) is expensive and its outputs are bounded by an OTIO that is already law — the content ladder is strict with exactly one attempt per tier. Infra failures (diagram 8) run on a separate ladder with its own budget, classified away from the content ladder by a diagnostic agent.

```mermaid
%% Escalation ladder (intended design).
%% Every pipeline operation is wrapped by recovery middleware. The ladder
%% is a graduated sequence of LLM-powered agents, each with higher authority
%% and wider scope than the previous. Escalation is a normal operating mode,
%% not an exception path.
%% L0 FIX -- domain specialist rewrites inputs.
%% L1 RETRY -- intelligent retry, analyses error patterns, adjusts params.
%% L2 CREATIVE -- alternative model or approach.
%% L3 COLLABORATIVE -- coordinates across pipeline agents.
%% L4 HUMAN -- AG-UI gate with full diagnostic chain.
%% ASYMMETRIC BUDGETS BY MEDIUM (this is the canonical shape).
%% The ladder SHAPE is shared across media, but retry budgets per tier are
%% calibrated to cost and to the role each medium plays in the pipeline.
%%   AUDIO (diagram 2): cheap, fast, and reconciliation IS the mechanism
%%     that produces the authoritative OTIO. L0 WIDE, L1 GENEROUS, L2
%%     NARROW-MULTI, L3 BOUNDED, L4 human. Abandoning L0 early would
%%     starve the OTIO of narration.
%%   VIDEO (diagram 3): expensive, slow, and constrained by an OTIO that
%%     is already law. STRICT one-shot per tier: each tier gets exactly
%%     one attempt; failure escalates immediately. A second attempt at the
%%     same tier is not permitted -- escalation gives the next-higher
%%     agent a genuinely different strategy space, which pays better
%%     than another roll of the same dice.
%%   INFRA (diagram 8): runs on a SEPARATE ladder with its own budget.
%%     Infra failures are classified away from the content ladder by a
%%     diagnostic agent before any content budget is charged.
flowchart TD
    OP["Operation fails"]
    OP --> L0["L0 FIX<br/>domain specialist agent<br/>rewrites inputs"]
    L0 -->|fixed| OK1["Retry operation"]
    L0 -->|escalate| L1["L1 RETRY<br/>intelligent retry agent<br/>adjusts params"]
    L1 -->|fixed| OK2["Retry operation"]
    L1 -->|escalate| L2["L2 CREATIVE<br/>alternative-strategy agent<br/>different model or approach"]
    L2 -->|fixed| OK3["Retry operation"]
    L2 -->|escalate| L3["L3 COLLABORATIVE<br/>inter-agent coordinator<br/>talks to other agents"]
    L3 -->|fixed| OK4["Retry operation"]
    L3 -->|escalate| L4["L4 HUMAN<br/>AG-UI intervention<br/>full diagnostic chain"]
    L4 --> END["Human decides<br/>approve, rewrite, or abort"]

    OK1 --> DONE["Resume main flow"]
    OK2 --> DONE
    OK3 --> DONE
    OK4 --> DONE
```


## 5. Recovery decision shape

```mermaid
%% RecoveryDecision shape.
%% Every recovery agent (L0-L3) returns a structured decision. The action
%% field determines whether the operation retries with patches, retries as-is,
%% accepts the failure, escalates to the next rung, or aborts the pipeline.
%% state_patches are the surgical kwargs mutations threaded through attempts;
%% this is how domain specialists (L0) actually fix inputs.
flowchart LR
    IN["Failure event<br/>error and context"] --> AG["Recovery agent<br/>for this rung"]
    AG --> D{"RecoveryDecision"}
    D -->|action fix| FIX["Apply state_patches<br/>re-run operation"]
    D -->|action retry| RT["Re-run operation"]
    D -->|action skip| SK["Accept failure<br/>continue"]
    D -->|action escalate| ES["Advance to next rung"]
    D -->|action abort| AB["Stop pipeline"]
```


## 6. Critique and QA substrate

```mermaid
%% Critique and QA substrate.
%% Producers (content agents + GPU clip generation) emit artifacts.
%% Critics (QA jury, Bearnaise gatekeeper, Timeline Guardian, scenario and
%% coherence evaluators) score them. Adapters normalise critic output into
%% the ArtifactCritiqueRecord schema, which accretes in the critique store
%% (disk + B2 mirror). The Escalation Supervisor reads the store via
%% read-only tools and picks canonical EscalationActions.
%% Critique is fire-and-forget where possible so it does not block the main
%% flow; results land in the store and surface when the supervisor reads.
flowchart TD
    subgraph PROD["Producers"]
        SD["Scenario Director"]
        VD["Visual Director"]
        AA["Audio Agent"]
        CL["Clips from GPU"]
    end

    subgraph CRIT["Critic agents"]
        QJ["QA Jury"]
        GK["Gatekeeper<br/>Bearnaise two-pass"]
        TGL["Timeline Guardian"]
        SE["Scenario Evaluator"]
        CE["Coherence Evaluator"]
    end

    SD --> SE
    VD --> CE
    AA --> TGL
    CL --> GK
    CL --> QJ

    SE --> AD["Adapters"]
    CE --> AD
    GK --> AD
    QJ --> AD
    TGL --> AD

    AD --> REC[("ArtifactCritiqueRecord")]
    REC --> STORE[("Critique store<br/>disk and B2 mirror")]

    STORE --> SUP["Escalation Supervisor<br/>read-only tools"]
    SUP --> ACT["Supervisor picks<br/>canonical EscalationAction"]
```


## 7. Human gates and approval flow

```mermaid
%% Human gates and approval flow.
%% Every stage boundary emits an approval gate with a 10-second intervention
%% window during which the reviewer may halt. Approvals are binding: once
%% approved, downstream stages cannot silently re-run or invalidate the
%% approved artifact -- they can only escalate fresh (which routes through
%% the prompt-revision mechanism in diagram 2a).
%% The approval_state.json file is the binding record; the pipeline polls
%% it and blocks until a decision lands.
sequenceDiagram
    autonumber
    participant P as Pipeline stage
    participant DR as DashboardReporter
    participant SSE as SSE stream
    participant UI as Frontend
    participant H as Human reviewer
    participant AS as approval_state.json

    P->>DR: stage complete, emit gate
    DR->>SSE: stream gate event
    SSE->>UI: render gate card
    UI->>H: show artifact plus 10s window

    alt Human approves
        H->>UI: click approve
        UI->>AS: write approved
        P->>AS: poll
        AS->>P: approved
        P->>P: continue to next stage
    else Human halts
        H->>UI: click halt
        UI->>AS: write halted
        P->>AS: poll
        AS->>P: halted
        P->>P: abort run
    else Timeout
        P->>AS: poll
        AS->>P: still pending
        P->>P: block until decision
    end
```


## 8. GPU fleet lifecycle, health, and the infra escalation ladder

Infrastructure failure is an escalation axis in its own right, orthogonal to content failure. Every clip failure is classified as content, infra, or unclear; each axis has its own L0–L4 ladder; both terminate at the same L4 human gate on the dashboard. Worker health is inferred from infra observation, never from job outcomes — a single failed clip does not condemn a worker, and a single good clip does not exonerate one.

```mermaid
%% GPU fleet lifecycle and the INFRA escalation ladder.
%% Infra failure is a first-class escalation axis, orthogonal to content
%% failure. Every clip failure is classified by a diagnostic agent into one
%% of three buckets: content (prompt/plan issue), infra (substrate issue),
%% or unclear (runs a short classifier to decide).
%% Failure classes at infra: worker death (preemption, OOM, driver reset),
%% cold-start failure (image pull, weight load), network partition (provider
%% outage, storage unreachable), VRAM exhaustion on model swap, thermal
%% throttle, authorization revocation, billing guard trip. Each has a
%% distinct signature; lumping them loses signal.
%% Infra escalation ladder, L0-L4, distinct from the content ladder:
%% Infra L0 FIX -- retry on a different healthy worker in the fleet.
%% Infra L1 RETRY -- recycle the suspect worker; redispatch in parallel.
%% Infra L2 CREATIVE -- scale fleet, hot-swap GPU tier (within VRAM floor),
%%   change region, change provider.
%% Infra L3 COLLABORATIVE -- coordinate with content ladder; maybe the clip
%%   params are the cause and content can down-spec. Coordinate with budget
%%   guard; may reshape the production plan.
%% Infra L4 HUMAN -- infra budget exhausted, provider outage, capacity
%%   mismatch with the scenario. Goes to the SAME L4 gate as content L4.
%% Workers are provisioned LAZILY in the background during the pipeline's
%% CPU-bound script phases so production never idles waiting on boot.
%% VRAM hard floor is 48-80GB depending on LTX-2.3 tier; models are loaded
%% sequentially through a state-dict registry to keep VRAM spikes bounded.
%% Single-failure-doesn't-condemn: the coordinator requires multiple
%% INDEPENDENT signals (job failure AND infra_agent CUDA error) before it
%% marks a worker bad. This prevents correlated-failure cascades where one
%% bad prompt takes down the whole fleet.
flowchart TD
    START["Pipeline starts<br/>CPU-bound script phase"]
    START -.lazy provision.-> PROV["worker_provisioner"]

    PROV --> VAST["Vast.ai API<br/>boot GPU instances"]
    VAST --> WARM["Warming workers<br/>loading LTX-2.3"]
    WARM --> READY["Ready workers<br/>state-dict loaded"]

    READY --> DISP["Fleet coordinator<br/>health-aware dispatch"]
    DISP --> JOBS["Clip generation jobs"]
    JOBS --> JR["Job result"]

    IA["infra_agent daemon<br/>reads GPU telemetry<br/>process health<br/>network status"]
    IA -.monitors.-> WARM
    IA -.monitors.-> READY
    IA -.reports health.-> DISP

    JR --> CLS["Diagnostic classifier<br/>content vs infra vs unclear<br/>requires 2 independent signals<br/>before condemning a worker"]

    CLS -->|content fail| CLAD["Content ladder<br/>see diagrams 2 and 4"]
    CLS -->|infra fail| ILAD["Infra ladder — entry"]
    CLS -->|unclear| DIAG["Short diagnostic run<br/>until reclassified"]
    DIAG --> CLS
    CLS -->|pass| B2["Upload clip to B2"]

    subgraph INFRA["Infra escalation ladder (L0 to L4)"]
        direction TB
        IL0["Infra L0 FIX<br/>retry same job on a different healthy worker<br/>covers transient errors, preemption"]
        IL1["Infra L1 RETRY<br/>recycle suspect worker<br/>redispatch in parallel on another<br/>covers driver state, leaked VRAM, stuck procs"]
        IL2["Infra L2 CREATIVE<br/>scale fleet or hot-swap GPU tier<br/>within VRAM hard floor<br/>different region, different offer, different provider"]
        IL3["Infra L3 COLLABORATIVE<br/>coordinate with content ladder<br/>down-spec params? reshape production plan?<br/>negotiate with budget guard"]
        IL4["Infra L4 HUMAN<br/>budget exhausted, provider outage,<br/>capacity mismatch with scenario"]
        IL0 -->|escalate| IL1
        IL1 -->|escalate| IL2
        IL2 -->|escalate| IL3
        IL3 -->|escalate| IL4
    end

    ILAD --> IL0
    IL0 -->|resolved| JOBS
    IL1 -->|resolved| JOBS
    IL2 -->|resolved| JOBS
    IL3 -->|resolved| JOBS
    IL4 --> GATE["Same L4 gate on dashboard<br/>see diagram 10"]

    IL1 -.recycle.-> PROV
    IL2 -.scale.-> PROV
    IL2 -.hot-swap tier.-> PROV

    READY --> TEAR["Tear down fleet<br/>at run end<br/>stop billing"]
```


## 9. Intermediate preview assemblies

Final assembly is not the only assembly — it is the last in a sequence. At every logical coherence boundary the pipeline produces a preview: a rough cut of everything that currently exists, with honest placeholders for what does not. Previews are QA artifacts that feed both agents and humans, never deliverables. They are how "I don't like this movie" becomes actionable before the whole film is generated.

```mermaid
%% Intermediate preview assemblies.
%% Final assembly is not the only assembly -- it is the last in a sequence.
%% At every logical coherence boundary the pipeline produces a PREVIEW
%% ASSEMBLY: a rough cut of everything that currently exists, with honest
%% placeholders for what does not.
%% Trigger points are fixed checkpoints, not opportunistic renders:
%%   - pre-production preview: authoritative OTIO + audio-only, no video
%%   - scene complete: all clips for scene N exist and passed QA
%%   - act complete: contiguous run of scenes complete
%%   - halfway milestone: 50 percent of clips produced
%% Placeholder treatment is honest: missing video slots render as black
%% cards with text labels ("scene 3 clip 2, in production, ETA 4 min");
%% missing audio blocks render as silence with a caption track of the
%% scripted text. No guessing, no interpolation.
%% Two audiences:
%%   - agents: coherence evaluator raises cross-scene issues invisible at
%%     per-clip granularity (pacing, tonal continuity, run length).
%%     Scenario director may reconsider a later scene based on how earlier
%%     ones are landing.
%%   - humans: real viewing experience on the dashboard. This is the point
%%     where "I don't like this movie" becomes actionable.
%% Previews are QA artifacts, not deliverables. They live in a separate
%% namespace, do not consume the final MP4 budget, and do not advance the
%% pipeline -- advancement is still gated by explicit human gates.
%% A dislike preview may trigger a proactive L4 (see diagram 10) that
%% writes scoped records to the Preference Ledger (see diagram 2a).
flowchart TD
    subgraph TRIG["Trigger points (fixed, not opportunistic)"]
        direction TB
        TP1["Pre-production<br/>auth. OTIO + audio, no video"]
        TP2["Scene N complete<br/>all clips passed QA"]
        TP3["Act complete<br/>contiguous scenes done"]
        TP4["Halfway milestone<br/>50 percent clips ready"]
    end

    TRIG --> PB["Preview builder<br/>reads authoritative OTIO<br/>collects ready clips and audio<br/>renders placeholders for missing"]

    subgraph PLACE["Honest placeholder treatment"]
        direction TB
        PL1["Missing video slot<br/>black card with text label<br/>scene and clip ID, ETA"]
        PL2["Missing audio block<br/>silence with caption track<br/>of scripted text"]
    end

    PB -.placeholders.-> PLACE
    PB --> PV["Preview MP4<br/>separate namespace<br/>not a deliverable"]

    PV --> AG_AUD["Agent audience"]
    PV --> HU_AUD["Human audience"]

    subgraph AGENTS["Agent consumers"]
        direction TB
        CE["Coherence evaluator<br/>cross-scene pacing, tone, ADHD"]
        SDR["Scenario director<br/>reconsider later scenes<br/>based on how earlier ones land"]
    end

    AG_AUD --> AGENTS
    AGENTS -->|issue detected| CLAD["Content escalation ladder<br/>see diagrams 2 and 4"]

    HU_AUD --> DASH["Dashboard<br/>see diagram 10"]
    DASH -->|dislike or concern| PROAC["Proactive human L4<br/>write to Preference Ledger<br/>see diagrams 2a and 10"]

    PV -.does not advance pipeline.-> GATE["Advancement still gated by<br/>explicit human gates (diagram 7)"]
```


## 10. Dashboard: OTIO timeline as centerpiece, continuity, and proactive user-initiated L4

The dashboard's primary surface is the authoritative OTIO rendered as a visual timeline with every produced asset laid on it in its actual slot. Everything else — event streams, reasoning digests, fleet health, cost, ETA — is peripheral metadata orbiting the timeline. Before the authoritative OTIO crystallises at the end of Stage Two, the draft OTIO is shown with a reconciliation overlay. Clicking a slot is the entry point to everything about that slot: artifact history, QA verdicts, reasoning digests, in-scope Preference Ledger records, current rung in the content or infra ladder, and the latest preview assembly containing it. Directives typed while a slot is selected are implicitly scoped to that slot by the Preference Interpreter. Preview assemblies play directly on the timeline. Continuity is the precondition for good human intervention, not an aesthetic choice.

```mermaid
%% Dashboard with OTIO timeline as centerpiece, continuity, and proactive L4.
%% The primary surface is the AUTHORITATIVE OTIO rendered as a visual
%% timeline with every produced asset laid on it in its actual slot.
%% Three tracks drawn to scale against real time, exactly mirroring the
%% pipeline's OTIO: V1_Video, A1_Narration, A2_Music.
%% Each slot shows its current state INLINE:
%%   - generated video as a thumbnail strip
%%   - generated narration as a waveform
%%   - generated music as a waveform
%%   - pending slots as labeled placeholders with ETA
%%   - failed slots in red with failure class (content vs infra)
%%   - in-progress slots in amber with current rung indicator
%% Before the authoritative OTIO exists (before end of Stage Two), the
%% DRAFT OTIO is shown with a reconciliation overlay: scripted durations
%% vs measured (WhisperX) durations vs current pacing slack.
%% CLICKING A SLOT is the entry point to everything about it:
%%   - artifact history and provenance
%%   - QA verdicts (Bearnaise structural + semantic, Timeline Guardian)
%%   - reasoning digests for every decision that touched the slot
%%   - Preference Ledger records whose scope covers the slot
%%   - current rung in content ladder (diagrams 2, 4) and infra ladder (diagram 8)
%%   - latest preview assembly containing the slot (diagram 9)
%% PLAY on the timeline renders a preview from current state -- the human
%% hears and sees the actual experience at any zoom level, with honest
%% placeholders for what is pending.
%% PROACTIVE L4 IS SLOT-SCOPED BY DEFAULT. A directive typed while a slot
%% is selected is implicitly scoped to that scene / block / clip; the
%% Preference Interpreter uses the selection as a scope hint. Unscoped
%% global directives are still possible via an explicit "global" control.
%% Peripheral panels stay available but stop being the centerpiece:
%% fleet health, cost/ETA, ledger history, agent reasoning digest stream,
%% gate prompts, infra events.
%% Continuity principle: the dashboard keeps the human's understanding
%% synchronized with pipeline state AT ALL TIMES, so that when the human
%% intervenes, their decision is well-founded. An out-of-the-loop human
%% making an L4 decision is worse than no L4 at all.
flowchart TD
    subgraph PIPE["Pipeline (all stages and systems)"]
        direction TB
        OTIO_SRC[("Authoritative OTIO<br/>THE LAW<br/>or Draft OTIO before Stage Two end")]
        S_ART["Media assets laid on OTIO slots<br/>video clips, narration, music"]
        S_PREV["Preview assemblies<br/>see diagram 9"]
        S_QA["QA verdicts<br/>per-slot"]
        S_L03["L0-L3 resolutions<br/>content and infra<br/>per-slot rung"]
        S_LED["Preference Ledger changes<br/>see diagram 2a"]
        S_GATE["Human gates"]
        S_INF["Infra events<br/>fleet health, cost tick<br/>see diagram 8"]
        S_ETA["ETA revisions"]
    end

    PIPE --> DIG["Reasoning digest writer<br/>structured, slot-addressed summaries<br/>what tried, what failed,<br/>what chosen, current rung"]
    DIG --> STREAM["Dashboard SSE stream<br/>always-on push channel"]

    STREAM --> UI["Dashboard UI"]

    subgraph CENTER["Centerpiece: OTIO timeline view"]
        direction TB
        TL["Timeline surface<br/>three tracks to scale<br/>V1_Video, A1_Narration, A2_Music"]
        SLOTS["Per-slot inline rendering<br/>thumbnails, waveforms,<br/>placeholders with ETA,<br/>red = failed, amber = in progress"]
        RECON["Reconciliation overlay<br/>scripted vs measured durations<br/>visible pre-crystallisation"]
        PLAY["Play from any point<br/>preview from current state<br/>honest placeholders"]
    end

    subgraph PERIPH["Peripheral panels (metadata orbiting the timeline)"]
        direction TB
        PP_FLEET["Fleet health<br/>worker list, GPU tier<br/>from diagram 8"]
        PP_COST["Cost and ETA"]
        PP_LED["Ledger history<br/>from diagram 2a"]
        PP_DIG["Reasoning digest stream"]
        PP_GATE["Pending human gates<br/>from diagram 7"]
        PP_INF["Infra event log"]
    end

    UI --> CENTER
    UI --> PERIPH

    HU["Human reviewer"]
    CENTER --> HU
    PERIPH --> HU

    HU --> SEL["Select slot on timeline<br/>opens slot detail panel"]
    SEL --> DETAIL["Slot detail<br/>artifact history and provenance<br/>QA verdicts<br/>reasoning digests for this slot<br/>in-scope ledger records<br/>current rung (content + infra)<br/>latest preview containing slot"]

    HU --> DEC{"Intervention mode"}
    DEC -->|pipeline asked| GATE_L4["Gate L4<br/>see diagram 7<br/>reactive to explicit prompt"]
    DEC -->|I don't like this movie| HALT["Halt-anywhere button<br/>suspend pipeline<br/>freeze state<br/>open L4 interface"]
    DEC -->|inject a preference| DIRECT["Direct directive input<br/>no halt required"]

    GATE_L4 --> DIREC["Free-form directive<br/>+ implicit scope from<br/>current slot selection"]
    HALT --> DIREC
    DIRECT --> DIREC
    SEL -.scope hint.-> DIREC

    DIREC --> PI["Preference Interpreter<br/>see diagram 2a<br/>uses selection as scope hint<br/>explicit 'global' control overrides"]
    PI --> LED["Append to Preference Ledger<br/>bump ledger revision"]
    LED --> CC["Consistency checker fires<br/>impact analyzer<br/>re-manifestation planner"]
    CC --> RE["Surgical re-manifestation<br/>of invalidated slots only"]
    RE --> PIPE

    HALT -.optional resume.-> PIPE
```
