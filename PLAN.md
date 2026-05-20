# Plan: Single-Scene 30s Video Pipeline

## Goal
Run pipeline once, generate exactly **1 scene, 30 seconds**, produce `master.mp4`.

## Current Problems (from observation)

1. **Scenario**: Generates 12 scenes by default. Need to constrain to 1 scene, ~30s total.
2. **Audio**: Generated narration but clip not added to A1_Narration track (timeline path issue).
3. **Video**: Production plan created but `submitted=0` clips to GPU worker.
4. **Assembly**: Tool schema mismatch (`assemble_final_cut` declares 0 params but needs 5).
5. **Debug-gym**: False-positive kills, excessive complexity.

## Fixes Needed

### 1. Scenario Constraining (MINIMAL)
**Where**: `server/strands_agents/graph_pipeline.py` - scenario agent prompt
**What**: Inject "Generate exactly 1 scene, max 30 seconds" into system prompt.
**How**: Read `.directives.json` at agent build time. If constraint present, append to prompt.
**No new abstractions. No CLI args. Just prompt injection.**

### 2. Audio Timeline Path (MINIMAL)
**Where**: `server/strands_agents/stages/audio_stage.py` - `add_narration_to_timeline`
**What**: Tool reads `_timeline_path` from `os.environ` which is never set.
**How**: Use `resolve_timeline_path()` from `tools.otio_file_ops` instead of env var.
**One-line fix in _ToolCtx.**

### 3. Video Clip Submission (INVESTIGATE)
**Where**: `server/strands_agents/stages/production_stage.py` - `submit_gpu_production_job`
**What**: Production plan shows `total=1` but `submitted=0`.
**How**: Add logging to see why clips aren't submitted. Check if QA rejection blocks submission.
**Do NOT change architecture. Just add visibility.**

### 4. Assembly Tool Schema (MINIMAL)
**Where**: `server/strands_agents/stages/assembly_stage.py` - `assemble_final_cut`
**What**: Tool declares 0 parameters but implementation expects 5 positional args.
**How**: Add parameters to decorator with defaults. Match declaration to implementation.
**No logic changes. Just fix the signature mismatch.**

### 5. Debug-gym Agent (SIMPLIFY)
**Where**: `scripts/debug_gym_agent.py`
**What**: Currently auto-kills, reports false positives, adds complexity.
**How**: 
- Remove auto-kill entirely. Only human can kill.
- Remove stage loop detection (false positive factory).
- Keep: budget monitoring, VM count, error counting.
- Action: report only. No directives. No patches. No installs.

## Execution Order
1. Fix audio timeline path (blocks everything downstream)
2. Fix assembly tool schema (blocks final output)
3. Fix scenario prompt injection (reduces cost)
4. Investigate video submission (add logging)
5. Simplify debug-gym (remove kill capability)
6. Run pipeline
7. Observe. If real bug found, fix it. Repeat.

## Budget
$4.89 remaining. Target spend: <$2.00 for 1-scene run.
