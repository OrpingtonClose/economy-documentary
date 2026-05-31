---
{
  "title": "Unit Agent and Integration Tests",
  "section": "21",
  "tags": [
    "architecture",
    "v7.1",
    "testing"
  ]
}
---

<- [[20 - Glossary|Glossary]] | [[00 - Index|Index]]

# 21. Unit Agent and Integration Tests

In V7.1, agents are autonomous HTTP services that communicate asynchronously through the SQLite Event Store and query state via the Global State Agent (GSA). To validate these components without breaking their design invariants, all tests are constructed as **HTTP endpoint-only simulation tests**. 

There is **no Python-level mocking (`unittest.mock.patch`)** of agent internal structures or event store databases. Mocking is restricted strictly to the external HTTP network boundaries (i.e. simulating VM workers, Vast.ai responses, or downstream agent triggers). 

Furthermore, **no testing framework (such as pytest) is used**. The test suite runs as a standalone test runner script that starts all agent servers and background mock services, drives requests to their HTTP endpoints, polls the database for state transitions, and verifies the pipeline's behavior purely over HTTP network boundaries. This ensures the tests act as true external clients, matching production deployment patterns.

---

## 21.1 Functionality of Individual Agents

Before defining unit agent tests, we document the precise functionality and expected behavior of each individual agent:

### 21.1.1 Scenario Agent (Port 8001)
* **First Draft Creation**: When the event store contains no narration script, the Scenario Agent writes the first draft script containing one or more narration blocks (specifying speaker, text, scripted duration, and scene layout).
* **Narrative Revision on Back-Edge**: If a downstream agent appends a `reconciliation_failed` event (with failure type `gap_unexpected` or `voice_mismatch`), the Scenario Agent reads the failure details and rewrites the affected narration blocks, appending a revised script.
* **Effect Extraction**: Emits `UpdateScript`, `DeleteScene`, `ReorderScenes`, `NoOp`, or `ClarificationRequest`.

### 21.1.2 Audio Agent (Port 8002)
* **TTS Job Generation**: Identifies blocks with `status="scripted"` and queues a TTS job (`QueueJob`) specifying target voice, speed, and text.
* **Duration Assessment & Tolerances**: Receives duration measurements from worker outputs and evaluates them against:
  $$\text{tolerance} = \max(\text{scripted\_sec} \times 0.15, 0.25\text{s})$$
* **Escalation**: Requeues failing blocks with adjusted parameters up to 5 times. On the 5th failure, emits `reconciliation_failed`.
* **Effect Extraction**: Emits `QueueJob`, `JobApproved`, `JobRequeued`, `DurationAdjusted`, `ReconciliationFailed`, `ReconciliationComplete`, `NoOp`, or `ClarificationRequest`.

### 21.1.3 Provisioner Agent (Port 8003)
* **Infrastructure Management**: Listens for queued jobs, queries Vast.ai GPU offers, rents instances, verifies worker agent health over HTTP, and dispatches payloads.
* **Cost Preservation**: Automatically terminates idle VMs when the queue is clean and deallocates unresponsive instances after a grace period.
* **Effect Extraction**: Emits `VMAllocated`, `VMDeallocated`, `VMProvisionFailed`, `VMObserved`, `JobCompleted`, `JobFailed`, `JobStarted`, `NoOp`, or `ClarificationRequest`.

### 21.1.4 Assembly Agent (Port 8005)
* **Timeline Validation**: Prior to muxing, asserts that all slots are fully delivered, there are no overlapping tracks, and audio/video durations align.
* **Video Muxing**: Invokes `ffmpeg` to merge audio and video tracks into the final MP4.
* **Pipeline Completion**: Confirms the file was created and is playable, emitting `PipelineComplete` or `ProductionFailed`.

---
## 21.2 BDD Endpoint Simulation Test Suite

The test suite consists of three real-world BDD simulation tests that drive actual agent and database endpoints over the HTTP network boundary. There is no Python-level mocking of internal agent state or databases.

