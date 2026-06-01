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

In V7.1, the test suite consists exclusively of three real-world BDD integration tests that drive actual agent and database endpoints over the HTTP network boundary. There is no Python-level mocking of internal agent state or databases. The tests are executed using the `pytest` and `pytest-bdd` frameworks.

### 21.0.1 Testing Principles

* **Uncompromised Thoroughness**: Test Efficiency and Cost Control MUST NEVER BE A FACTOR IN CONSTRUCTING TESTS - THOROUGHNESS IS KEY. All integration and BDD suites must exhaustively exercise the real VM creation steps, network tunnels, worker endpoints, and artifact validation to mirror actual production conditions without shortcutting behavior for performance or budget.

---

## 21.1 BDD Integration Test Suite

* **Scenario BDD Test: Scenario Agent Script Writing and Timeline Generation (UA-2-Real)**
  - **Pre-conditions**: GSA event store is clean. Scenario Agent and GSA are running on the host.
  - **Stimulus**: POST request to the Scenario Agent with instructions to write a 3-scene documentary script.
  - **Assertion**: Scenario Agent appends an `UpdateScript` effect containing valid script blocks (with dialog and visual prompts for all 3 slots) to the event store, and the OTIO timeline in the GSA updates automatically.

* **Real Vast.ai TTS Provisioning BDD Test (UA-10-Real) (VERY IMPORTANT DO NOT ALTER)**
  - **Pre-conditions**: GSA contains a queued TTS job, the budget has remaining funds, and the Provisioner Agent is configured for real Vast.ai cloud provisioning.
  - **Stimulus**: Provisioner Agent is woken up via POST request.
  - **Assertion**: Provisioner queries Vast.ai GPU offers, selects the cheapest suitable offer under $1.50/hour, allocates the VM, and logs `VMAllocated`. Once the VM is running and the worker agent is healthy, the Provisioner dispatches the TTS job. The worker processes the job, generating a non-zero size audio file. The Provisioner downloads the audio artifact, updates GSA with `JobCompleted`, and deallocates the VM (emitting `VMDeallocated` with reason `job_done`).

* **Real Vast.ai Video VM Lifecycle BDD Test (UA-11-Real)**
  - **Pre-conditions**: GSA contains a queued video (LTX) job, the budget has remaining funds, and the Provisioner Agent is configured for real Vast.ai cloud provisioning.
  - **Stimulus**: Provisioner Agent is woken up via POST request.
  - **Assertion**: Provisioner queries Vast.ai GPU offers (VRAM >= 24GB, under $2.00/hour), allocates the instance, and logs `VMAllocated`. Once the instance is running, the Provisioner dispatches the video job. The worker generates the clip, producing a non-zero size video file. The Provisioner downloads the clip, logs `JobCompleted`, and deallocates the VM.

* **Audio Agent Narration & Measurement BDD Test (UA-8-Real)**
  - **Pre-conditions**: GSA has a written script block needing audio narration, the budget has remaining funds, and the Audio Agent is running on the host.
  - **Stimulus**: Audio Agent is woken up via POST request.
  - **Assertion**: Audio Agent detects the narration block and appends a `QueueJob` effect for a TTS job. When the Provisioner completes the TTS job with a generated WAV file, a second wakeup stimulus triggers the Audio Agent to download the WAV file, call measurement tools to record physical properties, compare the duration against target tolerance, and append `ReconciliationComplete`.

* **Early/Incremental Assembly Timeline Validation BDD Test (UA-9-Real)**
  - **Pre-conditions**: GSA contains partially completed audio and video jobs for slots, and the Assembly Agent is running on the host.
  - **Stimulus**: Assembly Agent is woken up via POST request as slots progress.
  - **Assertion**: Assembly Agent performs incremental dual-threshold validation on completed slots. If a slot's audio/video durations mismatch, it appends a `ReconciliationFailed` or `SuggestedFix` effect to trigger early corrections. If slots pass validation, it merges the media tracks and appends `PipelineComplete` once all slots are verified.

* **Collaborative Cross-Agent Self-Correction BDD Test (UA-12-Real)**
  - **Pre-conditions**: Audio Agent has failed narration reconciliation for a slot and has logged `ReconciliationFailed` in the GSA event store. The Scenario Agent is running on the host.
  - **Stimulus**: Scenario Agent is woken up via POST request.
  - **Assertion**: Scenario Agent scans the event store, detects the `ReconciliationFailed` feedback, automatically rewrites/shortens the narration text to fit the target slot window, and appends `UpdateScript`. The subsequent Audio Agent activation generates the new audio conforming to the constraint, closing the self-correction loop.

---

*V7.1 Architecture — pydantic-deep, agent HTTP endpoints, SQLite event store (EventStoreDB for distributed deployments), prompt-based rules, agentic Provisioner with bash/research/memory tools, no watcher.*