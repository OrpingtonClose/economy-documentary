# `strands_agents.quanta` — the atom layer

This package makes the **atomic layer** of the 15-component pipeline
filesystem-visible. Every function exported here is:

- **pure** — dict (or primitive) in, dict (or primitive) out
- **deterministic** — same input always yields the same output
- **offline** — no network, no disk, no subprocess, no LLM call
- **mechanically testable** — tests feed realistic inputs and assert
  on output shape, without any judge calls

## Why this exists

The 15 component modules under `server/strands_agents/` each mix two
kinds of code:

- **Atoms** — pure deterministic transformations (prompt → revised
  prompt, scenes + alignment → timing report, diagnostic payload →
  escalation decision).
- **Connectors** — orchestration that chains atoms together (loops,
  retries, SubAgent prompts, approval gates, parallel task pools).

Before this package existed, the atoms were hidden behind
`set_*_helpers(...)` injection points used only by tests. Now they
are exposed as plain functions. The connectors remain in their
original modules — the whole point of the split is to mark which
code is pure and which is not.

## Layout

One module per component that contributes atoms. Some components
are wholly connective (the timing loop, the pipeline graph, the
visual loop SubAgent) and do not appear here.

| Module                    | Component(s) | Atoms                                                                                  |
| ------------------------- | ------------ | -------------------------------------------------------------------------------------- |
| `quanta/scenario.py`      | 01           | `evaluate_scenario_structural`, `sum_scenario_duration`, `derive_scenario_topic`       |
| `quanta/timing.py`        | 02           | `compute_timing_report`                                                                |
| `quanta/refiner.py`       | 03           | `adjust_scene_durations`, `validate_pronunciation_hints`                               |
| `quanta/style_lock.py`    | 07           | `check_style_lock`                                                                     |
| `quanta/coherence.py`     | 08           | `compute_structural_violations`                                                        |
| `quanta/artifact_qa.py`   | 10           | `evaluate_visual_artifact_quality`                                                     |
| `quanta/assembly.py`      | 11           | `check_assembly_inputs`                                                                |
| `quanta/recovery.py`      | 12           | `classify_failure`, `propose_revised_concept`, `diff_concept`                          |
| `quanta/escalation.py`    | 13           | `decide_escalation_action`                                                             |
| `quanta/approval.py`      | 15           | `validate_decision`, `resume_command_from_decision`, `allowed_decisions_for`           |

## Direct-proof tests

Every atom has a matching test module under
`server/strands_agents/tests/quanta/`. The tests are **mechanical**
— they feed realistic inputs and assert on output shape and
invariant compliance. No LLM judge calls on atoms. (Judges return
at the composition-proof layer where subjective calls live.)

Run them:

```bash
poetry run pytest server/strands_agents/tests/quanta/
```

## What does *not* live here

The following are connectors — they call atoms (and sometimes
LLMs / GPU workers / TTS) but they are not atoms themselves:

- **Timing loop** (component 05) — the 10-iteration ladder.
- **Visual loop SubAgent** (component 09) — drives the
  concept → coherence → refine cycle.
- **Production supervisor** (component 10 — the SubAgent, not the
  `evaluate_visual_artifact_quality` atom).
- **Assembly orchestrator** (component 11 — the module that actually
  writes the OTIO and drives ffmpeg; only `check_assembly_inputs`
  is an atom).
- **Pipeline graph** (component 14).
- **Escalation SubAgent** (the agent — only `decide_escalation_action`
  is the atom).
- **Approval gate middleware** (the LangGraph `interrupt_on` wiring —
  only the pure helpers are atoms).

They stay in their original modules.
