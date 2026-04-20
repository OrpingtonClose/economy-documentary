# documentary-strands-migration

> Rewrite the economy-documentary pipeline element-by-element using the
> Strands Agents SDK, following the anti-complexity-creep playbook.

This directory is the **design system of record** for migrating the
`server/agents/` tree (Google ADK, 5 k+ LOC of agents + 22 callback files +
`pipeline.py` monkey-patching) to a set of small, well-evaluated
`strands.Agent` components composed through `strands.multiagent.GraphBuilder`.

It is **not** the migration itself. Each document here is a spec that must be
precise enough to hand off to an implementer and have them write code that
passes evals without needing to re-read the current ADK pipeline.

---

## Principles (non-negotiable)

1. **One agent, 3–5 tools, one file per component.** If a component needs
   more, it gets split. The current `visual_director.py` (830 lines) is the
   anti-pattern.
2. **`GraphBuilder` only out of components we demonstrably know work.** A
   component enters the pipeline graph only after its evals harness passes
   all thresholds in [`eval-framework/THRESHOLDS.md`](./eval-framework/THRESHOLDS.md).
3. **The evals harness ships with every component.** PRs that add an agent
   without an `Experiment` JSON + passing CI are rejected. This is the
   regression wall we lacked in ADK (`run_pipeline.py` failures discovered in
   production).
4. **`SlidingWindowConversationManager` wherever an agent needs memory.**
   Especially on recovery + escalation agents that re-enter the same
   diagnostic context multiple times.
5. **Write code, not pipelines.** Prefer letting a single agent loop
   internally (generate → evaluate → refine as tool calls) over composing
   three agents through an external `LoopAgent`. Only hoist to
   `GraphBuilder` when the loop crosses a deterministic step (audio render,
   video render, assembly) or a human approval gate.

---

## How to use this repo

1. Read [`SEQUENCE.md`](./SEQUENCE.md) for the build order and dependency
   graph. Components are ordered so that each one's prerequisites already
   exist.
2. Read [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full current → target
   mapping, including the ADK-construct → Strands-construct table and model
   routing.
3. Read the relevant file in [`components/`](./components/) for the exact
   spec of the component you're implementing.
4. Open a PR that includes:
   - The Strands `Agent` file (single file, < 400 LOC).
   - The `Experiment` JSON (serialized via `Experiment.to_file`).
   - The pytest file that runs the experiment as a CI job.
   - A link back to this repo's component spec.
5. CI must pass all thresholds in
   [`eval-framework/THRESHOLDS.md`](./eval-framework/THRESHOLDS.md) before
   merge.

---

## Source repos

| Repo | Role |
|------|------|
| [`OrpingtonClose/economy-documentary`](https://github.com/OrpingtonClose/economy-documentary) | Current ADK pipeline — the thing being rewritten. Line refs in these docs point here. |
| [`OrpingtonClose/sdk-python`](https://github.com/OrpingtonClose/sdk-python) | Strands Agents SDK (`strands.Agent`, `GraphBuilder`, hooks, interrupts, conversation managers). |
| [`OrpingtonClose/strands-evals`](https://github.com/OrpingtonClose/strands-evals) | Evals SDK (`Experiment`, `Case`, evaluators, `ToolSimulator`, `ActorSimulator`). |
| [`OrpingtonClose/strands-devtools`](https://github.com/OrpingtonClose/strands-devtools) | CDK + Lambda eval runner patterns (`cdk-evals/lambda/eval-runner/handler.py`). |
| [`OrpingtonClose/strands-samples`](https://github.com/OrpingtonClose/strands-samples) | Reference Strands patterns for multi-agent + industry use cases. |

---

## Directory layout

```
docs/strands-migration/
├── README.md                  # this file
├── ARCHITECTURE.md            # current → target, ADK → Strands mapping
├── SEQUENCE.md                # build order, dependency graph, DoD per component
├── eval-framework/
│   ├── EVAL_ARCHITECTURE.md   # strands-evals primitives and how we wire them
│   ├── CUSTOM_EVALUATORS.md   # the 7 custom evaluators we ship with this migration
│   ├── SIMULATION.md          # ToolSimulator / ActorSimulator configurations
│   ├── CI_PIPELINE.md         # GitHub Actions workflow spec
│   └── THRESHOLDS.md          # per-stage regression thresholds (hard gates vs soft)
├── components/                # 15 component specs, numbered in build order
│   ├── 01-scenario-agent.md
│   ├── 02-timing-evaluator.md
│   ├── 03-scenario-refiner.md
│   ├── 04-audio-agent.md
│   ├── 05-timing-loop.md
│   ├── 06-content-analyst.md
│   ├── 07-visual-concepter.md
│   ├── 08-coherence-evaluator.md
│   ├── 09-visual-loop.md
│   ├── 10-production-supervisor.md
│   ├── 11-assembly-agent.md
│   ├── 12-recovery-agents.md
│   ├── 13-escalation-supervisor.md
│   ├── 14-pipeline-graph.md
│   └── 15-approval-gates.md
├── contracts/
│   ├── CONTRACTS.md           # StageContract as-is and the Strands-native equivalent
│   └── STATE_SCHEMA.md        # scenes[], style_lock, whisperx_alignment, visual_concepts shapes
└── reference/
    ├── CURRENT_AGENTS.md      # inventory of every file under server/agents/ with LoC
    ├── CURRENT_TOOLS.md       # inventory of every file under server/tools/
    ├── CURRENT_CALLBACKS.md   # the 22 callback files and which hooks they map to
    └── STRANDS_SDK_PATTERNS.md # canonical snippets for each SDK construct we use
```
