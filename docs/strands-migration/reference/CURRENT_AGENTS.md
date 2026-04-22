# CURRENT_AGENTS — inventory of `server/agents/`

Quick index so implementers can jump straight to the current behaviour a
given component must replicate.

| File | LoC | Role | Target component |
|------|-----|------|------------------|
| `assembler_agent.py` | ~200 | Deterministic OTIO → ffmpeg final assembly | 11 |
| `audio_agent.py` | ~150 | TTS + WhisperX orchestration | 04 |
| `chat_narrator.py` | ~100 | Run-log narrator for the dashboard | Out of scope (UI) |
| `diagnostic_classifier.py` | ~120 | Classifies failures as transient/fixable/persistent/catastrophic | 12 |
| `escalation_supervisor.py` | ~180 | Top-level escalation decisions | 13 |
| `intent_extractor.py` | ~90 | Parses user intent from chat | Out of scope |
| `model_config.py` | 167 | `build_model()` factory + role env vars | `server/strands_agents/models.py` (see ARCHITECTURE § 4) |
| `pipeline.py` | 1 111 | SequentialAgent + monkey-patched callbacks | 14 (distributed into `GraphBuilder` + hooks) |
| `preference_interpreter.py` | ~140 | Reads Preference Ledger, applies overrides | HookProvider in `server/strands_agents/hooks/preferences.py` |
| `preview_critic.py` | ~160 | Dashboard-side preview QA | Out of scope (moves to 12 recovery agents + 13) |
| `production_supervisor.py` | ~260 | GPU dispatch, escalation ladder | 10 |
| `remanifestation_agent.py` | ~180 | Re-generates a failed artifact with preserved context | 12 |
| `scenario_director.py` | 608 | LoopAgent(generator + evaluator) | 01 |
| `scenario_refiner.py` | ~180 | Timing-aware scene tweaks | 03 |
| `timing_evaluator.py` | ~240 | Deterministic duration check | 02 |
| `visual_director.py` | ~830 | LoopAgent(content_analyst + visual_concepter + coherence_evaluator) | 06, 07, 08, 09 |
| `whisperx_agent.py` | ~130 | Direct WhisperX API wrapper | Merged into 04 |

Total: ~4 500 LoC of agent code becomes ~1 800 LoC of Strands agents +
`~600 LoC` of hooks. Target: ≥ 60% reduction in agent surface area.
