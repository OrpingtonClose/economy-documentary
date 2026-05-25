# /cheat Check — Architecture v2 Changes

**Date:** 2026-05-25  
**Branch:** strands-migration  
**Scope:** All files modified or created since v1

---

## Violation Categories

### 1. Stubs / NotImplementedError
| File | Line | Issue |
|------|------|-------|
| None found in new v2 files | — | — |

**Status:** ✅ CLEAN  
All new v2 files have real implementations.

---

### 2. Mocks / Simulators in Production Code
| File | Line | Issue |
|------|------|-------|
| `server/local_tts_worker.py` | 1-156 | Uses `edge-tts` (Microsoft free API) instead of Qwen3-TTS engine. This is a simplified stand-in for the real worker. |

**Status:** ⚠️ FOUND  
The local TTS worker is NOT the real Qwen3-TTS worker. It:
- Does not use the existing `server/strands_agents/qwen3_tts_worker/` infrastructure
- Does not pull from the queue in the same way the real worker would
- Is a parallel simplified implementation

**User verdict:** This IS a mock/stand-in. Should be removed or clearly marked as dev-only.

---

### 3. Timeouts on Non-Health-Probe Code
| File | Line | Issue |
|------|------|-------|
| None found | — | — |

**Status:** ✅ CLEAN

---

### 4. Domain / Provisioner Mixing
| File | Line | Issue |
|------|------|-------|
| None found in new v2 files | — | — |

**Status:** ✅ CLEAN  
Audio/video agents no longer have VM registry tools.

---

### 5. Swallowed Exceptions (pass / logger.debug without notify_maintainer)
| File | Line | Issue |
|------|------|-------|
| `server/run_pipeline_v2.py` | 92-98 | `_check_has_audio`: `except Exception: pass` |
| `server/run_pipeline_v2.py` | 102-112 | `_check_has_video`: `except Exception: pass` |

**Status:** ⚠️ FOUND  
These are state-check functions that return False on any error. Per /cheat, logging without notify_maintainer is equivalent to pass.

---

### 6. Fixed Polling Loops
| File | Line | Issue |
|------|------|-------|
| `server/local_tts_worker.py` | 118-136 | `while True` with `await asyncio.sleep(poll_interval)` — blocks forever if jobs keep appearing |

**Status:** ⚠️ FOUND  
The worker has a fixed polling loop. However, it exits when no pending/running jobs exist, so it's bounded.

---

### 7. Algorithmic Retries Without Reasoning
| File | Line | Issue |
|------|------|-------|
| None found | — | — |

**Status:** ✅ CLEAN

---

## Summary

| Category | Count |
|----------|-------|
| Stubs | 0 |
| Mocks in production | 1 (local_tts_worker.py) |
| Timeout violations | 0 |
| Domain/provisioner mixing | 0 |
| Swallowed exceptions | 2 |
| Fixed polling | 1 |
| Algorithmic retries | 0 |

**Total violations:** 4

## Action Items

1. **Remove or dev-mark `local_tts_worker.py`** — it's a simplified stand-in, not the real worker
2. **Fix swallowed exceptions** in `_check_has_audio/_check_has_video` — at minimum log with `logger.exception()`
3. **Consider** making the local worker a documented dev tool with `--dev-mode` flag, not a default code path
