> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# V5 Architecture — Effect Types & State Machine

> Concrete implementation of the V5 abstract architecture.

---

## Effect Type Family

### How Scenes / Shots / Clips Map to OTIO

From the deep git history (`server/otio_timeline_model.py`, `server/models/scene.py`):

- A **Scene** has `scene_num` (1-based), `voices` (list of VoiceLine), `visual_description`, `duration_sec`
- Each **VoiceLine** is a `phrase` on track **A1_Narration**
- Each scene also has a matching slot on track **V1_Video**
- Slot identity: `{track}:{scene_num}:{phrase_idx}` (e.g. `A1:3:2`)
- Three canonical tracks: **V1_Video**, **A1_Narration**, **A2_Music**
- Gaps exist for pacing and are not "missing content"

### Effect Categories (23 types)

| Category | Effect | Producer | Meaning |
|---|---|---|---|
| **Script** | `UpdateScript` | Scenario | Write/revise scene narration |
| | `DeleteScene` | Scenario | Remove a scene |
| | `ReorderScenes` | Scenario | Change scene order |
| **Jobs** | `QueueJob` | Audio/Video | Demand TTS or LTX generation |
| | `JobCompleted` | Provisioner | VM finished, artifact ready |
| | `JobFailed` | Provisioner | VM failed |
| | `JobRequeued` | Audio/Video | Artistry rejection → retry |
| | `JobApproved` | Audio/Video | Artistry approval → ready for OTIO |
| **VMs** | `VMAllocated` | Provisioner | GPU instance created |
| | `VMDeallocated` | Provisioner | GPU instance destroyed |
| | `VMProvisionFailed` | Provisioner | Could not create VM |
| | `VMObserved` | VM Projection | Vast.ai truth differs from events |
| **OTIO** | `MergeIntoOTIO` | Audio/Video | Approved clip enters timeline |
| | `DeleteFromOTIO` | Audio/Video | Remove clip from timeline |
| **Pipeline** | `PipelineStarted` | Launcher | Run begins |
| | `TransitionState` | State Machine | Phase changed |
| | `PipelineComplete` | Assembly | Final MP4 done |
| | `PipelineAborted` | Any | Unrecoverable stop |
| **Bash** | `ExecuteRawBash` | Any | Escape hatch |
| **Human** | `HumanInstruction` | Overseer | Human posted to agent |
| | `ClarificationRequest` | Parser | Parse failed |
| | `AgentLoopDetected` | State Machine | Agent stuck in loop |
| **Fallback** | `NoOp` | Any | Informational |

### Key Design: Narration Reconciliation (Authoritative OTIO is Born Here)

This was Stage Two in the old mermaid diagrams. The Audio agent does this iteratively:

```
Audio Agent → QueueJob (TTS) → Provisioner → VM → TTS generates WAV
     ↑                                                            |
     |←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←|
     |                                                             |
     |←← WhisperX measures actual duration ←←←←←←←←←←←←←←←←←←←←←←←|
     |                                                             |
     |←← Compare measured vs scripted (±15% or ±0.25s tolerance) ←|
     |                                                             |
     |←← Within tolerance? → DurationAdjusted (update OTIO) ←←←←←|
     |                                                             |
     |←← Outside tolerance? → JobRequeued (new text/pacing) ←←←←←|
     |                                                             |
     |←← ALL blocks pass? → ReconciliationComplete ←←←←←←←←←←←←←|
```

**Only after `ReconciliationComplete` does the OTIO become authoritative.** Video production uses the measured durations as LAW.

### Key Design: Job Lifecycle

```
Audio Agent          Provisioner            VM Worker            Audio Agent
    |                    |                      |                      |
    |-- QueueJob ------->|                      |                      |
    |   (demand TTS)     |                      |                      |
    |                    |-- VMAllocated ------>|                      |
    |                    |   (create GPU)       |                      |
    |                    |<-- JobCompleted -----|                      |
    |                    |   (artifact ready)   |                      |
    |<-- POST / ---------|                      |                      |
    |   ("job 123 done") |                      |                      |
    |                    |                      |                      |
    |-- JobApproved ----------------------------->|   (or JobRequeued)  |
    |   (artistry pass)  |                      |                      |
    |                    |                      |                      |
    |-- MergeIntoOTIO --->|                      |                      |
    |   (enter timeline) |                      |                      |
```

