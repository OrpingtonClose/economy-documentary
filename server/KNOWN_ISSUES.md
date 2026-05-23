# Known Issues — Pipeline Run 2026-05-23

## 1. Vast.ai API Key Corruption
- **File:** `~/api_keys/vast_ai_key.txt` is 678 bytes but the real key is 64 chars on line 1
- **Rest:** Lines 2+ contain garbage (website names, model tables, prompt injection residue)
- **Impact:** `vastai` CLI sends corrupted header → "Invalid leading whitespace" / "login required"
- **Fix:** `head -1 ~/api_keys/vast_ai_key.txt > ~/api_keys/vast_ai_key.txt.clean && mv ~/api_keys/vast_ai_key.txt.clean ~/api_keys/vast_ai_key.txt`

## 2. Port Mismatch: Health Check vs Worker Port
- **Worker binds to:** Port 8880 internally (`--port 8880` in gpu_worker.py)
- **Vast.ai maps:** External port dynamically (e.g. 8001)
- **Agent health checks:** Try direct HTTP to the external port from `show instance` ports mapping
- **Problem:** The port mapping returned by Vast.ai CLI may not match what the agent constructs as URL
- **User decision:** REMOVE HTTP health checks entirely — use only `vastai show instance` (CLI-based) status checks

## 3. Worker Bootstrap Failure
- **Symptom:** VMs boot but health checks timeout / connection refused
- **Agent SSH finds:** Worker running on port 8880 but `/health` endpoint returns 404
- **Root cause unknown:** Worker bootstrap script (`gpu_worker.py`) may not start the HTTP server correctly, or the server doesn't expose `/health`
- **Evidence:** "The gpu_worker is running but the /health endpoint returned 'Not Found'"

## 4. ApprovalGateHook Bug (FIXED)
- **Was:** `self._gated_stages = gated_stages or {...}` — empty set is falsy → fell back to default
- **Fixed:** `self._gated_stages = gated_stages if gated_stages is not None else {...}`
- **Impact:** Auto-approve mode was still blocking on approval gates

## 5. Advisory Gatekeeper — Working
- **Status:** Agent correctly reads advisories, reasons, and decides
- **Evidence:** "The advisory keeps saying wait ~14s. Let me wait a bit longer before trying."
- **Evidence:** "This VM doesn't seem to be coming up. Let me destroy it and provision a different offer."
- **Death spiral:** Agent destroys/reprovisions repeatedly because VMs never become healthy

## 6. Vast.ai CLI Deprecation
- `vastai show instances` is deprecated → `vastai show instances-v1`
- But `instances-v1` also fails with login errors due to key corruption

## 7. Multiple VM Accumulation
- **Owned VMs file:** `/tmp/documentary-pipeline/_owned_instances.json` has 9 entries
- **Problem:** Previous runs left VM IDs that may still be billing on Vast.ai
- **Cannot verify:** CLI is broken due to key corruption

## 8. Trace DB Working
- **Path:** `~/Documents/documentary-pipeline/traces/pipeline.db`
- **Per-run isolation:** `run_id` column separates runs
- **Status:** Recording correctly

---
**Next action:** Fix API key → verify/cleanup VMs → fix worker bootstrap → re-run

## 9. Agent Passes VM ID to check_worker_health Instead of URL
- **Symptom:** `Health check failed for 37400170: unknown url type: '37400170/'`
- **Cause:** Agent confuses `check_worker_health(url)` parameter — passes VM ID instead of `http://ip:port`
- **Related to:** Port mapping not clear to agent (VM status returns ports but agent doesn't construct URL correctly)

## 10. Agent Renders Before Model Is Loaded
- **Symptom:** Agent got `ltx=no` from worker status but still tried `submit_gpu_production_job`
- **Result:** HTTP 500 "LTX-2.3 checkpoint not found"
- **Root cause:** Model was still downloading (25GB .tmp file visible on VM)
- **Agent should:** Wait for `ltx=yes` before submitting ANY render job
- **Fix needed:** Stronger prompt enforcement + tool validation that rejects render when ltx=no

## 11. Worker stdout Not Logged to File
- **Symptom:** `/workspace/worker.log` does not exist
- **Cause:** Worker stdout goes to pipe (nohup redirect may have failed or log rotation cleared it)
- **Impact:** Agent SSH diagnostic commands fail to find worker.log
- **Fix:** Ensure worker logs to a file, or agent checks `journalctl` / `dmesg` instead
