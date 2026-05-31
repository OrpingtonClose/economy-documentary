> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# Report: Problems with Making the Provisioner an Agent

## Executive Summary

Converting the provisioner from deterministic code to an LLM agent introduces **cascading failures across model compatibility, cost, reliability, latency, and framework fit**. The core tension is that **infrastructure orchestration benefits from determinism**, while LLM agents excel at ambiguity and reasoning. Forcing an agent onto VM provisioning creates expensive, slow, and brittle infrastructure.

---

## 1. Model Incompatibility: `deepseek-v4-flash` Cannot Do Multi-Turn Tool Calling

### Problem
`deepseek-v4-flash` is a **reasoning model** (like DeepSeek-R1). It returns `reasoning_content` alongside `content` in every response. DeepSeek's API **requires** this `reasoning_content` to be passed back verbatim in subsequent request messages. If missing, the API returns:

```
400 BadRequestError: 'The `reasoning_content` in the thinking mode must be passed back to the API.'
```

### Why This Breaks DeepAgents
- **Turn 1 works**: Model receives user message → generates `reasoning_content` + `tool_calls`.
- **Turn 2 fails**: LangGraph reconstructs message history from checkpoint. The assistant message from Turn 1 lacks `reasoning_content` in the serialized payload sent to the API.
- **Root cause chain**:
  1. `langchain-deepseek` stores `reasoning_content` in `AIMessage.additional_kwargs`
  2. `langchain-openai._convert_message_to_dict` ignores `additional_kwargs.reasoning_content`
  3. Monkey-patching `_convert_message_to_dict` fixes dict conversion but **LangGraph's state reconstruction or middleware may still strip it**
  4. Even with the patch, the second model call fails because the assistant message in the reconstructed history has no `reasoning_content`

### Evidence
```
msg[0] role=system has_reasoning=False
msg[1] role=user   has_reasoning=False
msg[2] role=assistant has_reasoning=False   <-- SHOULD BE True
msg[3] role=tool   has_reasoning=False
Error: 400 - reasoning_content must be passed back
```

### Impact
**The agent cannot complete a single job.** Any tool call that requires a follow-up turn (which is all of them: provision → check → tunnel → health → execute → upload) crashes on the second LLM invocation.

### Fix Options (All Bad)
| Option | Effort | Trade-off |
|--------|--------|-----------|
| Patch LangChain message serialization | High | Fragile, breaks on package updates |
| Patch LangGraph checkpoint serde | High | Deep internals, high maintenance |
| Build custom DeepAgents middleware | Medium | Adds complexity to already-complex stack |
| Use `deepseek-chat` instead | Zero | **User explicitly forbade this** |
| Don't use DeepAgents; call API directly | Medium | Defeats the purpose of using DeepAgents |

**Verdict**: The model and framework are fundamentally incompatible for multi-turn tool calling.

---

## 2. Cost: Every Job Costs $0.50–$2.00 in LLM Tokens

### Problem
A single job lifecycle requires the agent to make **5–10 tool calls across 2–4 LLM turns**:

1. Search offers → pick one
2. Provision VM → get instance_id
3. Poll status (may be multiple turns)
4. Create SSH tunnel
5. Health-check worker (may be multiple turns)
6. Execute job
7. Upload to B2
8. Mark complete
9. Destroy VM + kill tunnel

Each turn sends the full message history (system prompt + all previous messages + tool results) to the model. With `deepseek-v4-flash`, reasoning models are **more expensive per token** than base models.

### Math
- 1 job ≈ 3–5 LLM turns
- 1 turn ≈ 5k–15k tokens (long system prompt + tool results + message history)
- `deepseek-v4-flash` pricing: ~$0.50–$1.50 per job
- 50-scene documentary: **$25–$75 just in provisioner LLM costs**

### Comparison
| Approach | Cost per Job | 50-Scene Cost |
|----------|--------------|---------------|
| Deterministic loop | $0.00 | $0 |
| Agent (deepseek-v4-flash) | $0.50–$1.50 | $25–$75 |
| Agent + errors/retry | $1.00–$3.00 | $50–$150 |

### Impact
This is **pure overhead**. The LLM adds no creative value to "search Vast.ai, pick cheapest, provision, poll, tunnel, execute." The decisions are trivial (pick cheapest offer, wait for running, try tunnel). Paying $25–$75 for deterministic orchestration is wasteful.