**The Provisioner is the lackey.** He provisions, reports facts, and delivers results. The media agent judges artistry.

---

## State Machine

### States

```
[INIT] → [SCRIPT] → [AUDIO_VIDEO] → [ASSEMBLY] → [DONE](final)
            ↑__________|
```

| State | Active Agents | What Happens |
|---|---|---|
| **INIT** | Scenario | Pipeline starts. Scenario agent writes first script. |
| **SCRIPT** | Scenario | Refine script until all narration slots are complete. |
| **AUDIO_VIDEO** | Audio, Video, Provisioner | **Phase 1 — Audio Reconciliation:** Audio agent generates TTS, WhisperX measures, compares to scripted, adjusts OTIO iteratively until all blocks pass. `ReconciliationComplete` makes OTIO authoritative. **Phase 2 — Video Production:** Video agent generates LTX using measured durations as LAW. Approved clips merged into OTIO. |
| **ASSEMBLY** | Last active media agent | ffmpeg combines all OTIO clips into `final_documentary.mp4`. OTIO validation runs. |
| **DONE** | None | Pipeline complete. |

### Transitions (tick-driven)

| From | To | Guard | Condition |
|---|---|---|---|
| INIT | SCRIPT | (always) | Pipeline started |
| SCRIPT | AUDIO_VIDEO | `script_has_scenes` | A1_Narration has at least some non-gap slots |
| SCRIPT | SCRIPT | `script_incomplete` | Some slots exist but gaps remain |
| AUDIO_VIDEO | ASSEMBLY | `all_media_produced` | `ReconciliationComplete` exists AND 0 pending/running jobs AND all OTIO slots delivered |
| AUDIO_VIDEO | SCRIPT | `media_failed_due_to_script` | Failures blame script (`gap_unexpected`, `voice_mismatch`, or job error text) |
| AUDIO_VIDEO | AUDIO_VIDEO | `audio_still_reconciling` | Reconciliation not yet complete |
| AUDIO_VIDEO | AUDIO_VIDEO | `video_still_pending` | Reconciliation done but video jobs still active |
| ASSEMBLY | DONE | `assembly_valid_and_complete` | MP4 exists + NO unresolved `ProductionFailed` + OTIO validates (no overlaps, aligned tracks, valid media) |
| ASSEMBLY | ASSEMBLY | `assembly_in_progress` | MP4 missing or validation failed |

### Cyclic Transitions

- **SCRIPT ↔ AUDIO_VIDEO**: The back-edge from old mermaid. When jobs fail due to script errors, the state machine returns to SCRIPT for rewrite.
- No other back-edges (QA jury, preference ledger, preview assemblies removed per user).

### OTIO Validation in ASSEMBLY Guard

The `assembly_valid_and_complete` guard runs three checks from the OTIOProjection:

1. **`validate_no_overlaps()`** — Clips don't overlap without transitions between them
2. **`validate_track_alignment()`** — Timeline duration equals max track duration
3. **`validate_clip_media()`** — All clips have media references and valid ranges

These are the same checks from `server/otio_timeline_model.py` and the OTIO validation research.

### Watcher Loop

```python
async def watcher():
    while True:
        for proj in projections:
            proj.tick()          # Process new events
        machine.tick()           # Evaluate guards, fire transitions
        await asyncio.sleep(1)   # Throttle
```

### State Instructions (Prompt Injection)

When the state machine enters a state, the corresponding instruction is injected into the active agent's prompt (the R slot in T/M/D/R/W):

| State | Instruction |
|---|---|
| init | Write a narration script with scenes, speakers, timing. Report with Kind: update_script. |
| script | Refine narration. Focus on pacing, speaker consistency, duration targets. Report with Kind: update_script. |
| audio_video | Produce media. Formulate jobs (Kind: queue_job). Judge output. Approve (Kind: job_approved) or reject (Kind: job_requeued). Merge approved clips (Kind: merge_into_otio). |
| assembly | Use ffmpeg to combine all clips into final_documentary.mp4. Verify duration matches timeline. |
| done | Pipeline complete. |

---

## Files

| File | Purpose |
|---|---|
| `server/v5/effects.py` | 23 effect types + Pydantic discriminated union + kind router |
| `server/v5/state_machine.py` | python-statemachine with 5 states, tick-driven guards, OTIO validation, watcher loop |

---

*Version: 2026-05-17 v5*
