> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# GPU sizing ledger

Append-only. Written by the infra agent on every VM destruction.
Columns: `observed`, `vm_class`, `vram_total_gb`, `vram_peak_gb`,
`disk_total_gb`, `disk_peak_gb`, `workload`, `run_duration_s`, `notes`.

Current policy (per plan v2 § Wave 2 slice 5): **overprovision first,
measure, optimize.** Downshift a VM class only after 3+ successful runs
show consistent headroom.

## Observations

<!-- NEW ENTRIES APPENDED BELOW -->
### Slice 4b smoke test: qwen3-tts-worker on RTX 3090

```yaml
observed: 2026-04-24
source: slice-4b / vast instance 35516824
severity: info
tags: [qwen3-tts, rtx-3090, vast-ai, stub-engine, smoke-test]
```

- **VM class**: RTX 3090 / 24 GB VRAM, 32 GB RAM, 50 GB disk (requested), Xeon E5-2695 v4, reliability 99.47%, $0.113/hr base + $0.006/hr storage = $0.167/hr total.
- **Workload**: qwen3-tts-worker stub engine (no real Qwen3-TTS weights yet) + infra-agent guardian. One `/tts/render` request yielded a 162 KB WAV.
- **Peak observations**: VRAM used 1 MiB (stub engine is CPU-only — real Qwen3-TTS TBD), disk filled well under 10 GB for repo + venv + ffmpeg + base packages. 50 GB allocation is comfortable; could safely downshift to 25 GB for TTS worker.
- **Smoke test duration**: ≈12 minutes boot → idle destroy. Cost: ≈$0.033.
- **Destroy path**: manual shortening of `GUARDIAN_IDLE_SECONDS` to 60s; guardian fired in under 90s of no-bump traffic. `vastai destroy instance` call succeeded — SSH refused + instance gone from account listing within seconds.
- **Recommendation for qwen3-tts-worker (prod)**: RTX 3090 class (or A10/L4) is ample for a stub worker; revisit once real Qwen3-TTS weights load and VRAM numbers are observable. Budget 30 GB disk once we're past stub.


<!-- Example (delete when first real entry lands):

### 2026-04-22 — H200 / LTX-Video 2.3 (slice 5 first run)

```yaml
observed: 2026-04-22
source: slice-5
severity: info
tags: [ltx-video-2.3, h200, vram, first-run]
```

- vm_class: H200 SXM (141 GB VRAM)
- vram_peak_gb: 52
- disk_total_gb: 500
- disk_peak_gb: 78
- workload: LTX-Video 2.3, 8 scenes × 5s @ 768×512
- run_duration_s: 420
- notes: Significant headroom — recommend L40S (48 GB) or A100 (40 GB)
  test on next run. Disk usage dominated by weights (~55 GB) + intermediate
  scene mp4s (~15 GB).
-->
