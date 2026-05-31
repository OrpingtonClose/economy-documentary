> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# 04 — audio-agent

**Not an agent.** A single deterministic `@tool` function (with a small
sub-call to WhisperX).

---

## Intent

Given `scenes: list[dict]`, produce:

- One `.wav` per scene (composite V1+V2+V3 narration at master loudness).
- `whisperx_alignment: dict` with per-scene word-level timestamps.
- All artifacts uploaded to B2.

Fails loud on any TTS or WhisperX failure — no silent degradation,
no placeholder audio.

---

## Current implementation

[`server/agents/audio_agent.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/agents/audio_agent.py)
dispatches to
[`server/tools/tts_tools.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/tools/tts_tools.py)
and
[`server/tools/whisperx_tools.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/tools/whisperx_tools.py),
with loudness normalization from
[`server/tools/loudness_normalization.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/tools/loudness_normalization.py)
and the "deterministic audio step" logic in
`server/callbacks/deterministic_steps.py`.

---

## Strands implementation

Single module: `server/strands_agents/audio_tool.py`.

```python
from strands import tool

@tool(context=True)
async def render_audio(
    context, scenes: list[dict], voice_map: dict[str, str] | None = None,
) -> dict:
    """Synthesize per-scene narration and align via WhisperX.

    Uploads every wav to B2 before returning.  On any TTS / alignment /
    upload failure raises RuntimeError — this is a hard failure.
    """
    from tools.tts_tools import generate_scene_tts
    from tools.whisperx_tools import align_scene
    from tools.b2_checkpoint import upload_to_b2
    from tools.loudness_normalization import normalize_to_target

    per_scene: list[dict] = []
    for scene in scenes:
        wav_path = await generate_scene_tts(scene, voice_map=voice_map)
        normalize_to_target(wav_path, target_lufs=-23.0)
        word_ts = await align_scene(wav_path, scene)
        b2_url = upload_to_b2(wav_path)                 # raises on failure
        per_scene.append({
            "scene_id": scene["id"],
            "wav_path": wav_path,
            "b2_url": b2_url,
            "duration_sec": word_ts["duration_sec"],
            "word_timestamps": word_ts["words"],
            "voice_id": scene["voices"][0]["voice_id"],
        })

    alignment = {
        "total_duration_sec": sum(s["duration_sec"] for s in per_scene),
        "per_scene": per_scene,
        "language": "en",
    }
    context.invocation_state["whisperx_alignment"] = alignment
    return {"whisperx_alignment": alignment, "wav_paths": [s["wav_path"] for s in per_scene]}
```

### Why a tool, not an agent

Same reasoning as component 02. Audio synthesis is deterministic given
scenes + voice map. Adding an LLM adds latency, cost, and
non-determinism without value.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    ContractComplianceEvaluator(AUDIO_CONTRACT),       # hard gate
    AudioInvariantEvaluator(),                         # hard gate (7 invariants)
    CritiqueStoreEvaluator(artifact_type="audio"),     # soft
]
```

### Test cases (minimum 5)

| Case name | Input | Expected behaviour |
|-----------|-------|--------------------|
| `basic_3_scenes` | 3 scenes, standard voice map | 3 wavs uploaded; all 7 invariants pass |
| `long_scene_45s` | 1 scene at 45 s | wav duration within ±1 s of 45 s |
| `multi_voice_blocks` | scene with V1, V2, V3 blocks | composite wav has all three voices; voice_id consistency invariant passes |
| `tts_transient_failure` | TTS fails once then recovers | retry succeeds; artifact uploaded |
| `tts_persistent_failure` | TTS fails 3x | tool raises `RuntimeError`; no wav uploaded |

### Simulators

`TTS_WORKER_SIMULATOR` from
[`SIMULATION.md §2`](../eval-framework/SIMULATION.md#2-tts-worker-simulator).
Real TTS hit only in the nightly workflow (see
[`CI_PIPELINE.md §4`](../eval-framework/CI_PIPELINE.md#4-nightly-run-real-integrations)).

### Thresholds

- `ContractComplianceEvaluator` = 1.00 (hard)
- `AudioInvariantEvaluator` = 1.00 (hard)
- `CritiqueStoreEvaluator` ≥ 0.75 (soft)

---

## File layout

```
server/strands_agents/
├── audio_tool.py                             # ~150 LOC (thin orchestration)
└── evals/
    ├── simulators/
    │   └── tts_worker_simulator.py
    ├── evaluators/
    │   └── audio_invariant_evaluator.py
    └── experiments/
        └── audio_experiment.json
```

---

## Acceptance criteria

- [ ] `render_audio` unit-tested against the TTS simulator.
- [ ] All 7 audio invariants from
      [`server/critique/audio_invariants.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/critique/audio_invariants.py)
      pass on `basic_3_scenes`.
- [ ] Hard-failure path: `tts_persistent_failure` raises `RuntimeError`, no partial state persisted.
- [ ] B2 upload called exactly once per scene on success path.
- [ ] Experiment passes thresholds against simulator; nightly passes against real worker.
