> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# WhisperX tuning

Knowledge page. Updated as we accumulate tuning data from real runs.

## VRAM footprint

- Expected: ~2-4 GB for alignment (CPU-OK for small inputs).

## Use in this pipeline

- Post-TTS alignment to produce frame-accurate word timings for the
  `evaluate_timing` stage. Runs on the TTS worker VM after `/tts/render`.

<!-- Fill in after first real runs -->
