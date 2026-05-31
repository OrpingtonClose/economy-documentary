> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Guardian tuning ledger

Append-only. What `GUARDIAN_IDLE_SECONDS` and `GUARDIAN_MAX_LIFETIME_SECONDS`
values actually produced good behaviour per workload.

Columns: `observed`, `workload`, `idle_s`, `max_lifetime_s`,
`destroy_reason` (one of `idle`, `lifetime`, `manual`, `crash`), `notes`.

## Default recommendations (update as data accumulates)

| Workload | idle_s | max_lifetime_s | Rationale |
| --- | --- | --- | --- |
| `qwen3-tts` | 900 (15 min) | 14400 (4 h) | Short per-scene jobs, safe to kill fast |
| `ltx-video-2.3` | 1800 (30 min) | 21600 (6 h) | Longer jobs, weights warm-up expensive |
| `debug` | 3600 (1 h) | 14400 (4 h) | Manual ping cadence is slow |

## Observations

<!-- NEW ENTRIES APPENDED BELOW -->
