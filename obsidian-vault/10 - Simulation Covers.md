---
{
  "title": "Covered-Simulation Specifications",
  "section": "10",
  "tags": [
    "architecture",
    "testing",
    "covered-simulation",
    "v7.1"
  ]
}
---

[[00 - Index|◀ Back to Index]]

# 🛡️ Covered-Simulation Specifications

This module establishes the master registry of all **35 Simulation Covers (SC)**. Under the **Covered-Simulation** invariant (Global Invariant #7), any simulated process or capability used in testing must have an equivalent non-simulated, live verification test backing it.

---

## 1. Master Covered-Simulation Registry

### Category A: Module 01 - Philosophy & Topology (Global Invariants)

| Cover ID | Consequential Invariant / Claim | Live Backing Integration Test | Core Boundary Verification Target |
| :---: | :--- | :--- | :--- |
| **SC-01** | Event Log as Sole Source of Truth | `test_gsa_wal_concurrency_isolation` | Proves GSA state is derived solely by replaying event log from sequence 0. |
| **SC-02** | Mutations via typed Effects | `test_gsa_wal_concurrency_isolation` | Asserts direct updates to database write loops fail; all writes must go through Pydantic Effects. |
| **SC-03** | Prompt-driven state (no state machine) | `test_scenario_agent_live_prompt_turn` | Asserts Scenario transitions are based on prompt reasoning blocks in deep agent, not hardcoded switches. |
| **SC-04** | No timeouts in production code | `test_scenario_agent_live_prompt_turn` | Proves LLM HTTPS queries run to completion without hardcoded timeouts. |
| **SC-05** | Real engines only in production | `test_scenario_agent_live_prompt_turn` | Runs actual DeepSeek chat API endpoints rather than simulated dry models. |
| **SC-06** | Category-conditioned instructor extraction | `test_scenario_agent_live_prompt_turn` | Asserts natural language responses parse successfully using instructor without regex. |
| **SC-07** | PlainTextResponse HTTP boundaries | `test_ssh_handshake_and_docker_health` | Asserts agent GET/POST responses return conversational text with `text/plain` headers. |
| **SC-08** | Situation-driven agent tasking | `test_audio_agent_tts_job_queueing` | Proves agents discover tasks by reading state from GSA projection endpoint. |
| **SC-09** | Stateless agent process turns | `test_audio_agent_tts_job_queueing` | Verifies agent ASGI processes hold no cached memory between wakeup requests. |
| **SC-10** | Turn serialization via LoopBoundLock | `test_gsa_wal_concurrency_isolation` | Proves overlapping wakeups are queued and processed sequentially without DB lock failures. |
| **SC-11** | PUT Interruption / electric bolt | `test_ssh_handshake_and_docker_health` | Verifies PUT requests cancel active asyncio tasks and spawned subprocesses immediately. |

---

### Category B: Module 03 - Timeline Projections

| Cover ID | Consequential Invariant / Claim | Live Backing Integration Test | Core Boundary Verification Target |
| :---: | :--- | :--- | :--- |
| **SC-12** | OTIO Canonical representation | `test_coordinate_timeline_dynamic_drift` | Asserts GSA projection formats compile to compliant OpenTimelineIO schema slots. |
| **SC-13** | No digital stretching or shrinking | `test_coordinate_timeline_dynamic_drift` | Asserts timing drift is resolved by shifting coordinates, never using `atempo` filters. |
| **SC-14** | No looping or reusing media | `test_coordinate_timeline_dynamic_drift` | Asserts background music and narration clips are never looped or duplicated. |
| **SC-15** | No gap media padding | `test_coordinate_timeline_dynamic_drift` | Asserts no empty placeholder tracks are injected to fill timeline gaps. |
| **SC-16** | Track collision prevention checks | `test_coordinate_timeline_dynamic_drift` | Verifies that overlapping timespans on the same physical media track raise collision errors. |

---

### Category C: Module 05 - Provisioning & GPU Infrastructure

| Cover ID | Consequential Invariant / Claim | Live Backing Integration Test | Core Boundary Verification Target |
| :---: | :--- | :--- | :--- |
| **SC-17** | Progressive doubling fleet escalation | `test_vast_create_and_destroy_lifecycle` | Asserts Provisioner starts with 1 VM, verifying happy-path before doubling. |
| **SC-18** | GPU VRAM matching (TTS) | `test_provisioner_vast_offers_search` | Matches TTS jobs against RTX 4090/A6000 (VRAM >= 24GB, cost < $0.80/hr). |
| **SC-19** | GPU VRAM matching (LTX) | `test_provisioner_vast_offers_search` | Matches LTX video jobs against A6000/H100 (VRAM >= 48GB, cost < $1.20/hr). |
| **SC-20** | Unique VM HTTPS endpoints | `test_ssh_handshake_and_docker_health` | Asserts concurrent worker VMs bind to distinct ports (preventing overlap on 8888). |
| **SC-21** | Destroy unreachable ghost VMs | `test_vast_create_and_destroy_lifecycle` | Verifies Provisioner destroys VMs failing to boot within grace period. |

---

### Category D: Module 07 - Security, Traceability & Auditing

| Cover ID | Consequential Invariant / Claim | Live Backing Integration Test | Core Boundary Verification Target |
| :---: | :--- | :--- | :--- |
| **SC-22** | Budget limit hard gate | `test_budget_limit_aborted_gate` | Aborts run and destroys leased VMs if cumulative costs cross budget cap ($10.00). |
| **SC-23** | Duplicate-Effects Loop Detection | `test_budget_limit_aborted_gate` | Pauses turn and emits `ClarificationRequest` if 2 identical effects hash in 10 turns. |
| **SC-24** | No-Progress Loop Detection | `test_budget_limit_aborted_gate` | Pauses turn and alerts Operator if checklist progress is zero for 5 turns. |

---

### Category E: Module 08 - Testing, Concurrency & Rollout (BDD Suites)

| Cover ID | Consequential Invariant / Claim | Live Backing Integration Test | Core Boundary Verification Target |
| :---: | :--- | :--- | :--- |
| **SC-25** | Scale Timeline Integrity Test | `test_gsa_wal_concurrency_isolation` | Compiles 120-block timeline checking for perfect WAL database durability. |
| **SC-26** | Multi-VM Fleet Coordination Test | `test_provisioner_vast_offers_search` | Asserts tasks route to correct VM configurations. |
| **SC-27** | Localized Segment Recovery Test | `test_gsa_wal_concurrency_isolation` | Asserts that failing 2 blocks in a 100-block run retries only those 2 blocks. |
| **SC-28** | Infrastructure Preemption Recovery | `test_vast_create_and_destroy_lifecycle` | Proves VM preemption triggers recovery by replaying event log. |
| **SC-29** | Multi-Scene Visual Cuts Test | `test_audio_loudness_normalizer_compilation` | Asserts 10-scene transitions are simple cuts with zero blank frame gaps. |
| **SC-30** | Accumulative Sync Drift Correction | `test_coordinate_timeline_dynamic_drift` | Asserts timing sync drift is trimmed below 0.05 seconds. |
| **SC-31** | Audio Loudness Normalization | `test_audio_loudness_normalizer_compilation` | Verifies final audio gain normalization of -16.0 LUFS +/- 1.0 LUFS. |
| **SC-32** | End-to-End Orchestration | `test_scenario_agent_live_prompt_turn` | Verifies pipeline phase progression from script to final cuts. |
| **SC-33** | Scenario-to-Audio Happy Path | `test_audio_agent_tts_job_queueing` | Asserts Scenario block updates trigger immediate audio queueing. |
| **SC-34** | Muxing and Timeline Composition | `test_audio_loudness_normalizer_compilation` | Asserts FFmpeg output container meets web specifications (H.264/AAC). |
| **SC-35** | Integrated Dynamic Offset Shift | `test_coordinate_timeline_dynamic_drift` | Proves DurationAdjusted events shift movie length precisely by the delta change. |
| **SC-36** | Perplexity Web Fact-checking | `test_perplexity_verify_live` | Verifies that perplexity_verify queries the live API and parses citations correctly. |

---

*Covered-Simulation Invariant Registry — V7.1. Verified via tests/units/test_consequential_claims.py.*

---

## 2. Core Concepts & Definitions

### The Unit: The Simulation Cover (SC)
A **Simulation Cover (SC)** is an individual integration or unit test case $t$ designed to validate a simulated capability or interface $c$.

For test case $t$ to qualify as a valid **Simulation Cover** for capability $c$, it must satisfy four structural criteria:
1. **Target Production Execution:** $t$ must invoke the actual production script, module, or code path that implements $c$ (rather than a mock wrapper or test-only replica).
2. **Live Boundary Interaction (Mandatory Live Execution):** $t$ must communicate directly with the real-world/physical dependency of $c$ by executing a live shell command, calling a live external API, writing to physical disk, or executing a physical binary like `ffmpeg`. **There are absolutely no exceptions for offline or dry-run fallbacks.**
3. **No Boundary Mocking:** $t$ must **not** intercept, mock, or monkey-patch the production code path or the external/physical boundary under test. Any test that falls back to a mock, stub, or simulated response when running offline (such as mock vastai, mock LLMs, or mock search endpoints) fails Condition 2 and Condition 3 and is strictly **DISQUALIFIED** as a valid Simulation Cover. To be valid, a Simulation Cover must fail immediately (raising a fatal error or assertion failure) if the live network, external API credentials, or physical binaries are missing.
4. **Scoped Verification:** $t$ is **not** required to execute the entire system workflow or agent loop end-to-end. It is strictly a scoped test validating that a single live production element's interface and behavior match the simulator's mock assumptions.


### The Metric: Simulation Coverage
**Simulation Coverage** is the percentage of all simulated boundaries in the test suite that are backed by at least one valid **Simulation Cover**.

#### Mathematical Formulation:
Let $S = \{s_1, s_2, \dots, s_n\}$ be the set of all simulated interfaces, stubs, or mock capabilities used in the test suite.

Let $T_{cover} \subseteq T$ be the set of all valid **Simulation Covers** in the test suite.

For each simulated interface $s_i \in S$, we define the cover mapping function:
$$\text{Covered}(s_i) = \begin{cases} 
1 & \exists t \in T_{cover} \text{ such that } t \text{ is a valid Simulation Cover for } s_i \\
0 & \text{otherwise}
\end{cases}$$

The **Simulation Coverage Percentage** is calculated as:
$$\text{Simulation Coverage \%} = \left( \frac{\sum_{i=1}^{n} \text{Covered}(s_i)}{n} \right) \times 100$$
