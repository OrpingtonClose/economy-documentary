> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Provisioner Agent — Plan & Problem Analysis

## What the User Wants

1. **Provisioner must be an LLM agent**, not deterministic code.
2. **Use deepagents** (specifically "pedantic-deepagents" — emphasis on strict agentic behavior).
3. **Model: `deepseek:deepseek-v4-flash`** (never `deepseek-chat`).
4. **No algorithmic text parsing** between agents — plain text or instructor-based structured extraction only.
5. **Agent decides, code does not constrain** — no hardcoded polling intervals, no fixed retry loops, no timeouts.

---

## Current Architecture (What I Built)

```
provisioner_loop.py  →  thin outer loop (claims jobs, invokes agent)
       ↓
provisioner_agent.py →  deepagents agent with tools:
                          claim_job, search_offers, provision_vm,
                          get_instance_status, create_ssh_tunnel,
                          check_worker, execute_job, upload_result,
                          complete_job, fail_job, teardown_instance,
                          kill_tunnel
```

The outer loop is cheap deterministic code. The agent is invoked once per job and decides the full VM lifecycle using tools.

---

## Problems Identified

### Problem 1: `deepseek-v4-flash` is a Reasoning Model — Broken with DeepAgents Tool Calling

**Symptom:**
```
openai.BadRequestError: Error code: 400 —
'The `reasoning_content` in the thinking mode must be passed back to the API.'
```

**Root Cause:**
- `deepseek-v4-flash` returns `reasoning_content` in its responses (like DeepSeek-R1).
- DeepSeek's API requires this `reasoning_content` to be included in the next request's assistant message.
- `langchain-deepseek` stores `reasoning_content` in `AIMessage.additional_kwargs` but does NOT pass it back to the API on subsequent calls.
- `langchain-openai`'s `_convert_message_to_dict` ignores `additional_kwargs.reasoning_content`.
- My monkey-patch on `_convert_message_to_dict` fixed the dict conversion, but **LangGraph's checkpoint serializer or message reducer may strip `additional_kwargs` when reconstructing messages from checkpoint state**.

**Impact:** The agent cannot complete a multi-turn tool-calling conversation. First turn works (model outputs tool_calls). Second turn fails because the reconstructed assistant message lacks `reasoning_content`.

**Fix Options (ranked by preference):**

| Option | Description | Effort |
|--------|-------------|--------|
| A | **Custom message serialization** — ensure LangGraph checkpoints preserve `additional_kwargs.reasoning_content` through full round-trip (dict → JSON → dict → AIMessage). | Medium |
| B | **Subclass `ChatDeepSeek`** — override `_generate` and `_stream` to manually inject `reasoning_content` into request payloads from message history. | High |
| C | **Post-process messages before model call** — middleware that walks the message history and injects `reasoning_content` back into assistant message dicts right before the API call. | Medium |
| D | **Use `deepseek-chat` instead** — User explicitly forbade this. Not an option. | N/A |

**Recommended fix:** Option C — add a DeepAgents middleware that intercepts model requests and patches assistant messages to include `reasoning_content` from `additional_kwargs`. This is clean, non-invasive, and survives package updates.

---

### Problem 2: DeepAgents is Designed for Interactive Chat, Not Background Loops

**Symptom:** DeepAgents agents expect a human-in-the-loop or single-shot conversation pattern. Running one in a continuous background loop is awkward.

**Root Cause:**
- DeepAgents has built-in tools (`ls`, `read_file`, `write_file`, `execute`, `task`, `write_todos`) that assume an interactive session.
- The `SummarizationMiddleware` truncates message history, which may lose critical context (e.g., "I already provisioned instance 12345").
- The `FilesystemMiddleware` may try to read/write files in ways not relevant to VM provisioning.

**Impact:** Agent may lose track of state mid-job, or waste tokens on irrelevant built-in tools.

**Fix:**
- Provide a very explicit system prompt that tells the agent exactly what tools to use and in what order.
- Use `response_format` (Pydantic `ProvisionerResult`) to force structured output so the outer loop knows what happened.
- Consider disabling unnecessary built-in tools via DeepAgents' tool exclusion mechanism if available.

---

### Problem 3: Cost — LLM Invocation Per Job is Expensive

**Symptom:** Each job requires at least one LLM invocation (potentially 5–10 tool calls × multiple turns).

**Root Cause:** VM provisioning is mostly deterministic. Searching offers, creating instances, polling status, uploading files — these don't require LLM reasoning. Only error recovery and edge-case handling benefit from an agent.

**Impact:** At $0.50–$2.00 per job in LLM costs, a 50-scene documentary costs $25–$100 just in provisioner tokens.

**Mitigation:**
- The thin outer loop is already cheap. Only the job execution is agent-driven.
- The agent should be given very strict instructions to use tools efficiently and avoid unnecessary LLM turns.
- For happy-path execution (offer found → instance running → tunnel up → worker ready → job executed → uploaded), the agent might only need 1–2 LLM turns.

---

### Problem 4: SSH Tunnel PID Tracking Across Agent Turns

**Symptom:** The agent creates an SSH tunnel (returns PID). On a later turn, it needs to destroy the tunnel. But if the agent's message history was summarized, it might forget the PID.

