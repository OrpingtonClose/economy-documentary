# documentary-strands-migration

> Rewrite the economy-documentary pipeline as a **DeepAgent orchestrator
> driving Strands agent leaves**, following the anti-complexity-creep
> playbook and the MiroThinker architecture pattern.

This directory is the **design system of record** for migrating the
`server/agents/` tree (Google ADK, 5 k+ LOC of agents + 22 callback files +
`pipeline.py` monkey-patching) to:

- a **single DeepAgent orchestrator** ([`create_deep_agent(...)`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/graph.py#L218)) that plans, monitors, escalates, and learns across runs via `MemoryMiddleware`, and
- a fixed roster of **Strands-agent leaves** (`@tool` wrappers + small `strands.Agent`s) that do the domain work the orchestrator delegates to.

It is **not** the migration itself. Each document here is a spec that must be
precise enough to hand off to an implementer and have them write code that
passes evals without needing to re-read the current ADK pipeline.

---

## Principles (non-negotiable)

1. **DeepAgent is the only brain.** Planning, retry, escalation, parallel
   scheduling, and human-in-the-loop all live in one DeepAgent configured
   via [`create_deep_agent(...)`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/graph.py#L218).
   No `GraphBuilder` at the top level. No `LoopAgent`-style external loops.
2. **Strands agents are leaves.** Every Strands component does one thing,
   ships ≤ 5 tools, lives in ≤ 400 LOC, and is called by the orchestrator
   either through a `@tool` wrapper or as a [`SubAgent`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/middleware/subagents.py#L25).
3. **MemoryMiddleware carries the invariants.** The orchestrator boots with
   [`memory=["docs/strands-migration/AGENTS.md", ".deepagents/AGENTS.md"]`](./AGENTS.md).
   That file is the durable behavioural contract — hard invariants (VRAM
   floor, fail-closed, OTIO contract) plus mutable planning heuristics the
   agent `edit_file`s when it learns.
4. **Long jobs use AsyncTaskPool, not synchronous tool calls.** TTS, LTX
   video, and assembly are launched through `launch_*` / `check_tasks` /
   `await_tasks` tools (the MiroThinker pattern —
   [`apps/strands-agent/task_tools.py`](https://github.com/OrpingtonClose/MiroThinker/blob/main/apps/strands-agent/task_tools.py)).
   The orchestrator can launch many scenes in parallel without blocking.
5. **Human-in-the-loop is declarative.** Approval gates are
   [`interrupt_on={...}`](https://github.com/OrpingtonClose/deepagents/blob/main/libs/deepagents/deepagents/graph.py#L363)
   on sensitive tools, not file polling. The orchestrator's graph pauses;
   the caller resumes with `accept` / `edit` / `reject`.
6. **Evals split into orchestration and leaf layers.** Orchestration evals
   measure trajectory, parallel launch, memory honouring, and escalation
   decisions. Leaf evals measure what each Strands agent produces
   (ScenarioQuality, AudioInvariant, TimelineCompliance, …). Both ship
   with every PR; see [`eval-framework/THRESHOLDS.md`](./eval-framework/THRESHOLDS.md).

---

## Why DeepAgent on top, Strands underneath?

| Concern | DeepAgent (orchestrator) | Strands (leaf) |
|---------|-------------------------|----------------|
| Planning, todo tracking | Yes (`TodoListMiddleware`) | No |
| Persistent memory / learning | Yes (`MemoryMiddleware`) | No |
| Filesystem / skills / subagents | Yes (`FilesystemMiddleware`, `SubAgentMiddleware`, `SkillsMiddleware`) | No |
| Human-in-the-loop gates | Yes (`HumanInTheLoopMiddleware` via `interrupt_on`) | Yes (`Interrupt`), but we lift it up |
| OTel tracing | Yes (via LangGraph) | Yes (`strands.telemetry`) |
| Concurrent `@tool` execution inside a single agent turn | Limited | Yes (`ConcurrentToolExecutor`) |
| Long-running remote jobs | Yes (`AsyncSubAgent` on Agent Protocol server) | No — delegate via `launch_*` |
| Stateful small domain agent (1 LLM + ≤ 5 tools) | Overkill | Yes |

MiroThinker made this split explicit in
[`apps/strands-agent/orchestrator.py`](https://github.com/OrpingtonClose/MiroThinker/blob/main/apps/strands-agent/orchestrator.py):
the DeepAgent wraps `launch_research` / `launch_harvest` / `launch_gossip`
task tools + corpus tools + skills, while the actual Strands agents
(`Thinker`, `Researcher`, `Synthesiser`) run as workers. We mirror that
split for the documentary pipeline.

---

## How to use this repo

1. Read [`AGENTS.md`](./AGENTS.md) — the seeded memory. That's the
   behavioural contract the orchestrator boots with.
2. Read [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full current →
   target mapping, ADK-construct → DeepAgent+Strands-construct table, and
   model routing.
3. Read [`SEQUENCE.md`](./SEQUENCE.md) for the build order and dependency
   graph.
4. For each component you implement:
   - Read [`components/NN-name.md`](./components/) for the exact spec.
   - Read [`reference/DEEPAGENT_PATTERNS.md`](./reference/DEEPAGENT_PATTERNS.md)
     (if adding to the orchestrator) or
     [`reference/STRANDS_SDK_PATTERNS.md`](./reference/STRANDS_SDK_PATTERNS.md)
     (if implementing a leaf).
   - Ship the code, the evals `Experiment` JSON, and a CI job together.
5. CI must pass all thresholds in
   [`eval-framework/THRESHOLDS.md`](./eval-framework/THRESHOLDS.md) before
   merge.

---

## Source repos

| Repo | Role |
|------|------|
| [`OrpingtonClose/economy-documentary`](https://github.com/OrpingtonClose/economy-documentary) | Current ADK pipeline — the thing being rewritten. Line refs in these docs point here. |
| [`OrpingtonClose/deepagents`](https://github.com/OrpingtonClose/deepagents) | Orchestrator framework (`create_deep_agent`, `SubAgent`, `AsyncSubAgent`, `MemoryMiddleware`, `HumanInTheLoopMiddleware`). |
| [`OrpingtonClose/sdk-python`](https://github.com/OrpingtonClose/sdk-python) | Strands Agents SDK (`strands.Agent`, hooks, conversation managers). The leaf framework. |
| [`OrpingtonClose/MiroThinker`](https://github.com/OrpingtonClose/MiroThinker) | Reference architecture: DeepAgent + Strands + AsyncTaskPool. The shape we copy. |
| [`OrpingtonClose/strands-evals`](https://github.com/OrpingtonClose/strands-evals) | Evals SDK (`Experiment`, `Case`, evaluators, `ToolSimulator`, `ActorSimulator`). |
| [`OrpingtonClose/strands-devtools`](https://github.com/OrpingtonClose/strands-devtools) | CDK + Lambda eval runner patterns (`cdk-evals/lambda/eval-runner/handler.py`). |

---

## Directory layout

```
docs/strands-migration/
├── README.md                      # this file
├── AGENTS.md                      # seeded memory for MemoryMiddleware
├── ARCHITECTURE.md                # current → target, ADK → DeepAgent+Strands mapping
├── SEQUENCE.md                    # build order, dependency graph, DoD per component
├── eval-framework/
│   ├── EVAL_ARCHITECTURE.md       # orchestration evals + leaf evals
│   ├── CUSTOM_EVALUATORS.md       # custom evaluators (leaf + orchestration)
│   ├── SIMULATION.md              # ToolSimulator / ActorSimulator configurations
│   ├── CI_PIPELINE.md             # GitHub Actions workflow spec
│   └── THRESHOLDS.md              # per-stage regression thresholds
├── components/
│   ├── 01-scenario-agent.md       # Strands leaf (or SubAgent) — internal loop
│   ├── 02-timing-evaluator.md     # Strands leaf — deterministic @tool
│   ├── 03-scenario-refiner.md     # Strands leaf — called inside scenario/timing
│   ├── 04-audio-agent.md          # Strands leaf — @tool wrapping TTS + WhisperX
│   ├── 05-timing-loop.md          # DeepAgent plan: launch_audio → evaluate → refine
│   ├── 06-content-analyst.md      # Strands leaf
│   ├── 07-visual-concepter.md     # Strands leaf
│   ├── 08-coherence-evaluator.md  # Strands leaf
│   ├── 09-visual-loop.md          # DeepAgent plan: delegate to visual SubAgent
│   ├── 10-production-supervisor.md # DeepAgent SubAgent — GPU dispatch specialist
│   ├── 11-assembly-agent.md       # Strands leaf — @tool wrapping OTIO → ffmpeg
│   ├── 12-recovery-agents.md      # Strands leaves — fix/retry/skip specialists
│   ├── 13-escalation-supervisor.md # DeepAgent SubAgent — escalation decisions
│   ├── 14-pipeline-graph.md       # Top-level create_deep_agent(...) spec
│   └── 15-approval-gates.md       # interrupt_on configuration + resume protocol
├── contracts/
│   ├── CONTRACTS.md               # StageContract → ContractEnforcer hooks
│   └── STATE_SCHEMA.md            # deepagents state + Strands agent.state shapes
└── reference/
    ├── CURRENT_AGENTS.md          # inventory of every file under server/agents/
    ├── CURRENT_TOOLS.md           # inventory of every file under server/tools/
    ├── CURRENT_CALLBACKS.md       # the 22 callback files and which hooks they map to
    ├── DEEPAGENT_PATTERNS.md      # create_deep_agent, SubAgent, AsyncSubAgent, middleware
    └── STRANDS_SDK_PATTERNS.md    # canonical snippets for each Strands SDK construct
```
