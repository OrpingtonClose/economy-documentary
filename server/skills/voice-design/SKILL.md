---
name: voice-design
description: TTS voice design techniques for documentary narration
---

# Voice Design for Documentary Narration

## Speaking Rate Control

- English: ~150 words per minute (natural documentary pace)
- Russian: ~130 words per minute (slightly slower for clarity)
- Use estimate_tts_duration tool to verify timing before committing

## Emotional Tone by Voice Role

### V1 (The Hook)
- Slightly faster delivery for urgency
- Rising intonation on key claims
- Brief pauses before revealing statements

### V2 (The Expert)
- Measured, even pace
- Clear enunciation of technical terms
- Deliberate pauses between data points

### V3 (The Storyteller)
- Natural, conversational rhythm
- Emotional emphasis through pace changes
- Longer pauses at emotional beats

## Pause Placement

- Inter-voice pauses: 0.3-0.5s between V1→V2→V3 transitions
- Inter-scene pauses: 0.8-1.2s between scenes
- Dramatic pauses: written into the text as "..." or em-dashes

## Multi-Language Considerations

For dual-language (ru/en) mode:
- Russian is the PRIMARY narration — design for Russian pacing
- English translation follows — may be slightly longer/shorter
- Each voice block format: "[RU] Russian text\n[EN] English translation"
- Video timing is based on the PRIMARY language duration
