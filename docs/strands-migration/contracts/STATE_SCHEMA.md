> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# STATE_SCHEMA — canonical shapes for cross-component data

Every key written to `agent.state` or `invocation_state` has a schema
below. Implementers MUST match these shapes; LLM-produced fields that
deviate get rejected at the `ContractEnforcer` hook.

Where a current ADK file already defines the shape, this doc just points
at it.

---

## 1. `topic: str`

Free-text, 1–500 chars. Set by the user (input to the graph).

## 2. `corpus_path: str`

Absolute path to the research corpus markdown file. Produced by upstream
research agents (out of scope for this migration). Consumed by the
scenario agent.

---

## 3. `visual_style: dict`

Movie-level aesthetic directive. Produced by scenario agent.

```python
{
    "style": str,                      # e.g. "photorealistic documentary"
    "realism_anchors": list[str],      # e.g. ["4K", "no CGI"]
    "avoid": list[str],
    "palette": str,
    "camera_language": str,
    "reference_genre": str,            # matches LTX-2.3 genre tag
}
```

See [`server/agents/scenario_director.py:56-75`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/agents/scenario_director.py#L56-L75).

---

## 4. `style_lock: StyleLock`

Defined at [`server/contracts.py:88`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/contracts.py#L88):

```python
@dataclass
class StyleLock:
    dominant_style: Literal[
        "cinematic_documentary", "hand_drawn_animation", "realistic_3d",
        "stylized_2d_animation", "live_action_interview", "archival_footage",
        "mixed_media_collage", "painterly",
    ]
    forbidden_styles: list[str]
    camera_language: str
    palette: str
    realism_anchors: list[str]
    # ... additional fields; see contracts.py
```

Strict: exactly one `dominant_style`. Produced by scenario agent, consumed
by every visual component.

---

## 5. `scenes: list[dict]`

Every scene:

```python
{
    "id": int,                         # 1-indexed
    "title": str,
    "target_duration_sec": float,      # scenario-planned duration
    "visual_notes": str,
    "dopamine_hook": str,
    "voices": [                        # V1/V2/V3 narration blocks
        {"speaker": str, "text": str, "ssml": str, "voice_id": str,
         "pronunciation_hints": list[str]},
        ...
    ],
    "hook_spec": {...},                # HookSpec dict, see contracts.py
    "outro_spec": {...},               # OutroSpec dict, only on final scene
    "phrases": list[dict] | None,      # filled by content analyst later
}
```

Full schema in [`server/contracts.py:205`](https://github.com/OrpingtonClose/economy-documentary/blob/main/server/contracts.py#L205).
Scenario agent produces it; every downstream stage reads it.

---

## 6. `whisperx_alignment: dict`

Produced by audio tool, consumed by timing evaluator, visual loop, and
production supervisor:

```python
{
    "total_duration_sec": float,
    "per_scene": [
        {
            "scene_id": int,
            "duration_sec": float,
            "word_timestamps": list[{"word": str, "start": float, "end": float}],
            "wav_path": str,           # absolute path, uploaded to B2
            "b2_url": str,
            "voice_id": str,
        },
        ...
    ],
    "language": str,
}
```

---

## 7. `timing_passed: bool` and `timing_report: dict`

Produced by timing evaluator:

```python
state["timing_passed"] = True | False
state["timing_report"] = {
    "target_duration_sec": float,
    "actual_duration_sec": float,
    "deviation_ratio": float,          # (actual - target) / target
    "per_scene_analysis": [
        {"scene_id": int, "target": float, "actual": float, "deviation": float, "ok": bool},
        ...
    ],
    "violations": list[str],           # e.g. ["scene 3 exceeds target by 18%", "total short by 12%"]
}
```

Consumed by scenario refiner (short-circuits when `timing_passed`).

---

## 8. `content_analysis: dict`

Produced by content analyst:

```python
{
    "per_scene": [
        {
            "scene_id": int,
            "phrases": [
                {
                    "phrase_id": str,                # stable across re-runs
                    "text": str,
                    "phrase_type": Literal["concept", "entity", "process", "transition", "data"],
                    "narrative_weight": Literal["hook", "build", "payoff", "connective"],
                    "visual_intent": str,
                    "word_span": [int, int],         # indices into voices[].text
                    "time_span": [float, float],     # from whisperx_alignment
                }, ...
            ],
        }, ...
    ],
}
```

---

## 9. `visual_concepts: list[dict]`

Produced by visual concepter:

```python
[
    {
        "phrase_id": str,
        "scene_id": int,
        "camera_movement": str,
        "shot_type": str,
        "prompt": str,                  # LTX-2.3 prompt
        "negative_prompt": str,
        "duration_sec": float,
        "style_lock_applied": bool,     # hook: RevisionTagger stamps this
        "ltx_params": {...},            # resolution, seed, steps
    }, ...
]
```

---

## 10. `clip_artifacts: list[dict]`

Produced by production supervisor (GPU dispatch):

```python
[
    {
        "phrase_id": str,
        "scene_id": int,
        "mp4_path": str,
        "b2_url": str,
        "duration_sec": float,
        "qa_verdict": Literal["pass", "warn", "escalate", "fail"],
        "qa_notes": str,
        "worker_url": str,
        "render_seconds": float,
    }, ...
]
```

---

## 11. `recovery_log: list[dict]`

Append-only log maintained by recovery agents + escalation supervisor:

```python
[
    {
        "t": float,                    # unix timestamp
        "artifact_type": str,          # "scene", "clip", "audio", ...
        "artifact_id": str,
        "action": Literal["retry", "fix", "skip", "escalate", "abort"],
        "reason": str,
        "agent": str,                  # which agent made the decision
        "diagnostic_ref": str | None,  # pointer into CritiqueStore
    }, ...
]
```

Consumed by `EscalationDecisionEvaluator` for scoring decisions post-hoc.

---

## 12. `final_output: dict`

Produced by assembly tool:

```python
{
    "mp4_path": str,                   # final assembled documentary
    "b2_url": str,
    "duration_sec": float,
    "scene_count": int,
    "otio_path": str,                  # pre-ffmpeg OTIO timeline
}
```

Terminal state. Written once, never mutated.

---

## Placement: `agent.state` vs `invocation_state`

| Key | Lives in | Rationale |
|-----|----------|-----------|
| `topic`, `corpus_path`, `target_duration_sec`, `output_dir` | `invocation_state` | Read-only run context set at `graph.invoke`. |
| `scenes`, `whisperx_alignment`, `visual_concepts`, `clip_artifacts`, `final_output` | `agent.state` of the node producing it, copied to `invocation_state` by the node's `AfterInvocationEvent` hook | Write once per stage, visible to downstream nodes. |
| `timing_passed`, `timing_report` | `invocation_state` | Read by the timing-loop cycle edge condition. |
| `recovery_log` | `invocation_state` | Append-only, every node may touch it. |
| Conversation buffers, per-agent scratch | `agent.state` (per-agent) | Local, never read across nodes. |

Rule: `invocation_state` only holds cross-node data; everything else stays
on the owning agent. If an LLM wants to read it, the owning node must
explicitly republish it via a hook — no implicit blackboard leaks.
