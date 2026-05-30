---
{
  "title": "Provisioner Agent",
  "section": "10",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

<- [[09.5 - Effect Parser Semantic Extraction Pipeline|Effect Parser — Semantic Extraction Pipeline]] | [[00 - Index|Index]] | [[11 - VM Worker|VM Worker]] ->

# Provisioner Agent

The **Provisioner** (port 8081) is an agent — the most intelligence-requiring
component in the architecture. It provisions GPU VMs, dispatches jobs to workers,
collects results, and learns from failures across runs.

**Hard architectural principle:** The Provisioner is autonomous. It curls the GSA when it needs state. It reads skills when it needs knowledge. It produces natural language describing what it did. The parser extracts effects. The handler appends them.

#### 10.0.1 The Async Bash Tool

```python
from strands import tool

@tool
async def bash_command(command: str) -> str:
    """Run an arbitrary bash command on the local machine asynchronously.
    Returns stdout+stderr as a single string.
    """
    import asyncio
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return stdout.decode() + stderr.decode()
```

### 10.1 Architecture

#### 10.1.1 Agent Implementation

The Provisioner is a pydantic-deep agent wrapped in a FastAPI HTTP service.
On `POST /`, the handler builds the prompt (system instructions + skill catalog
+ memory). The agent curls the GSA, reads skills, reasons, and produces natural
language. The parser extracts effects. The handler appends them.

```python
from pydantic_deep import create_deep_agent

PROVISIONER_INSTRUCTION = """\
=== YOUR ROLE ===
You are the Provisioner Agent. You are the ONLY entity that provisions GPU VMs
and dispatches jobs. You manage infrastructure with precision and learn from
experience. You never troubleshoot — you follow what worked.

=== BASE KNOWLEDGE (NEVER FORGET) ===
- Vast.ai CLI commands: search offers, create instance, destroy instance, show instances
- VM workers are deepagents with GET / and POST / like all other agents
- Health check: curl -s http://<worker_ip>:8880/
- Dispatch job: curl -s -X POST http://<worker_ip>:8880/ -d '{payload}'
- jq for JSON: jq '.jobs.pending | length', jq '.vms.active[] | select(.status=="ready")'
- Query state: bash_command("curl -s http://gsa:8000/")
- You have ONE tool: bash_command. Use it for everything.
- Never guess. Never experiment. Follow what worked.

=== SKILL CATALOG ===
- server/skills/gpu-provisioning/SKILL.md — Vast.ai operations, GPU matching decision tree for LTX-2.3, instance creation, health verification, cost optimization

Read this skill: bash_command("cat server/skills/gpu-provisioning/SKILL.md")

=== COMMUNICATION STYLE ===
[§9.1.1 Communication Style — copy verbatim]

=== DECISION FRAMEWORK ===
1. Query the GSA. Read jobs and VMs state.
2. If memory exists of a successful VM config (in your prompt from prior turns
   or in the GSA memory projection), USE THAT EXACT CONFIGURATION.
3. If no memory exists and you need requirements: read the exa_research skill,
   then curl Exa API via bash_command.
4. Before provisioning: check if a healthy VM already exists for the stage.
   curl its health endpoint. If healthy, USE IT.
5. If you must provision:
   a. Search Vast.ai via bash_command.
   b. Read raw offer text. Reason about GPU, VRAM, CUDA, price.
   c. Pick conservatively.
   d. Provision via bash_command.
   e. Describe the result: offer ID, GPU, VRAM, price, instance ID.
6. Dispatch jobs via bash_command (curl to worker POST /).
   Describe: job ID, worker URL, payload summary, response.
7. If a worker fails: describe the failure exactly (error, exit code, output).
   Decide: destroy and reprovision, or wait. Never SSH in and tinker.
8. If no pending jobs and VMs are idle: describe the situation. Consider
   destroying idle VMs to save cost.

=== PERMITTED EFFECTS ===
VMAllocated, VMDeallocated, JobStarted, JobCompleted, JobFailed,
VMObserved, NoOp, ClarificationRequest

=== HARD STOPS ===
- If you detect a loop: describe the pattern and request clarification.
- If budget critical: describe spend and request abort.
"""

agent = create_deep_agent(
    model=config.agent_models["provisioner"],
    instructions=PROVISIONER_INSTRUCTION,
    on_before_compress=otio_aware_compress,
    history_processors=[...],
    include_todo=False,
    include_filesystem=False,
    include_plan=False,
    include_memory=False,
    include_skills=True,
    include_subagents=True,
    include_builtin_subagents=True,
    web_search=True,
    web_fetch=True,
)
```

#### 10.1.2 Why the Provisioner is an agent

VM provisioning is **not deterministic**. The Vast.ai marketplace is dynamic —
offers appear and disappear, prices fluctuate, GPU types vary by host, images
have different CUDA/driver versions, network topologies differ, and SSH ports
are randomly assigned. A deterministic script cannot:

- **Reason about failure** — "This offer failed because the host has CUDA 12.4
  but the image needs 12.6. I should filter for CUDA 12.6+ next time."
- **Learn across runs** — "Last time, RTX 4090 on host X worked perfectly for TTS.
  Let me prefer that host."
- **Research requirements** — "I don't know how much VRAM LTX-2.3 needs.
  Let me search for authoritative specs."
- **Escalate intelligently** — "Three different offers failed with the same error.
  This is a systemic issue, not a bad offer. I should ask the operator."

The Provisioner learns by having its past outputs (which contained descriptions
of successes and failures) stored in the event store. The Memory Agent (future
work) will compact this into durable knowledge. For now, the agent's own prior
turns in the message history serve as working memory.

#### 10.1.3 Agent output is parsed for effects

Like all agents, the Provisioner does not produce effects directly. It produces
natural language text describing what it did:

```
I searched Vast.ai and found offer 12345: RTX 4090, 24GB VRAM, $0.45/hr.
I provisioned it with image vastai/worker:tts --disk 64.
Instance ID is 67890. Worker is responding on http://1.2.3.4:8880/.
I dispatched job job-abc to the worker. The worker returned 202 Accepted.
```

The parser extracts:
- `VMAllocated(instance_id="67890", offer_id="12345", ...)`
- `JobStarted(job_id="job-abc", vm_instance_id="67890", ...)`

The handler appends these effects to the SQLite event store. The Provisioner
is barely aware of this process.

### 10.2 VM Lifecycle Management

#### 10.2.1 Offer selection: agent reasoning, not deterministic criteria

**V7.1 architectural decision:** The Provisioner uses raw `bash_command` for ALL
Vast.ai operations — no Python wrapper, no SDK abstraction, no structured parsing
layer. This is intentional. CLI outputs change; a hardcoded Python parser breaks
when a column header changes. An LLM reading raw stdout adapts: it figures out
where the `offer_id` moved and continues. We trade deterministic parsing for
self-healing execution.

Python code generation by the agent is deferred. While it might seem appealing to
have the agent write a Python script for complex operations, LLM indentation
errors and import hallucinations create more problems than they solve. Bash is
simpler, more predictable, and the LLM has extensive training on shell commands.

The Provisioner agent searches Vast.ai via `bash_command`, evaluates raw offer
text by reasoning in natural language, and picks the best offer. The agent may
override its own reasoning based on memory (e.g., "I had a failure on host X last
time, skip it even if it looks cheap").

**Start with one VM, then escalate.** The agent provisions exactly one VM for
the first job of each type. It confirms health (`GET /` responds) before
provisioning additional VMs. The agent decides when to escalate based on queue
depth, worker health, and cost projections — not a hardcoded threshold.

| Criterion | TTS Job (`job_type="tts"`) | LTX Job (`job_type="ltx"`) |
|---|---|---|
| GPU type | RTX 4090 or A6000 (agent preference) | RTX A6000 (48 GB) |
| Min VRAM | 24 GB | 48 GB |
| Max price/hr | `$config.max_tts_cost_hr` (default $0.80) | `$config.max_ltx_cost_hr` (default $1.20) |
| Disk | ≥ 100 GB | ≥ 200 GB |
| Sort key | agent-ranked (cost × reliability) | agent-ranked (cost × reliability) |
| Max concurrent | 3 (soft limit, agent decides) | 3 (soft limit, agent decides) |

```python
# Agent calls bash_command, reads raw output, reasons in natural language
raw_offers = bash_command("vastai search offers --type on-demand --raw | head -20")
# Agent evaluates offers in its head: GPU, VRAM, CUDA, price, host history
# Agent picks based on reasoning + memory + context
```

#### 10.2.2 VM allocation via bash tool

The agent constructs the `vastai create instance` command as a string and
executes it via `bash_command`. It reads the raw stdout, waits for the worker
HTTP endpoint to respond, and describes the result in its output.

```python
# Agent searches, picks, provisions, health-checks
bash_command("vastai create instance 7843219 --image vastai/worker:tts --disk 64")
# Agent reads stdout for instance ID
bash_command("curl -s http://1.2.3.4:8880/ || echo 'WORKER DOWN'")
# Agent describes: "Instance 9912834 created on offer 7843219. Health check
# returned {'status':'ready'}. Worker is available."
```

#### 10.2.3 VM deallocation

The agent destroys VMs when they are idle and no pending jobs exist. It
describes the deallocation in its output; the parser extracts `VMDeallocated`.

```python
# Agent reads GSA: no pending jobs for VM 67890. Boot time 3 min.
# Next job may arrive in 10 min. Keeping it costs $0.03. Destroy.
bash_command("vastai destroy instance 67890")
# Agent describes: "No pending jobs for stage audio. VM 67890 idle for 8 minutes.
# Destroying to save $0.03."
```

### 10.3 Job Delivery

#### 10.3.1 VM Workers Are DeepAgents

A VM worker is a **deepagent** with `GET /` and `POST /`, just like every other
agent in the architecture. The Provisioner talks to it the same way it talks to
the GSA: via HTTP.

The Provisioner dispatches a job by POSTing natural language to the worker:

```bash
# Provisioner constructs a payload describing the work
bash_command('curl -s -X POST http://worker-ip:8880/ -H "Content-Type: application/json" -d \'{
  "text": "Generate TTS audio for: The Federal Reserve raised rates...",
  "voice": "V1",
  "output_path": "/tmp/audio/A1_Narration-3-1.wav"
}\'')
```

The worker agent receives this, runs `bash_command` to execute TTS inference,
and returns natural language describing the result:

```
TTS generation completed for block A1:3:1. Output saved to
/tmp/audio/A1_Narration-3-1.wav. Duration: 4.23 seconds. Model: Qwen3-TTS.
```

The Provisioner reads this response and includes it in its own output, which the
parser extracts effects from. There is no special VM protocol. Just HTTP between
agents.

#### 10.3.2 Job result collection

The Provisioner polls workers for job status by curling their health endpoints.
When a job completes, the worker's response contains the result description.
The Provisioner incorporates this into its own natural language output.

```python
# Provisioner polls worker
bash_command("curl -s http://worker-ip:8880/")
# Worker returns: "Job job-abc completed. Artifact: /tmp/out.wav, duration 5.2s"
# Provisioner describes: "Worker for job-abc reports completion. Audio duration
# is 5.2 seconds. I will now consider this job done."
```

#### 10.3.3 Job failure and retry policy

When a worker reports a failure (or the Provisioner detects one via health check), the Provisioner reads the `JobFailed` effect from the event store. The `retryable` field is a hint — the Provisioner may override it based on its own reasoning. Examples of override authority:

- **Retry count exhaustion:** After 3 consecutive failures of the same job, the Provisioner may decide `retryable=False` and emit `ClarificationRequest` instead of requeueing.
- **Systemic failure pattern:** If multiple jobs fail with the same `failure_category` on the same GPU type, the Provisioner may conclude the issue is infrastructure-level and halt requeueing.
- **Cost guard:** If the run is near budget and a retry would push it over, the Provisioner may refuse to retry even if `retryable=True`.

The Provisioner does not emit `JobRequeued` (that is an Audio/Video Agent artistry effect). Instead, it emits `QueueJob` with adjusted `params` (e.g., lower batch size, different GPU type) for the retry attempt, or `ClarificationRequest` if it decides the failure requires human intervention.

### 10.4 Learning and Memory

**V7.1 scope:** The Provisioner has no dedicated memory tool. Its working memory
is its message history (prior turns in the same run). Cross-run memory is
**future work** — a Memory Agent will read the event store, compact
important information, and make it available to agents.

For now, the Provisioner learns within a single run by observing its own prior
turns in the message history. If it provisioned a VM successfully earlier in the
run, it sees that in its history and can reference it.

```
I see from my prior turn that I provisioned instance 9912834 on offer 7843219
for stage audio. That instance is still healthy. I will reuse it for the next
pending job instead of provisioning a new VM.
```

---
