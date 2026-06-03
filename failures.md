# Documentary Pipeline Failures Log

This file gathers all critical failures, bottlenecks, and architectural issues identified during the execution of the Lacanian biography documentary pipeline.

---

## 1. DeepSeek US Datacenter Regional Block (CRITICAL)
- **Symptom:** VM agent endpoints fail with HTTP `500 Internal Server Error` on POST requests.
- **Root Cause:** The active worker VMs are provisioned in California (US datacenters). DeepSeek API returns `403 Forbidden` (`unsupported_country_region_territory`) for requests originating from US IP addresses.
- **Impact:** The VM agent cannot communicate with its LLM backend, preventing automated text-to-speech (TTS) and LTX rendering job execution. The orchestrator hangs indefinitely waiting for job updates.

## 2. Rigid Regex/Schema Parsing Violating Semantic Guidelines
- **Symptom:** Small changes in model output formatting or description details cause parser errors.
- **Root Cause:** Downstream components use rigid regex string matching instead of semantic parsing, directly violating the project's design guidelines.
- **Impact:** Unnecessary pipeline crashes and retries.

## 3. Monolithic Synchronous REST Endpoints Hanging
- **Symptom:** PUT/POST requests to agent endpoints (e.g. Scenario or Provisioner) hang or timeout.
- **Root Cause:** The FastAPI endpoints await the entire agent turn (which performs slow LLM requests and SSH/SCP operations) before returning a response, rather than immediately returning a `202 Accepted` receipt and processing asynchronously.
- **Impact:** REST clients timeout and pipeline orchestration is stalled.

## 4. NoOp / Requeue Loop Event Store Clogging
- **Symptom:** Hundreds of `noop` events cluttering the SQLite database.
- **Root Cause:** State check loops write `noop` to the persistent database.
- **Impact:** Floods the event log, making debug/replay slow and consuming unnecessary resources.

## 5. Slow Bootstrapping & Compilation Timeouts
- **Symptom:** VM provisioning takes 15+ minutes.
- **Root Cause:** Bootstrap scripts compile heavy Python packages (like `flash-attn`) from source and download models directly from Hugging Face instead of cloning pre-built environments from local object stores.
- **Impact:** Heavy startup delays and high failure rates during initialization.

## 6. WhisperX Missing Import Failure
- **Symptom:** `align_narration_audio` fails with `ModuleNotFoundError: No module named 'whisperx'`.
- **Root Cause:** WhisperX was not installed or configured in the python environment.
- **Impact:** Word-level alignment is skipped, degrading final video timing.

## 7. Provisioner Agent Hypervigilance & Non-US Geolocation Filtering
- **Required Improvement:** The Provisioner Agent must be hypervigilant to regional LLM API blocks (e.g., DeepSeek blocking US IPs, causing HTTP 500/403 errors on VM Agent job dispatches).
- **Proposed Solution:**
  1. Append `geolocation notin [US]` to all `vastai search offers` queries so VMs are never provisioned in the United States.
  2. Enhance the Provisioner Agent's troubleshooting rules to automatically check VM Agent logs for country restrictions or DeepSeek block errors. If found, it must immediately teardown the instance using `yes | vastai destroy instance <instance_id>` with the reason `provision_failed` and provision a new instance in a non-US region.
