# Documentary Pipeline TODOs

- [ ] Secure API communication protocol with cloud VMs
- [ ] Implement duration-aligned narration validation and model-based coherence evaluation for the recovery ladder
  - [ ] Replace simple string-length validation checks with duration alignment heuristics based on speech rate (e.g., 2.5–3.0 words/sec)
  - [ ] Integrate an LLM-as-judge framework (e.g., `confident-ai/deepeval` G-Eval) to verify semantic faithfulness, narrative flow, and stylistic QA invariants after rewriting blocks
  - [ ] Investigate zero-shot duration-controlled TTS systems (e.g., `index-tts/index-tts`) to control prosody and timing directly at generation time
  - [ ] Explore multi-agent script polishing tools and Omni-modal LLMs (e.g., `AJaySi/alwrity_yt_script`, `TencentARC/OmniScript`) for maintaining long-form narrative coherence
- [ ] Leverage the entire full feature set of Qwen-TTS:
  - [ ] Implement text-described voice design via `generate_voice_design(..., instruct="...")` using the `VoiceDesign` variant model to synthesize custom speaker profiles
  - [ ] Support zero-shot voice cloning via `generate_voice_clone` with reference audio prompt inputs (e.g., 3-second wav clip) for dynamic speaker replication
  - [ ] Implement Qwen-TTS Prosody and Pacing Control: Support specifying explicit speech rate, tone/pitch variation, volume, and emotional-expressive prompts (e.g., whispering, shouting, tense, excited) via formatted voice-instruct prompts to dynamically stylize dialogue delivery based on narrative context (like scene tension and target duration constraints)
  - [ ] Optimize GPU pipeline by adopting the "Voice Design then Clone" pattern: generate speaker prompts via `VoiceDesign`, then reuse prompts with the `Base`/`CustomVoice` model for subsequent script generation
  - [ ] Leverage native multilingual capabilities (English, Chinese, Japanese, Russian, Portuguese, etc.) for localized speaker dialogue
- [ ] Support alternative engines and fast-draft fallbacks:
  - [ ] Integrate an option to use Grok TTS as an alternative/fallback speech synthesis engine
  - [ ] Integrate an option to use older LTX-Video model variants for quick, low-fidelity generation of symbolic draft visuals that do not need to be sharp
- [ ] Implement problem escalation patterns using POST/PUT API boundary:
  - [ ] Support agents programmatically escalating issues by posting/putting external instructions or help request events when encountering unresolvable errors
  - [ ] Refactor `HumanInstruction` events to represent general external intervention / problem escalation, rather than being strictly operator-exclusive

