# Implementation Plan: Covered-Simulation BDD & Integration Continuum

This document outlines the blueprint for aligning all pipeline simulations with robust, non-simulated BDD and Integration Cover Tests under the **Covered-Simulation** invariant (Global Invariant #7).

---

## 1. Architectural Architecture & Mapping

Every simulated process used during test sweeps for execution performance or local isolation MUST have a corresponding **Simulation Cover (SC)** test that executes the actual production code against live APIs, remote SSH connections, and physical media processors.

```mermaid
graph TD
    subgraph Simulated Path
        SimTest[BDD Queue/Capacity Test] -->|Uses Mocks| MockVast[Vast Mocks]
        SimTest -->|Uses Mocks| MockLLM[DryRunModel]
        SimTest -->|Uses Mocks| MockMedia[FFmpeg color/nullsrc]
    end
    subgraph Live Covered Path (SC)
        SC_Vast[test_vast_create_and_destroy_lifecycle] -->|Real CLI/API| RealVast[Vast.ai API / Spot Lease]
        SC_LLM[test_scenario_agent_live_prompt_turn] -->|Real HTTP POST| RealLLM[DeepSeek Chat API]
        SC_Media[test_audio_loudness_normalizer_compilation] -->|Real Filters| RealFFmpeg[FFmpeg loudnorm/ffprobe]
    end
```

---

## 2. Gherkin BDD Specifications & Simulation Covers

For each of the 10 core simulated capabilities, we define the BDD scenario and its matching live validation cover test.

### SC-01: LLM Reasoning Cover
* **BDD Scenario**:
  ```gherkin
  Scenario: Ingesting a screenplay and generating script blocks via LLM
    Given the Scenario Agent is initialized with the production DeepSeek model
    When a raw screenplay text prompt is POSTed to the Scenario Agent
    Then the agent should query the live LLM API
    And the response must be parsed into valid ScriptBlock models
    And the Scenario Agent must append an UpdateScript effect to the event store
  ```
* **Cover Test (`test_scenario_agent_live_prompt_turn`)**: Invokes a live turn of the Scenario Agent using the DeepSeek API key, verifies HTTPS round-trip, and parses the output script blocks using `instructor`.

### SC-02 & SC-07: Vast.ai API Operations
* **BDD Scenario**:
  ```gherkin
  Scenario: Querying and parsing on-demand GPU offers from Vast.ai
    Given valid Vast.ai API credentials are loaded in the environment
    When the Provisioner Agent executes a Vast.ai search command
    Then the command must exit with code 0
    And the output table must contain valid GPU types and lease prices
  ```
* **Cover Test (`test_provisioner_vast_offers_search`)**: Calls the local `vastai search offers` CLI command using your credentials, checking for correct output structure and CLI version compatibility.

### SC-03: VM Instance Allocation
* **BDD Scenario**:
  ```gherkin
  Scenario: Leasing a live GPU instance and polling until running
    Given a valid offer ID is selected from the Vast.ai search results
    When the Provisioner Agent issues a create instance command
    Then a new contract ID must be successfully generated
    And polling the instance status must return "running" within the grace period
  ```
* **Cover Test (`test_vast_create_and_destroy_lifecycle`)**: Performs a live lease of the cheapest available GPU, monitors the creation lifecycle, parses connection details, and teardowns the VM immediately.

### SC-04: VM Worker Health
* **BDD Scenario**:
  ```gherkin
  Scenario: Probing the boot status of the remote worker container
    Given a running GPU VM is provisioned on port 9001
    When an HTTP GET request is sent to the worker URL
    Then the server must respond with status 200
    And the Content-Type header must be "text/plain"
    And the response body must be a plain natural language status description
  ```
* **Cover Test (`test_ssh_handshake_and_docker_health`)**: Spawns [mock_gpu_worker.py](file:///Users/orpington/Documents/economy-documentary-work/scripts/mock_gpu_worker.py) in the background to verify the API contract and port bindings over loopback sockets.

### SC-05 & SC-06: TTS & LTX Job Dispatch
* **BDD Scenario**:
  ```gherkin
  Scenario: Queueing narration and video jobs autonomously
    Given a new script block is appended to the event store
    When the Audio and Video agents poll GSA
    Then the Audio Agent must queue a TTS job for narration slots (A1:)
    And the Video Agent must queue an LTX job for visual slots (V1:)
    And jobs must remain grouped and isolated by track type
  ```
* **Cover Test (`test_audio_agent_tts_job_queueing` & `test_video_agent_ltx_job_queueing`)**: Seeds GSA slots and asserts that agents dynamically parse state and queue jobs with correct parameters.

### SC-08: Timeline Dynamic Offset Cascade
* **BDD Scenario**:
  ```gherkin
  Scenario: recalculating slot timings on duration adjustment
    Given a timeline containing 3 blocks is active
    When a DurationAdjusted event increases block 1 duration by 2.0 seconds
    Then GSA must update the start/end coordinates of blocks 2 and 3
    And the total timeline duration must increase exactly by 2.0 seconds
  ```
* **Cover Test (`test_coordinate_timeline_dynamic_drift`)**: Verifies offset shifting math directly on the GSA projection engine.

### SC-29/SC-31/SC-34: Audio Loudness Normalization & Assembly
* **BDD Scenario**:
  ```gherkin
  Scenario: Compiling media and applying loudness normalization
    Given a timeline containing a loud narration clip is active
    When the Assembly Agent renders the final cut movie
    Then the output movie must contain a normalized audio track
    And the loudness of the final track must measure -16.0 LUFS +/- 1.0 LUFS
    And the emitted PipelineComplete event must conform to the expected schema
  ```
* **Cover Test (`test_audio_loudness_normalizer_compilation`)**: Invokes the actual production `run_movie_assembly` module, processes the loud wav through the real FFmpeg normalizer filter, measures final track LUFS, and verifies event schema.

### SC-09: Budget Gates
* **BDD Scenario**:
  ```gherkin
  Scenario: Aborting execution when charges exceed budget
    Given a pipeline budget limit of 1.00 USD
    When cumulative charges (tokens + GPU leases) cross 1.01 USD
    Then GSA must transition the current phase to "aborted"
    And the Provisioner must destroy all running VMs
  ```
* **Cover Test (`test_budget_limit_aborted_gate`)**: Seeds a cost cap violation and asserts pipeline state abort triggers.

### SC-10: WAL Concurrency
* **BDD Scenario**:
  ```gherkin
  Scenario: Replaying log events under parallel writes
    Given GSA is configured in SQLite WAL mode
    When multiple microservices write events concurrently using direct SQLite connection queries
    Then GSA must reconstruct projections from sequence 0 without locking database transactions
  ```
* **Cover Test (`test_gsa_wal_concurrency_isolation`)**: Asserts lock-free writes and state reconstruction under high parallel database writes.

---

## 3. Implementation Steps & Validation Checklist

- [x] **Registry Definition**: Formalize all 10 Simulation Covers inside the technical specifications.
- [x] **Dynamic Porting**: Replace hardcoded localhost GSA URLs with the environment-configurable `AgentRegistry`.
- [x] **Test Scaffolding**: Write the BDD cover tests inside `test_consequential_claims.py`.
- [ ] **Runner Verification**: Run the tests using `python tests/units/run.py` to ensure live verification passes.
