> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Qwen3-TTS tuning

Knowledge page. Updated as we accumulate tuning data from real runs.

## VRAM footprint (placeholder — fill in after slice 4b first run)

- Expected: ~16 GB weights, peak ~18-20 GB under inference.
- Minimum recommended: A10 / L4 / 3090 (24 GB).

## Voice pinning

- **One voice per VM** is enforced at the playground worker registry
  (see `server/strands_agents/playground/worker_registry.py`). This is
  not a Qwen3-TTS limitation — it's a pipeline invariant derived from
  the stateful nature of the TTS worker's cached model state.

## Prompt shape

<!-- Fill in after first real runs -->

## Known failure modes

<!-- Fill in after first real runs -->
