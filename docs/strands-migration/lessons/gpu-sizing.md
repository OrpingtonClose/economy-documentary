# GPU sizing ledger

Append-only. Written by the infra agent on every VM destruction.
Columns: `observed`, `vm_class`, `vram_total_gb`, `vram_peak_gb`,
`disk_total_gb`, `disk_peak_gb`, `workload`, `run_duration_s`, `notes`.

Current policy (per plan v2 § Wave 2 slice 5): **overprovision first,
measure, optimize.** Downshift a VM class only after 3+ successful runs
show consistent headroom.

## Observations

<!-- NEW ENTRIES APPENDED BELOW -->

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
