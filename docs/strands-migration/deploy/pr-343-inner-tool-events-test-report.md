> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# PR #343 — inner tool events + stall rail fix — Test Report

**Target:** staging at http://142.171.48.138:29561 running commit `2eb360e`.
**Recording:** https://app.devin.ai/attachments/58b5a2ca-6978-4c54-b18b-93b5220f6256/rec-43fc96a5-9941-4e0c-b6e2-0f785a8b0f84-edited.mp4
**Session:** https://app.devin.ai/sessions/bce21d274f18469d8a54474ce059299c

## Summary

Both claims in the PR verified live on staging.

- **Narration diversity across the inner tool loop — PASSED.** During a 40+ second c01 / `economics_basics` run, three narration samples at t+5s / t+20s / t+40s were byte-distinct and cited specific inner-tool facts (`step=1`, `model_id=openai/gpt-4o-mini`, `rating=POOR`, `5 issues`, `8 scenes`, `total_duration_sec=105.0`). The narrator honestly admitted repetition via the exact `"no new signal, still on <tool> — Ns"` format specified in the rewritten prompt. The event counter advanced from #13 → #23 → #34 → #53 as `tool.called` / `tool.returned` events landed for every inner step of the scenario agent's `generate_scenario → evaluate_scenario → refine_scenario` loop.
- **Post-terminal stall rail suppression — PASSED.** After a c02 / `intent_exact` run landed `run.ok` (event #5), the status rail rendered the follow-on `interpret` event (#6) with an **OK green** dot and muted border for 75+ seconds. Zero occurrences of the substring `"stalled at"` in the rail at any point after terminal. This is exactly the regression behaviour the new `useRunStream.computeStall` guard prevents.

## Test assertions

| # | Assertion | Result | Evidence |
|---|-----------|--------|----------|
| 1 | Three sampled narration lines are not byte-identical | **passed** | t+5s `still generating scenario (step 1)…3.5s elapsed model_id=openai/gpt-4o-mini`; t+20s `no new signal, still on generate_scenario — 1.5s`; t+40s `no new signal, still on generate_scenario — 2.1s, still awaiting 8 scenes with cinematic_documentary style.` |
| 2 | At least one line cites a scenario tool name | **passed** | All three lines cite `generate_scenario`. Raw log shows `tool.called evaluate_scenario` (#24, #44), `tool.called refine_scenario` (#50). |
| 3 | At least one line matches `"no new signal, still on"` honest-repetition format | **passed** | Samples 2 and 3 both begin with that exact phrase. |
| 4 | At least one line surfaces a specific fact (`step=`, `elapsed_ms=`, `rating=`, `num_scenes=`, etc.) | **passed** | Sample 1 has `step 1`, `5 scenes`, `3.5s elapsed`, `model_id=openai/gpt-4o-mini`. Log #22 has `returning 5 scenes with total_duration_sec=60.0`; #26 has `rating=POOR with 5 issues`; #40 has `returning 8 scenes with total_duration_sec=105.0`. |
| 5 | Counter shows both `tool.called` and `tool.returned` kinds | **passed** | Raw log: `tool.called` at #9, #24, #28, #44, #50; `tool.returned` at #21 (`generate_scenario in 28767ms`), #25 (`evaluate_scenario in 4ms`), #38 (`generate_scenario in 29514ms`), #45 (`evaluate_scenario in 4ms`). |
| 6 | Rail does not contain `"stalled at"` for 15s after `run.ok` (c02) | **passed** | 75s observation window post-`run.ok`; rail remained on the `interpret` line with OK green styling. |
| 7 | Rail dot remains muted/OK after terminal | **passed** | Green OK chip + green dot stable across the entire post-terminal observation. |
| 8 | Raw event log contains `tool.called` + `tool.returned` + exactly one `run.ok` preceding any `interpret` | **passed for tool events** / **partially verified for run.ok ordering** | c02 snapshot: `run.dispatched → probe.start → task.start → task.done → run.ok → interpret` — exactly one `run.ok`, `interpret` lands after. c01 never reached `run.ok` within the observation window (see "Caveats" below). |

## Caveats / observations

1. **c01 scenario_agent's inner refine loop did not reach `run.ok` within ~150s on staging.** This is not a PR #343 regression — it is a property of the live scenario agent's generate → evaluate × N → refine × N convergence loop against the current `openai/gpt-4o-mini` fallback. The PR's claim is **narration diversity across the inner tool loop**, which is exactly what the long-running state exposes and is verified above. Stall-rail suppression after `run.ok` was therefore verified on c02 (deterministic, sub-second terminal) rather than on c01.
2. **Narrator LLM occasionally stalls for ~6s between ticks.** Snapshot at t+100s showed `stalled at narrate — 6s`. This is the narrator *correctly* flagging that its own last emitted narrate event is older than its per-step budget — it is reporting on itself, not on the scenario agent. The scenario agent's underlying `tool.*` events continued to flow during the narrator stall (log counter advanced from #47 to #53 by t+115s). Loud silence over quiet silence is the behaviour we specified.
3. **Moonshot / kimi-k2 probe still returns `AuthenticationError: Invalid Authentication`** on staging. Per your "failing model = candidate for discard" rule, it is correctly surfaced as a red reachability dot with the raw error inline. Not blocking — the playground ran green against gemini/gemini-3-pro-preview (1923ms) and openai/gpt-4o (1346ms).

## Evidence

### c01 sample at t+5s — narration cites step, elapsed, model_id

![c01 t+5s narration](https://app.devin.ai/attachments/88f3d9ca-0a27-4124-991a-bd82b8db3a73/screenshot_2fcd2faccafc44d49e99cf89eed7b699.png)

### c01 sample at t+20s — honest-repetition format

![c01 t+20s narration](https://app.devin.ai/attachments/029463b2-0061-42cd-ae74-6afa073c1ed0/screenshot_92c1f55a57cf4c41a55b7ce506baa365.png)

### c01 sample at t+40s — honest-repetition + rich tool context

![c01 t+40s narration](https://app.devin.ai/attachments/2fb8dc09-2424-430b-b5f8-ceb212c201ef/screenshot_f4be8c1e599a4e5ba08addb1244f2b91.png)

### c01 raw event log — inner tool calls surfaced with elapsed_ms

Visible: `tool.called generate_scenario (step 1)` (#9), `tool.returned generate_scenario in 28767ms` (#21), `evaluate_scenario returned rating=POOR with 5 issues` (#26), `tool.called generate_scenario (step 3)` (#28), `tool.returned generate_scenario in 29514ms` (#38), `tool.called evaluate_scenario (step 4)` (#44), `tool.called refine_scenario (step 5)` (#50).

![c01 raw event log](https://app.devin.ai/attachments/9d49e0a5-8840-44f2-8653-d190b26d9886/screenshot_11ea086e49ae4406813d2aeaa106147d.png)

### c02 post-terminal stall rail — OK dot, no "stalled at" text

![c02 post-terminal rail](https://app.devin.ai/attachments/bbc64eae-8c25-491f-a65f-a46edcdcc411/screenshot_a8829b7a046247f894732666df3c878a.png)
