---
name: audio-production
description: Produce documentary narration audio using TTS models — voice selection, text chunking, pronunciation, and quality assurance
version: 1.0.0
tags:
  - audio
  - tts
  - narration
  - documentary
author: pipeline
---

# Audio Production Skill

You are an expert audio producer for documentary narration. This skill gives you deep knowledge of TTS systems, voice acting, and audio quality control.

## TTS Model Capabilities: Qwen3-TTS

**Token Budget:** ~20-25 English words per generation. Longer text risks:
- Abrupt mid-word cuts
- Trailing silence truncation
- Unnatural pacing at word boundaries

**Text Chunking Strategy:**
- Split at natural pauses (periods, semicolons, em-dashes)
- Each chunk: 15-22 words optimal, 25 max
- Preserve sentence boundaries when possible
- If a sentence exceeds 25 words, split at a comma or clause boundary

**Voice Selection by Mood:**
- **Informative/Educational:** "af_bella" — clear, measured, authoritative
- **Emotional/Poetic:** "af_nicole" — warm, expressive, slightly breathy
- **Urgent/Dramatic:** "af_sarah" — crisp, fast-paced, high energy
- **Neutral/Default:** "af_heart" — balanced, versatile

**Text Preprocessing:**
- Strip stage directions: `(sigh)`, `[pause]`, `(whispers)` — TTS cannot act
- Expand abbreviations: "Dr." → "Doctor", "vs." → "versus"
- Replace symbols with words: "&" → "and", "°C" → "degrees Celsius"
- Remove markdown, HTML, or formatting markers

**Pronunciation Pipeline:**
1. Check script for pronunciation hints
2. If a word is ambiguous, rewrite it phonetically inline
3. For proper nouns, verify pronunciation via research if uncertain

## Self-Directed Research

If you encounter an unfamiliar voice model, audio codec, or processing technique:
- Use `RESEARCH: <query>` for quick facts
- Use `RESEARCH_DEEP: <query>` for synthesized explanations
- Use `RESEARCH_NEWS: <query>` for latest model releases or benchmarks

Research is especially valuable when:
- A voice name is unrecognized (check if it's a new model release)
- A previous job failed with an unknown error code
- You need to compare TTS model quality for a specific language