---

## 3. Latency: Each Job Takes Minutes Longer

### Problem
Every LLM turn adds **2–10 seconds** of network latency (DeepSeek API is in China, high latency from EU/US). With 3–5 turns per job:

- Agent overhead: **10–50 seconds per job**
- VM boot time: **3–5 minutes**
- Worker model load: **1–3 minutes**

For 50 scenes, agent overhead adds **~15–40 minutes** of wall-clock time to a process that's already slow.

### Impact
Slower iteration. Slower delivery. More Vast.ai billing hours because VMs sit idle while the agent "thinks."

---

## 4. Reliability: Agents Hallucinate and Loop

### Problem
LLMs are non-deterministic. An agent provisioner can:
- **Hallucinate instance IDs**: "Destroy instance 99999" (doesn't exist)
- **Get stuck in loops**: Check worker → not ready → check worker → not ready → ... (forever)
- **Make wrong decisions**: Pick an offer without CUDA support because the prompt was ambiguous
- **Skip steps**: Forget to destroy the VM, leaving $0.18/hr instances running
- **Parse tool results creatively**: See "loading" and decide the instance is "ready enough"

### Evidence from Testing
During dry-run testing, the deterministic code had clear failure modes:
- Instance loading for 7+ minutes → deterministic timeout would catch this
- Worker HTTP 500 → deterministic retry would catch this
- Port mapping missing → deterministic SSH fallback would catch this

With an agent, these failures become **opaque**: "The agent decided the worker was ready" → job crashes with HTTP 500 → agent might retry, might not, might blame B2.

### Impact
**Orphaned VMs costing money. Failed jobs. Hard-to-debug production issues.**

---

## 5. Framework Mismatch: DeepAgents Is for Chat, Not Background Loops

### Problem
DeepAgents (LangGraph-based) is designed for:
- Interactive chat sessions
- Human-in-the-loop approval
- File system exploration
- Sub-agent spawning

The provisioner needs:
- Continuous background polling
- State persistence across crashes
- Cheap idle loops
- Deterministic cleanup on SIGTERM

### Specific Mismatches
| DeepAgents Feature | Provisioner Need | Conflict |
|--------------------|------------------|----------|
| `SummarizationMiddleware` | Preserve full message history (tunnel PIDs, instance IDs) | Summarization drops old tool results |
| `FilesystemMiddleware` | No file access needed | Wastes tokens on irrelevant tools |
| `SubAgentMiddleware` | No sub-agents needed | Wastes tokens on irrelevant tools |
| `TodoListMiddleware` | Simple job queue exists | Redundant abstraction |
| Interactive `interrupt_on` | Headless background process | No human to approve |
| Checkpoint serialization | Need SSH tunnel PIDs to survive | PIDs are process-local, not serializable |

### Impact
**Bloated agent with 10+ built-in tools it doesn't need**, consuming context window and increasing token costs. Summarization middleware actively **destroys critical state** (tunnel PIDs) to save context window.

---

## 6. State Management: Tunnel PIDs and Instance IDs Are Ephemeral

### Problem
The agent needs to track:
- `instance_id` (Vast.ai VM)
- `tunnel_pid` (local SSH process)
- `worker_url` (localhost:port)

These values are produced by one tool call (`provision_vm`, `create_ssh_tunnel`) and consumed by later tool calls (`check_worker`, `execute_job`, `teardown_instance`).

**If the agent's message history is summarized or truncated, these values are lost.** The agent cannot destroy a VM whose ID it forgot.

### Impact
**Resource leaks.** Orphaned VMs running at $0.18/hr. Orphaned SSH processes consuming local ports. On a 50-scene documentary, this could leak 10–20 VMs = **$2–$4/hr ongoing cost** until manual cleanup.

---

## 7. Debugging: Agent Failures Are Opaque

### Problem
When deterministic code fails:
```
2026-05-23 18:40:56,450 ERROR Job job_9460d8554cd9 failed: HTTP Error 500
Traceback (most recent call last):
  File "provisioner_loop.py", line 409, in _process_one_job
    data, meta = _execute_job(job, worker_url)
```
Clear line number. Clear function. Clear error.

When an agent fails:
```
Agent decided to call execute_job(worker_url="http://localhost:34500/")
Worker returned HTTP 500
Agent decided to call check_worker(worker_url="http://localhost:34500/")
Worker returned HTTP 200
Agent decided to call execute_job again
Worker returned HTTP 500
Agent decided to fail_job
```
**Why did the agent call execute_job when the worker wasn't ready?** Unknown. The agent's reasoning is in its internal monologue, not in logs.

### Impact
**Longer MTTR (mean time to repair).** Harder to write regression tests. Harder to explain failures to users.

---

## 8. The `/cheat` Tension: "Agent Decides" vs. "Infrastructure Must Be Reliable"

### Problem
The `/cheat` rules state:
> "Agent Decides, Code Does Not Constrain"

This is **correct for creative decisions** (which TTS voice, which video prompt, how to edit a scene). It is **wrong for infrastructure** (poll VM status, retry SSH tunnel, upload to B2).

Infrastructure has **invariant sequences**:
1. You MUST provision before executing
2. You MUST upload before marking complete
3. You MUST destroy after marking complete
4. You MUST NOT mark complete if upload failed

An agent that "decides" to skip step 3 or reorder steps creates **data loss and financial liability**.

### Impact
**Violating infrastructure invariants is catastrophic.** An agent that forgets to destroy a VM costs money forever. An agent that marks a job complete before B2 upload succeeds loses the render output.

---

## 9. What the Agent Would Actually Decide (Nothing Interesting)

### Problem
Let's be honest about what decisions the provisioner agent would make:

| Decision | Agent's "Reasoning" | Deterministic Code Equivalent |
|----------|---------------------|-------------------------------|
| Which offer? | "Pick cheapest with enough VRAM" | `offers[0]` (already sorted by price) |
| When to check status? | "Wait a bit and check again" | `time.sleep(10)` |
| Tunnel or direct? | "Try direct first, then tunnel" | `if worker_url: return else: tunnel` |
| Worker ready? | "HTTP 200 means ready" | `resp.status == 200` |
| Retry or fail? | "Try 3 times then fail" | `for attempt in range(3)` |

**The agent adds zero intelligence.** Every "decision" is a trivial conditional that deterministic code expresses more clearly, faster, and cheaper.

---

## 10. Summary: The Provisioner Is Not a Good Agent

| Criterion | Agent Score | Deterministic Score |
|-----------|-------------|---------------------|
| Cost | ❌ $25–$75 per run | ✅ $0 |
| Latency | ❌ +15–40 min per run | ✅ Baseline |
| Reliability | ❌ Hallucinations, loops | ✅ Predictable |
| Debuggability | ❌ Opaque reasoning | ✅ Clear stack traces |
| Model compatibility | ❌ Broken (v4 flash) | ✅ N/A |
| State management | ❌ Leaks PIDs/VMs | ✅ Reliable cleanup |
| Framework fit | ❌ Wrong tool for job | ✅ Purpose-built |
| Infrastructure safety | ❌ Can skip steps | ✅ Enforces invariants |
| Decision quality | ❌ Trivial decisions | ✅ Same decisions, free |

---

## Recommendation

**Do not make the provisioner an agent.**

The provisioner is **infrastructure glue**, not a reasoning task. The `/cheat` principle "Agent Decides, Code Does Not Constrain" applies to **creative and ambiguous decisions**, not to VM lifecycle management.

### What Should Be an Agent
- **Audio agent**: Decides narration tone, pacing, which TTS voice, how to align with video
- **Video agent**: Decides prompts, camera angles, visual style, duration per scene
- **Assembly agent**: Decides transitions, timing, color grading, final export settings

### What Should Be Deterministic Code
- **Provisioner**: Search → Provision → Poll → Tunnel → Execute → Upload → Complete → Destroy
- **Job queue**: SQLite transactions, status transitions, retry counting
- **B2 uploads**: Checksum, retry, multipart

### Hybrid Compromise (If User Insists)
If the user absolutely requires agentic behavior in the provisioner, implement it as **error-only escalation**:

```python
# Happy path: deterministic (fast, cheap, reliable)
try:
    _process_one_job_deterministic(job)
except Exception as exc:
    # Error path: agent decides how to recover (expensive, but rare)
    agent.invoke(f"Job {job.job_id} failed with: {exc}. Decide: retry, reprovision, or escalate.")
```

This gives agentic flexibility **only where it's needed** (error recovery) without paying the cost on every single job.