**Root Cause:** DeepAgents `SummarizationMiddleware` drops old messages to save context window. Tunnel PID lives in a tool result message that may be dropped.

**Impact:** Orphaned SSH tunnels leaking processes on the host.

**Fix:**
- Store tunnel PIDs and instance IDs in a small SQLite table or JSON file.
- Provide the agent with a `get_active_tunnels()` tool that reads from this persistent store.
- The outer loop also reads this store for cleanup on shutdown.

---

### Problem 5: Worker HTTP 500 When Model Not Loaded

**Symptom:** Even after `_wait_for_worker` returns success, `execute_job` can get HTTP 500 if the TTS/LTX model hasn't finished loading.

**Root Cause:** The worker starts its HTTP server before models are loaded. The old code tried to parse `tts=yes/no` from the GET response, which the user correctly identified as algorithmic parsing.

**Fix Applied:** Worker now returns HTTP 503 while loading, HTTP 200 when ready. The agent's `check_worker` tool reports the HTTP status. The agent decides whether to wait or proceed.

**Remaining Risk:** The agent might call `execute_job` before the worker is truly ready. If it gets HTTP 500, the agent should recognize this as "worker not ready yet" and call `check_worker` again rather than failing the job.

---

### Problem 6: Slow VM Boot Times Make Testing Painful

**Symptom:** Each test iteration takes 3–7 minutes because the Docker image is large and must be pulled fresh on each host.

**Root Cause:**
- Base image: `pytorch/pytorch:2.10.0-cuda12.6-cudnn9-runtime` (~7GB)
- Onstart script: `apt-get install ffmpeg` + `pip install fastapi uvicorn soundfile` + `git clone`
- No Docker image caching across Vast.ai hosts

**Impact:** Slow iteration. Each failed test costs money and wall-clock time.

**Fix:** Build a custom Docker image with everything pre-installed:
```dockerfile
FROM pytorch/pytorch:2.10.0-cuda12.6-cudnn9-runtime
RUN apt-get update && apt-get install -y ffmpeg git curl wget
RUN pip install --break-system-packages fastapi uvicorn soundfile torch
COPY scripts/gpu_worker.py /worker.py
CMD ["python", "/worker.py", "--mode", "auto", "--port", "8880"]
```
Push to Docker Hub. Then provisioning is ~30 seconds instead of 5 minutes.

---

## Recommended Implementation Plan

### Phase 1: Fix the Reasoning Model (Critical — Blocks Everything)

1. Create a DeepAgents middleware that patches `reasoning_content` into assistant message dicts before API calls.
2. Test with a minimal 2-turn tool-calling conversation.
3. Verify the provisioner agent can be created and invoked without `BadRequestError`.

### Phase 2: Build Persistent State for Tunnels/Instances

1. Create `/tmp/provisioner/state.json` (or SQLite) tracking:
   - `job_id` → `instance_id`, `tunnel_pid`, `worker_url`, `status`
2. Add agent tools:
   - `save_state(job_id, instance_id, tunnel_pid, worker_url)`
   - `load_state(job_id)` → returns state dict
   - `list_active_jobs()` → returns all in-progress jobs
3. Update outer loop to read state on startup and clean up orphaned resources.

### Phase 3: Refine Agent System Prompt & Tools

1. Rewrite system prompt to be extremely explicit about workflow:
   - "Step 1: claim_job. If none, stop."
   - "Step 2: search_offers. Pick cheapest suitable."
   - "Step 3: provision_vm. Save state."
   - "Step 4: get_instance_status. If not running, repeat this step."
   - "Step 5: create_ssh_tunnel. Save state."
   - "Step 6: check_worker. If not HTTP 200, repeat this step."
   - "Step 7: execute_job. If HTTP 500, go back to step 6."
   - "Step 8: upload_result."
   - "Step 9: complete_job."
   - "Step 10: teardown_instance + kill_tunnel. Clear state."
   - "If ANY step fails after 3 attempts, call fail_job and cleanup."
2. Remove or disable built-in deepagents tools that aren't relevant (filesystem, subagents).
3. Keep `response_format=ProvisionerResult` for structured output.

### Phase 4: Dry-Run Test

1. Create a test narration job.
2. Run the outer loop.
3. Verify: claim → provision → tunnel → worker ready → execute → upload → complete → destroy.
4. Fix any issues.

### Phase 5: Build Custom Docker Image (Optimization)

1. Write `scripts/Dockerfile.worker`.
2. Build and push to Docker Hub.
3. Update `provision_vm` tool to use the custom image.
4. Re-test — boot time should drop from 5 min to 30 sec.

---

## Open Questions for User

1. **Model fix priority:** Should I focus on the reasoning_content middleware first, or would you prefer a simpler approach (e.g., not using deepagents' built-in tool loop and instead calling the LLM manually with instructor)?
2. **Cost sensitivity:** Is the LLM-cost-per-job acceptable, or should I design a hybrid where the agent only handles errors and the happy path is deterministic?
3. **Docker image:** Should I build and push a custom worker image now, or is the 5-minute boot time acceptable for initial testing?
4. **CHEAT_CHECK update:** Should I add the `deepseek-v4-flash` model rule to CHEAT_CHECK now?
