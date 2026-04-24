# Timing drift

Knowledge page. Placeholder until the first full pipeline run produces
real timing data.

## Expected failure surface

- WhisperX alignment drifting from declared scene durations.
- Accumulated per-scene rounding producing a duration mismatch at
  assembly time.
- Scene-boundary silence padding being consumed by TTS overflow.

<!-- Fill in after first full pipeline runs -->