* **Scenario BDD Test: Scenario Agent Script Writing and Timeline Generation (UA-2-Real)**
  * **Pre-conditions**: GSA event store is clean. Scenario Agent and GSA are running on the host.
  * **Stimulus**: POST request to the Scenario Agent with instructions to write a 3-scene documentary script.
  * **Assertion**: Scenario Agent appends an `UpdateScript` effect containing valid script blocks (with dialog and visual prompts for all 3 slots) to the event store, and the OTIO timeline in the GSA updates automatically.

* **Real Vast.ai TTS Provisioning BDD Test (UA-10-Real)**
  * **Pre-conditions**: GSA contains a queued TTS job, the budget has remaining funds, and the Provisioner Agent is configured for real Vast.ai cloud provisioning.
  * **Stimulus**: Provisioner Agent is woken up via POST request.
  * **Assertion**: Provisioner queries Vast.ai GPU offers, selects the cheapest suitable offer under $1.50/hour, allocates the VM, and logs `VMAllocated`. Once the VM is running and the worker agent is healthy, the Provisioner dispatches the TTS job. The worker processes the job, generating a non-zero size audio file. The Provisioner downloads the audio artifact, updates GSA with `JobCompleted`, and deallocates the VM (emitting `VMDeallocated` with reason `job_done`).

* **Real Vast.ai Video VM Lifecycle BDD Test (UA-11-Real)**
  * **Pre-conditions**: GSA contains a queued video (LTX) job, the budget has remaining funds, and the Provisioner Agent is configured for real Vast.ai cloud provisioning.
  * **Stimulus**: Provisioner Agent is woken up via POST request.
  * **Assertion**: Provisioner queries Vast.ai GPU offers (VRAM >= 24GB, under $2.00/hour), allocates the instance, and logs `VMAllocated`. Once the instance is running, the Provisioner dispatches the video job. The worker generates the clip, producing a non-zero size video file. The Provisioner downloads the clip, logs `JobCompleted`, and deallocates the VM.

---

## 21.4 macOS Host Virtualization for Worker Isolation and Testing

When executing integration tests or running local workers on a macOS host and requiring isolated Linux environments, the following virtualization technologies are recommended:

* **Lume (Primary Recommendation)**:
  * **Description**: A single-binary CLI using Apple's native `Virtualization.framework` (avoiding QEMU translation layers). It provides near-native ARM64 execution performance.
  * **Capabilities**: Exposes an HTTP API and a Python SDK (`pylume`), aligning perfectly with the agent-orchestration model.
  * **VM Deployment**: Spin up pre-built ARM Ubuntu images from the Lume GitHub Container Registry with a single command: `lume run ubuntu-24.04:latest`.
  * **Multi-Server Support**: Multiple instances can be started via the API (`POST /lume/vms`) or Python SDK. Each VM receives its own IP on an internal network bridge, preventing port collision issues on the host.
  * **Logging**: Serial console outputs can be piped directly to a file on the macOS host. The native hypervisor prevents VM stuttering even under heavy host load.
  * **Isolation**: Supports fully internal VM networks, allowing ingress and egress controls from the host side without interference from other processes.

* **Lima (Mature Alternative)**:
  * **Description**: A YAML-configured VM utility. It is highly mature, well-documented, and stable.
  * **Capabilities**: Suitable when `containerd` is needed inside the VM. Multiple instances can be spawned using `limactl start template://ubuntu-lts` with unique names. They can share a `user-vnet` to communicate.
  * **Performance**: Slightly heavier than Lume due to some QEMU pathways.

* **Tart (Automation-Focused)**:
  * **Description**: A virtualization tool built specifically for CI/CD environments, utilizing Apple's `Virtualization.framework`.
  * **Capabilities**: Stores virtual machines in OCI registries, allowing you to `push` and `pull` VM states as simple container layers.

---

*V7.1 Architecture — pydantic-deep, agent HTTP endpoints, JSONL event store (EventStoreDB for distributed deployments), prompt-based rules, agentic Provisioner with bash/research/memory tools, no watcher.*