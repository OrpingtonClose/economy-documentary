> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Analysis: Will the Pipeline Produce a Movie?

## Three Tools Analyzed

1. **agentic-codebase (acb)** — Compiled graph analysis of 417 files, 6,775 units, 10,020 edges
2. **pyright + ruff** — Static type analysis: 0 errors, 0 lint errors
3. **wirelessr/codebase-analyzer-agent** — AutoGen multi-agent exploration (terminated after 5 iterations, no final result — see below for human synthesis)

---

## Verdict: CONDITIONAL YES

The pipeline **can** produce a movie. The architecture is complete. But **5 critical blockers** determine whether it actually will on the next run.

---

## What Works (The Architecture)

### 1. End-to-end graph exists
```
scenario → otio_gate → audio → otio_gate → video → otio_gate → assembly → otio_gate
```
- `build_documentary_graph()` constructs a 5-node `pydantic-graph`
- `RecoveryShell.run()` executes with retry logic
- `run_documentary()` verifies `master.mp4` exists before declaring success

### 2. Assembly is real
- `assembly_tools.assemble_documentary()` (line 253) is the actual implementation
- It calls `concat_clips()` + `mux_audio_video()` to produce the final MP4
- `run_strands.py` checks `os.path.exists(master_mp4)` and reports file size

### 3. Audio pipeline works
- TTS via `generate_scene_narration()` → WAV per scene
- WhisperX alignment via `align_narration_audio()`
- Clips added to OTIO timeline via `add_narration_to_timeline()`

### 4. Video pipeline works
- LTX video generation via `submit_gpu_production_job()`
- Clips added to OTIO via `add_video_clip_to_timeline()`

### 5. Static analysis is clean
- **Pyright**: 0 errors (8 warnings, all missing optional deps)
- **Ruff**: 0 errors
- Typed extraction layer is built and integrated

---

## What Blocks Success (The 5 Critical Issues)

### Blocker 1: Tool Stubs (HIGH — WILL FAIL)
**Files:** `server/tools/video_tools.py`, `server/tools/assembly_tools.py`

Three functions were added as stubs during type-fixing:
- `probe_clip()` — raises `NotImplementedError`
- `mux_audio_video()` — raises `NotImplementedError`
- `normalize_audio_loudness()` — raises `NotImplementedError`

**Impact:** The assembly stage WILL crash when it tries to mux audio+video.

**Fix:** Replace stubs with real `ffmpeg` subprocess calls.

### Blocker 2: VM Provisioning Reliability (MEDIUM — MAY FAIL)
**Files:** `server/strands_agents/graph_pipeline.py`, `server/strands_agents/shared_a2a/vast_provisioning.py`

The audio and video agents provision VMs via `bash_command` tool. The agent:
- Parses raw `vastai` CLI text
- Decides what to do
- May provision wrong GPU, wrong disk size, or give up

**Mitigation added:** VM registry tools (`query_vm_registry`, `check_worker_health`, `get_provisioning_guidance`) are now in the agent's tool list. The system prompt instructs the agent to check registry before provisioning.

**But:** The agent still COULD ignore the guidance. The registry is new and untested in a real run.

### Blocker 3: Worker Boot Time (UNKNOWN — MAY TIME OUT)
**Files:** Worker bootstrap scripts, `gpu_worker.py`

TTS (Qwen3) and video (LTX) workers download models on first boot. No timing data exists. The agent's system prompt says "Wait 2 minutes" — this may not be enough.

**Status:** Unmeasured. The plan says "Phase 1: measure first."

### Blocker 4: No Mid-Stage Resume (MEDIUM — WASTES MONEY)
**Files:** `server/strands_agents/graph_pipeline.py`, `server/strands_agents/run_strands.py`

If DeepSeek drops connection mid-stream:
- `RecoveryShell` retries from the **beginning**
- Audio agent re-runs, potentially re-provisioning a second VM
- First VM is orphaned and bills indefinitely

**Mitigation:** SnapshotHook is now wired. It records every tool call to SQLite. But resume logic (`resume=True`) is not yet implemented.

### Blocker 5: Agent Ignores Registry Guidance (LOW — THEORETICAL)
**Files:** `server/strands_agents/graph_pipeline.py`

The agent's system prompt now says "Query VM registry before provisioning." But LLM agents can ignore instructions. If the agent decides to `bash_command("vastai create instance ...")` directly, the registry can't stop it.

---

## acb Prophecy: Top Risks

From `acb query documentary.acb prophecy --limit 15`:

| Risk | Function | Complexity | Issue |
|------|----------|-----------|-------|
| 0.40 | `tool_executor._execute_tool_inner` | 106 | No test coverage, highest complexity |
| 0.40 | `assembly_tools.assemble_documentary` | 39 | No test coverage |
| 0.40 | `analyze.run_verification_subagent` | 36 | No test coverage |
| 0.40 | `provisioner_tools.create_instance` | 30 | No test coverage |
| 0.40 | `search_tools2.tool_youtube_video_metadata` | 30 | No test coverage |

**Key insight:** `assemble_documentary` is in the top 3 riskiest functions — and it has stub dependencies.

---

## What Must Happen Before the Next Run

### Required (or it WILL fail)
1. **Replace tool stubs with real ffmpeg calls**
   - `probe_clip` → `ffprobe -v quiet -print_format json -show_streams`
   - `mux_audio_video` → `ffmpeg -i video -i audio -c copy output.mp4`
   - `normalize_audio_loudness` → `ffmpeg -i input -af loudnorm output`

### Strongly Recommended
2. **Test VM registry tools** with a real Vast.ai provisioning
3. **Measure worker boot time** for TTS and video workers
4. **Add retry to AsyncOpenAI client** (`max_retries=3`) for connection drops

### Nice to Have
5. **Implement stage-level resume** (skip completed stages on restart)
6. **Add worker boot timing** to worker HTTP response

---

## wirelessr/codebase-analyzer-agent Status

**Terminated after 5 iterations.** The AutoGen agent successfully:
- Listed the project directory
- Found Python files
- Read `pipeline/` and `.agents/skills/` files
- Searched for video/render/ffmpeg keywords

**Did NOT complete:** The agent entered an exploration loop without converging on a conclusion. It was looking at old `pipeline/` code (from a previous architecture) rather than the active `server/` codebase.

**Root cause:** The `pipeline/` directory contains legacy code. The active code is in `server/`. The agent had no way to know which was current.

---

## Final Answer

> **Will the pipeline produce a movie?**

**Not yet.** The architecture is sound, the types are clean, the extraction layer is built. But three `NotImplementedError` stubs in the assembly path guarantee a crash.

**After replacing the stubs with ffmpeg calls:** Yes, with high probability, assuming:
- DeepSeek connection stays stable
- Agent follows VM registry guidance
- Workers boot within agent timeout

**Confidence: 70% after stub fix, 30% as-is.**
