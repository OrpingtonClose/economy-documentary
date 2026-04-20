# CURRENT_TOOLS — inventory of `server/tools/`

Every tool below either becomes a Strands `@tool` function or migrates
verbatim as a pure helper imported from a `@tool`. Line counts in the
current repo.

| File | Role | Migration |
|------|------|-----------|
| `assembly_tools.py` | OTIO + ffmpeg composition | Port to `@tool` in 11; ffmpeg helpers stay plain functions |
| `b2_checkpoint.py` | B2 upload helper (Backblaze) | Pure helper; import from multiple `@tool`s |
| `lora_catalog.json` | LoRA model metadata | Static data; no migration |
| `lora_tools.py` | LoRA selection for GPU worker | Import from `@tool` in 10 |
| `loudness_normalization.py` | LUFS target application | Pure helper; called from `audio_invariants.py` and the audio tool |
| `master_profiles.py` | Character/voice master profiles | Pure helper; imported by 03, 04 |
| `otio_moments.py` | OTIO scene/clip range helpers | Pure helper; used by 11, 14 |
| `otio_tools.py` | OTIO timeline creation (`create_timeline_tool`) | Becomes `@tool create_timeline` in 01, 05, 11 |
| `qa_jury.py` | Multi-critic QA voting | Port to `@tool` in 10; feeds `CritiqueStoreEvaluator` |
| `scenario_evaluator_checks.py` | Deterministic scenario checks | Wrapped by `ScenarioQualityEvaluator` and used via `@tool evaluate_scenario` in 01 |
| `title_cards.py` | Title/outro card rendering | Pure helper for 11 |
| `tts_ssml_smoke.py` | SSML smoke test | Unit test under 04 |
| `tts_tools.py` | TTS worker client | `@tool generate_tts` in 04 |
| `validation_tools.py` | OTIO compliance validation | Used by `TimelineComplianceEvaluator`; `@tool validate_timeline` in 11 |
| `vastai_tools.py` | vast.ai instance management | Keep as-is, called from 10's recovery agents |
| `video_tools.py` | Video worker client | `@tool dispatch_video_job`, `@tool check_job_status` in 10 |
| `whisperx_tools.py` | WhisperX client | `@tool align_whisperx` in 04 |

Policy: anything that is a pure helper stays a module-level function.
Only the things the LLM actually calls become `@tool`s. The current
`FunctionTool(create_timeline_tool)` pattern over every callable is the
wrong direction — Strands makes the `@tool` decorator cheap enough that
we should be selective.
