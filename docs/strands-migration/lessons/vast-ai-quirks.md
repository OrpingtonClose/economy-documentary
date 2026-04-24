# Vast.ai quirks

Narrative knowledge page. Edit as we accumulate gotchas.

## Image compatibility

- We standardise on `pytorch/pytorch:2.3.0-cuda12.1-cudnn8-devel` as the
  base image for GPU workers. Tested against H100, H200, A100, L40S,
  3090, 4090. Driver mismatch is the most common first-boot failure —
  always confirm `nvidia-smi` works before installing weights.

## Boot times

<!-- Append observations as we gather them. -->

## Geo / latency

- Prefer US-East or EU-Central instances for lower RTT to Backblaze B2
  (which has free egress to Cloudflare and Vast). Asia-Pacific
  instances have observed ~4× B2 upload latency in prior ADK runs.

## API gotchas

- `vastai destroy instance <id>` returns 200 even if the instance was
  already destroyed. The infra agent treats "not found" as success.
- `vastai search offers` listings drift — price quoted at search time
  is a snapshot, not a guarantee. Budget +10 % margin.
