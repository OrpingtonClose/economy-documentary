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

In V7.1, the test suite consists of real-world BDD integration tests that drive actual agent and database endpoints over the HTTP network boundary. There is no Python-level mocking of internal agent state or databases. The tests are executed using the `pytest` and `pytest-bdd` frameworks.

### 21.0.1 Testing Principles

* **Uncompromised Thoroughness**: Test Efficiency and Cost Control MUST NEVER BE A FACTOR IN CONSTRUCTING TESTS - THOROUGHNESS IS KEY. All integration and BDD suites must exhaustively exercise the real VM creation steps, network tunnels, worker endpoints, and artifact validation to mirror actual production conditions without shortcutting behavior for performance.

---

## 21.1 BDD Integration Test Suite

* **Scale Timeline Integrity & Gap Validation Test (Hour-Long Movie Scaffolding)**
  - **Pre-conditions**: The SQLite event store contains a script for an hour-long documentary (120 blocks, 3600s target). The Assembly Agent is running on the host.
  - **Action**: The Provisioner schedules all parallel rendering jobs (120 TTS, 120 Video), and all rendering jobs are completed with media file metadata.
  - **Assertion**: The Assembly Agent compiles the entire 120-slot OpenTimelineIO sequence. The compiled sequence contains zero gaps or overlaps, matches the 3600s target duration, and the SQLite WAL database performance is stable under load.

* **Multi-VM Job Dispatch & Fleet Coordination Test**
  - **Pre-conditions**: The job queue in GSA contains 50 pending audio and video rendering tasks. The Provisioner is running on the host.
  - **Action**: The Provisioner registers multiple active worker VM instances and initiates parallel job claiming across the active fleet.
  - **Assertion**: Jobs are routed to distinct worker VMs based on capability matches, and the event store logs each job's completion with its specific handling VM instance ID.

* **Localized Segment Recovery & Pipeline Completeness Test**
  - **Pre-conditions**: A 100-block documentary run has 98 blocks completed and 2 blocks failed. The Provisioner and Assembly Agent are running on the host.
  - **Action**: The Provisioner detects the failure logs in the event store.
  - **Assertion**: The Provisioner isolates and retries only the 2 failed jobs on a fresh or recycled worker VM. The Assembly Agent halts timeline compilation until these retried segments are complete, resulting in a 100% complete compiled movie with all 100 media slots present.

* **Provisioning Happy-Path Escalation Test**
  - **Pre-conditions**: The queue contains multiple pending rendering and TTS jobs. The Provisioner is running on the host.
  - **Action**: The Provisioner initiates provisioning with exactly 1 VM, escalates by doubling the VM count to 2 VMs, and continues doubling to the soft limit of 4 VMs.
  - **Assertion**: Initial jobs are completed sequentially on the single VM. As the fleet escalates, jobs are successfully routed and executed in parallel across the 2 VMs and then across the 4 VMs.

* **Automated Infrastructure Failure Recovery Test (Provisioning Durability)**
  - **Pre-conditions**: The pipeline queue contains pending jobs. The Provisioner is running on the host.
  - **Action**: Worker VMs experience cold-start boot timeouts and spot preemption mid-job. The Provisioner process is terminated and restarted mid-run.
  - **Assertion**: The Provisioner condemns VMs that timeout and provisions replacements. It reschedules jobs interrupted by preemption and allocates replacement VMs. Upon restart, the Provisioner replays the event log to discover active VMs and resume job routing without double-provisioning.

* **Multi-Scene Transition and Visual Integrity Test**
  - **Pre-conditions**: A script with 10 scenes, each scene containing multiple blocks. The Assembly Agent is running on the host.
  - **Action**: The rendering jobs for all audio and video blocks are completed, and the Assembly Agent applies a cross-dissolve transition at scene boundaries.
  - **Assertion**: The compiled timeline contains correct transitions at scene boundaries with zero track misalignment.

* **Accumulative Duration Drift Correction Test**
  - **Pre-conditions**: A 60-block timeline where each segment has slightly mismatching audio/video durations.
  - **Action**: The Assembly Agent evaluates track alignment and applies duration-stretching or trim effects to sync the video and audio tracks.
  - **Assertion**: The final maximum sync drift at any point in the timeline is less than 0.05 seconds.

* **Audio Loudness & LUFS Verification at Scale Test**
  - **Pre-conditions**: 60 audio segments with varying loudness levels and different voice roles.
  - **Action**: The Assembly Agent processes the final timeline mix using loudness normalization filters.
  - **Assertion**: The output integrated loudness matches -16.0 LUFS +/- 1.0 LUFS, and true peak does not exceed -1.0 dBTP.

---

## 21.2 Progressive Production Rollout Workflow

To ensure systematic scalability and stability before embarking on full-scale rendering runs, production movie generation follows a progressive testing and rollout workflow:

1. **Incremental Test-Writing Flow**:
   - Integration tests are written and added to the suite **3 at a time** to incrementally cover complexity (e.g. scale scaffolding, preemption, loudness, transition boundaries).
2. **Ready Status Declaration**:
   - Once all BDD integration test suites are fully passing and verified, the pipeline is officially declared **ready**.
3. **Progressive Movie Generation Runs**:
   - Movie generation testing escalates sequentially across five target duration steps to verify compilation and rendering scaling limits:
     - **1 Minute** (Test convergence and alignment)
     - **3 Minutes** (Scene transitions validation)
     - **10 Minutes** (Fleet provisioning and routing validation)
     - **20 Minutes** (Budget and load monitoring under sustained execution)
     - **60 Minutes** (Full production run)
4. **Movie Generation Platform Authorization**:
   - Movie generation runs may be executed separately on a separately provisioned Vast.ai VM using the old LTX-video model (or on the local laptop if resources and keys permit).

---

*V7.1 Architecture — pydantic-deep, agent HTTP endpoints, SQLite event store (EventStoreDB for distributed deployments), prompt-based rules, agentic Provisioner with bash/research/memory tools, no watcher.*