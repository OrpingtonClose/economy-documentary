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

<!-- NEW ENTRIES APPENDED BELOW -->
### Bootstrap script gaps found by slice 4b smoke test

```yaml
observed: 2026-04-24
source: slice-4b / vast instance 35516824
severity: friction
tags: [bootstrap, systemd, pip-install-editable, vast-ai, cuda-base-image]
```

Two real blockers hit the happy-path bootstrap on `nvidia/cuda:12.2.0-base-ubuntu22.04` on Vast.ai. Both deferred from slice 4b into a followup:

1. **No systemd in Vast.ai containers**. Images launched via `vastai create instance --ssh` run with an SSH wrapper as PID 1, not systemd. `systemctl enable --now` fails with *"System has not been booted with systemd as init system (PID 1). Can't operate."* Bootstrap needs a non-systemd path — candidates: `supervisord`, `s6-overlay`, a plain `nohup` + per-service PID file shim, or document+require `vastai/base-image:systemd` as the only supported image. Current smoke test worked via `nohup` + PID files manually.

2. **`pip install -e server/` fails**. `server/pyproject.toml` has no `[tool.setuptools.packages.find]` or explicit `packages=` block, and setuptools refuses to auto-discover because there are >1 top-level package dirs (`api/`, `fleet/`, `agents/`, `plugins/`, `adk_eval/`, `strands_agents/`, …). Worked around by dropping `-e` entirely and running with `PYTHONPATH=/opt/economy-documentary/server`. The real fix is either (a) add `packages = ["strands_agents"]` to the pyproject, or (b) move to `src/` layout, or (c) stop editable-installing the whole server and just `PYTHONPATH` the worker module.

Both land as a cleanup PR before the next real-GPU slice (LTX-Video).


