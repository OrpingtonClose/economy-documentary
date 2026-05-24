# /cheat Check — 2026-05-23

## Global Rules (Must Be Obeyed Everywhere)

### ❌ NEVER: `deepseek:deepseek-chat`

**Rule:** All LLM agents MUST use **`deepseek:deepseek-v4-flash`** (or `deepseek:deepseek-v4-pro`). `deepseek-chat` is **forbidden**.

**Why:** `deepseek-chat` is the legacy V3 model. `deepseek-v4-flash` is the current production model with better reasoning, faster inference, and lower cost.

**Applies to:**
- `create_deep_agent(model=...)`
- `init_chat_model(...)`
- Any direct `ChatDeepSeek(...)` instantiation
- Any model string passed to LangChain, DeepAgents, or raw API calls

**Current violation:** None in committed code, but must be checked on every new agent creation.

---

## Violations Found in Modified Files

---

### ❌ CRITICAL: NotImplementedError Stubs (NO MOCKS / STUBS / FAKES)

**server/tools/video_tools.py:101**
```python
def probe_clip(*, mp4_path: str) -> str:
    raise NotImplementedError("probe_clip is not yet implemented.")
```
→ Assembly stage WILL crash when it tries to probe clip duration.

**server/tools/assembly_tools.py:419**
```python
def mux_audio_video(*, audio_path: str, video_path: str, output_path: str) -> str:
    raise NotImplementedError("mux_audio_video is not yet implemented.")
```
→ Final assembly WILL crash when it tries to mux audio+video into master.mp4.

**server/tools/assembly_tools.py:432**
```python
def normalize_audio_loudness(*, input_path: str, output_path: str) -> str:
    raise NotImplementedError("normalize_audio_loudness is not yet implemented.")
```
→ Audio normalization WILL crash.

**Fix:** Replace with real ffmpeg subprocess calls. NO TIMEOUT.

---

### ❌ CRITICAL: Timeouts on Non-Health-Probe Operations (NO TIMEOUTS ANYWHERE)

**server/research_tools.py:60**
```python
with urllib.request.urlopen(req, timeout=15) as resp:
```
→ Brave Search API call with 15s timeout. Not a health probe.

**server/research_tools.py:88**
```python
with urllib.request.urlopen(req, timeout=15) as resp:
```
→ Exa Search API call with 15s timeout. Not a health probe.

**server/vm_registry_tools.py:76**
```python
with urllib.request.urlopen(url, timeout=10) as resp:
```
→ Worker health check with 10s timeout. This IS a health probe (allowed per /cheat exception) BUT must be documented in the function docstring.

**Fix for research_tools.py:** Remove `timeout=` parameters. If the search API hangs, the agent should decide what to do, not be killed by code.
**Fix for vm_registry_tools.py:** Add docstring documenting that `timeout=10` is an intentional fast-fail health probe.

---

### ❌ CRITICAL: Domain Agent Has Infra Tools (Domain ↔ Provisioner Separation)

**server/strands_agents/graph_pipeline.py**

Audio agent tool list includes:
- `bash_command` — VM provisioning
- `_query_vm_registry` — VM registry query
- `_check_worker_health` — VM health check
- `_get_provisioning_guidance` — VM provisioning decision
- `_research_model_requirements` — GPU research
- `_evaluate_vastai_offers` — Vast.ai offer ranking

Video agent tool list includes the same infra tools.

Per /cheat:
> "Domain agents never know VM URLs exist. They know job queues and B2 objects. Provisioner agents own VMs."

**Fix:** Remove all infra tools from audio/video agents. Create separate `audio_provisioner` and `video_provisioner` graph nodes (or provisioner agents within the same node but separate from domain work). The domain agent posts jobs to a queue; the provisioner agent owns VMs.

---

### ❌ CRITICAL: Exceptions Swallowed Without notify_maintainer (Notify the Maintainer Agent)

Per /cheat: "All logging without notify_maintainer is equivalent to pass."

**server/research_tools.py:68**
```python
except Exception as exc:
    return f"Brave search failed: {exc}"
```
→ Returns error string. Agent sees text but system never notifies maintainer.

**server/research_tools.py:96**
```python
except Exception as exc:
    return f"Exa search failed: {exc}"
```
→ Same.

**server/vm_registry_tools.py:78**
```python
except Exception as exc:
    return f"Worker at {url} is NOT reachable: {exc}"
```
→ Same.

**server/tool_extract.py:73**
```python
except Exception as exc:
    logger.warning("Extraction failed for %s: %s", tool_name, exc)
```
→ Logging without notify_maintainer = pass.

**server/tool_extract.py:146**
```python
except Exception:
    pass  # Snapshot store is best-effort
```
→ Literal pass. Violation.

**server/callbacks/after_tool.py:128**
```python
except Exception:
    pass  # Snapshot store is best-effort
```
→ Literal pass. Violation.

**server/callbacks/after_tool.py:130**
```python
except Exception as exc:
    logger.debug("Tool extraction failed for %s: %s", tool_name, exc)
```
→ Logging without notify_maintainer = pass.

**Fix:** Either call `notify_maintainer()` in every exception handler, or let exceptions propagate so the agent can see them. Do not swallow.

---

### ❌ MEDIUM: Fixed Polling Interval (Agent Decides, Code Does Not Constrain)

**server/strands_agents/shared_a2a/vast_provisioning.py:230-234**
```python
for attempt in range(1, 61):
    healthy = check_worker_health(worker_url, "video")
    if healthy:
        break
    time.sleep(10)
```
→ Blocks for 10 minutes with fixed 10s intervals. Agent cannot reason while blocked.

**server/strands_agents/shared_a2a/vast_provisioning.py:257-261**
```python
for attempt in range(1, 61):
    healthy = check_worker_health(worker_url, "tts")
    if healthy:
        break
    time.sleep(10)
```
→ Same.

**Fix:** Replace with agent-driven polling. Each check returns immediately; agent decides when to check again based on reasoning.

---

### ❌ MEDIUM: Algorithmic Retry Without Reasoning (Agent Decides, Code Does Not Constrain)

**server/strands_agents/graph_pipeline.py:248**
```python
for attempt in range(self.max_retries + 1):
    try:
        result = await self.graph.invoke_async(task)
        ...
    except RuntimeError as exc:
        if attempt >= self.max_retries:
            raise
        failed_node = self._classify_failure(exc)
        reason = str(exc)
        ...
```
→ Retries up to `max_retries` times without reasoning about whether retry is appropriate. DeepSeek connection drop → retry. Worker OOM → retry. These need different responses.

**Fix:** On failure, call `notify_maintainer()` or a recovery agent that decides: retry, reprovision, relax constraints, or escalate.

---

## Summary

| Category | Count | Files |
|----------|-------|-------|
| Stubs (NotImplementedError) | 3 | video_tools.py, assembly_tools.py |
| Timeouts | 2 (+1 documented) | research_tools.py, vm_registry_tools.py |
| Domain/Provisioner mixing | 1 | graph_pipeline.py |
| Swallowed exceptions | 7 | research_tools.py, vm_registry_tools.py, tool_extract.py, after_tool.py |
| Fixed polling | 2 | vast_provisioning.py |
| Algorithmic retry | 1 | graph_pipeline.py |

**Blocks next run:** The 3 NotImplementedError stubs guarantee a crash during assembly.

**Must fix before committing again:** Stubs, timeouts on non-health-probes, domain/provisioner mixing, swallowed exceptions.
