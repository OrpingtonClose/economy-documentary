# ADK Environment Simulation — Design Document

## Problem

The pipeline has hand-rolled `if _TEST_MODE` checks scattered across 12 files.
ADK provides a native `EnvironmentSimulationConfig` primitive that replaces all of
this with a single, centralized configuration. This refactor:

1. Removes all `DOCUMENTARY_TEST_MODE` branching from tool implementations
2. Replaces it with ADK-native `EnvironmentSimulationConfig` scenarios
3. Enables **targeted failure injection** to test the escalation ladder at every
   error site

## Architectural Challenge

The pipeline is **hybrid**:
- **Agent-driven tools** (called by LLM through ADK tool pipeline):
  `create_timeline`, `query_lora_catalog`, `get_lora_details`, `align_narration`, `exit_loop`
- **Callback-driven functions** (called directly from Python in `before_agent_callback`):
  `generate_narration`, `generate_video_clip`, `provision_gpu_vm`, assembly functions

ADK's `EnvironmentSimulationConfig` hooks into `before_tool_callback` — it only
intercepts calls that flow through ADK's tool execution pipeline. Callback-driven
functions bypass this entirely.

### Solution: Simulation Bridge

Create a **SimulationBridge** that:
1. Holds a reference to the `EnvironmentSimulationEngine` singleton
2. For ADK tools: standard `before_tool_callback` (via `EnvironmentSimulationFactory`)
3. For callback-called functions: a `@simulated` decorator that checks the engine
   before calling the real function, using a lightweight `ToolProxy` adapter

```python
# ADK tools — standard path
agent = LlmAgent(
    ...,
    before_tool_callback=EnvironmentSimulationFactory.create_callback(config)
)

# Callback-called functions — bridge path
@simulated("generate_narration")
def generate_narration(scene_num, voice_role, text, ...):
    # Real implementation — only reached if no simulation injection matches
    ...
```

The `@simulated` decorator:
```python
def simulated(tool_name: str):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            engine = get_simulation_engine()
            if engine:
                proxy = ToolProxy(name=tool_name)
                # Build args dict from function signature
                result = asyncio.run(engine.simulate(proxy, bound_args, None))
                if result is not None:
                    return json.dumps(result) if isinstance(result, dict) else result
            return fn(*args, **kwargs)
        return wrapper
    return decorator
```

## Test Scenario Catalog

### Category A: Tool-Level Failures → Escalation

| ID | Tool | Injection | Escalation Site | Tests |
|----|------|-----------|-----------------|-------|
| A1 | generate_narration | Duration 65% of target | audio_gatekeeper | L0 agent rewrites narration (5 attempts) |
| A2 | generate_narration | HTTP 503 error | audio_narration | L0 retry → L1 different worker |
| A3 | generate_narration | Empty WAV (0 bytes) | audio_narration_clip | L0 regenerate |
| A4 | generate_video_clip | HTTP 503 (no worker) | video_worker_missing | Full L0-L3 ladder |
| A5 | generate_video_clip | CUDA OOM error | production_video_clip | L0 reduce resolution → L1 retry |
| A6 | generate_video_clip | Timeout (no response) | production_video_clip | L1 retry with longer timeout |
| A7 | generate_video_clip | QA rejected (black frames) | video_qa | L0 re-seed → L1 adjust params |
| A8 | generate_video_clip | QA rejected (style mismatch) | video_qa | L0 adjust prompt → L2 different LoRA |
| A9 | provision_gpu_vm | Empty offers list | vast_no_gpu_offers | L0 different tier → L3 wait |
| A10 | provision_gpu_vm | Insufficient credits error | vast_credits_insufficient | L4 human tops up |
| A11 | align_narration | WhisperX crash | audio alignment | Synthetic fallback |
| A12 | create_timeline | Corrupt OTIO data | timeline creation | L0 recreate |

### Category B: Gatekeeper Failures

| ID | Gate | Injection Strategy | Tests |
|----|------|--------------------|-------|
| B1 | Audio gatekeeper | TTS returns short audio (drift 15-30%) | L0 rewrites narration ×5 |
| B2 | Audio gatekeeper | TTS missing voice tracks | L0 regenerates missing |
| B3 | Production handoff | Visual concepts incomplete | L0 reruns visual direction |
| B4 | Production gatekeeper | Low clip QA pass rate (<50%) | L0 regenerates failed clips |
| B5 | Assembly handoff | Missing video clips | L0 regenerates |
| B6 | Assembly handoff | OTIO structural violations | L0 repairs timeline |

### Category C: Timeline Guardian Failures

| ID | Phase | Injection | Tests |
|----|-------|-----------|-------|
| C1 | Post-audio | Timeline file missing | Escalate → recreate |
| C2 | Post-audio | Overlapping audio clips | Escalate → repair |
| C3 | Post-production | Gaps between video clips | Escalate → fill |
| C4 | Post-assembly | Invalid track structure | Escalate → rebuild |

### Category D: Systemic/Fleet Failures

| ID | Pattern | Injection Strategy | Tests |
|----|---------|-------------------|-------|
| D1 | Cascade | 3+ sequential video failures | Fleet coordinator pause |
| D2 | Common error | Same CUDA OOM on 60%+ clips | Pattern detection |
| D3 | Performance | GPU gen time 3× baseline | Throttle detection |
| D4 | Poison clip | 1 clip fails on all workers | Dead-letter queue |
| D5 | Budget burn | Cost exceeds 80% ceiling | Fleet pause |

### Category E: Happy Paths

| ID | Scenario | Purpose |
|----|----------|---------|
| E1 | Full success | Baseline — all tools return valid mock data |
| E2 | Resume from checkpoint | Stage markers exist, skip completed stages |

### Category F: Edge Cases

| ID | Scenario | Injection Strategy | Tests |
|----|----------|-------------------|-------|
| F1 | Malformed JSON | String scene_num in TTS response | _safe_int() conversion |
| F2 | Partial success | 3/5 clips succeed, 2/5 fail | Mixed recovery |
| F3 | Agent abort | All agents return abort | Clean pipeline stop |
| F4 | Agents exhausted | All L0-L3 fail → human | L4 human escalation path |
| F5 | Flaky failures | 50% injection probability | Retry resilience |

## Implementation Plan

### Phase 1: Infrastructure
- `server/testing/__init__.py` — Package init
- `server/testing/simulation_bridge.py` — `@simulated` decorator + `ToolProxy` + `SimulationRegistry`
- `server/testing/scenarios.py` — All scenario configs as functions returning `EnvironmentSimulationConfig`

### Phase 2: Refactor Tools
- Remove `if _TEST_MODE` from: `tts_tools.py`, `video_tools.py`, `whisperx_tools.py`,
  `vastai_tools.py`, `approval_gate.py`, `contracts.py`, `worker_provisioner.py`,
  `timeline_guardian.py`, `gatekeeper.py`, `run_pipeline.py`
- Add `@simulated("tool_name")` decorator to callback-called functions
- Wire `EnvironmentSimulationFactory.create_callback(config)` into ADK agents

### Phase 3: Test Runner
- `server/testing/runner.py` — CLI: `python -m testing.runner --scenario A1`
- Loads scenario config, sets up simulation engine, runs pipeline, reports results

### Phase 4: Validation
- Run E1 (happy path) — verify synthetic movie produced
- Run A1 (audio drift) — verify L0 agent fires and attempts recovery
- Run A4 (no GPU worker) — verify full escalation ladder
