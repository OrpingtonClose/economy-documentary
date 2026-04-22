# 11 — assembly-agent

**Not an agent.** A single deterministic `@tool` function that composes
the final `.mp4` from clip artifacts, OTIO timeline, and per-scene
audio.

---

## Intent

Given `clip_artifacts`, `scenes`, `whisperx_alignment`, and the OTIO
timeline produced in 01, produce `final_output: dict` (see
[`STATE_SCHEMA.md § 12`](../contracts/STATE_SCHEMA.md#12-final_output-dict)).

Hard invariants (from current `strict_assembler.py` callback):

- No gaps in the timeline.
- Every scene has both audio and video tracks.
- Final duration within ±2 s of sum-of-scene-durations.
- Upload `final_output.mp4` to B2 before returning.

---

## Current implementation

[`server/agents/assembler_agent.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/agents/assembler_agent.py)
+
[`server/tools/assembly_tools.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/tools/assembly_tools.py)
+
[`server/tools/otio_tools.py`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/tools/otio_tools.py)
+ `server/callbacks/strict_assembler.py` +
`server/callbacks/narration_reconciliation.py`.

---

## Strands implementation

Single module: `server/strands_agents/assembly_tool.py`.

```python
from strands import tool

@tool(context=True)
async def assemble_final_cut(
    context,
    scenes: list[dict],
    clip_artifacts: list[dict],
    whisperx_alignment: dict,
    timeline_path: str,
    output_dir: str,
) -> dict:
    """Build the final documentary mp4."""
    from tools.otio_tools import load_timeline, write_timeline
    from tools.assembly_tools import render_final
    from tools.title_cards import prepend_title, append_outro
    from tools.validation_tools import validate_otio_compliance
    from tools.b2_checkpoint import upload_to_b2

    tl = load_timeline(timeline_path)
    tl = _apply_clips_to_timeline(tl, clip_artifacts)
    tl = _apply_audio_to_timeline(tl, whisperx_alignment)
    tl = prepend_title(tl, scenes[0])
    tl = append_outro(tl, scenes[-1])

    report = validate_otio_compliance(tl)
    if not report["ok"]:
        raise RuntimeError(f"OTIO compliance failed: {report['violations']}")

    final_otio = f"{output_dir}/final.otio"
    write_timeline(tl, final_otio)
    final_mp4 = render_final(tl, output_dir)            # ffmpeg
    b2_url = upload_to_b2(final_mp4)

    final_output = {
        "mp4_path": final_mp4,
        "b2_url": b2_url,
        "duration_sec": sum(s["target_duration_sec"] for s in scenes),
        "scene_count": len(scenes),
        "otio_path": final_otio,
    }
    context.invocation_state["final_output"] = final_output
    return final_output
```

All prior logic (title cards, narration reconciliation, OTIO validation,
strict assembler checks) ports into the helpers above.

---

## Evals harness

### Evaluator stack

```python
evaluators = [
    ContractComplianceEvaluator(ASSEMBLY_CONTRACT),
    TimelineComplianceEvaluator(),
    Equals("final_output_has_b2_url", True),
]
```

### Test cases (minimum 5)

| Case name | Input | Expected |
|-----------|-------|----------|
| `clean_3_scenes` | 3 scenes, 3 clips, clean alignment | final mp4, duration within ±2 s |
| `missing_clip` | 3 scenes, 2 clips | raises `RuntimeError` with missing-scene detail |
| `gap_in_timeline` | synthetic gap | OTIO validation flags gap; raise |
| `outro_missing` | scenes without outro_spec | appends default outro card |
| `b2_upload_fails` | upload raises | tool raises; no `final_output` in state |

### Simulators

None for unit tests (ffmpeg runs locally). B2 upload mocked via
`unittest.mock`.

### Thresholds

- `ContractComplianceEvaluator` = 1.00 (hard)
- `TimelineComplianceEvaluator` = 1.00 (hard)

---

## File layout

```
server/strands_agents/assembly_tool.py            # ~200 LOC
```

---

## Acceptance criteria

- [ ] Every successful case has `final_output.b2_url` populated.
- [ ] No partial artifact if any invariant fails.
- [ ] Bit-exact same input → same output mp4 hash (ffmpeg deterministic flags set).
- [ ] Experiment passes thresholds.
