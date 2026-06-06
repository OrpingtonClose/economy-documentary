# Compliance Analysis Report — Pipeline and Scripts

## Executive Summary
This report presents a paragraph-to-paragraph compliance check of Python and shell scripts under the `pipeline/` and `scripts/` directories against the technical specifications documented in the `obsidian-vault/` directory.

### Key Findings
1. **Natural Language Invariant [R3]**: Active worker scripts (`vm_agent.py` and `mock_gpu_worker.py`) communicate strictly in plain-text conversational prose. However, the legacy script `gpu_worker.py` returns structured `exit=...` outputs on fallback bash commands, and `tts_worker.py` returns structured `vram=...` and `mode=...` parameter outputs. These are dead/inactive files but present in the codebase.
2. **GPU Infrastructure Discrepancies**: Significant structural discrepancies exist between the active GPU provisioning scripts (`provision_central.py`, `vm_onstart_ltx.sh`, `vm_onstart_tts.sh`) and the design specifications in `05 - Provisioning and GPU Infrastructure.md`. These include mismatched TCP ports (8880 vs. 9000+), different container base images (`ubuntu:22.04` vs. `vastai/worker:*`), dynamic repository cloning vs. static folders, and LLM-driven provisioning vs. hardcoded doubling logic.
3. **Source Code Mapping [R1]**: All python files under `pipeline/` and `scripts/` have been mapped against the corresponding specification documents.

---

## 1. Natural Language Invariant Check [R3]

### Specification Reference:
* **`05 - Provisioning and GPU Infrastructure.md` (§3.2 "HTTP API Surface")**:
  - `GET /` expects "Natural language status text".
  - `POST /` expects "Natural language result description".
* **`01 - Philosophy and Topology.md` (§1 "Core Philosophies")**:
  - "Agents speak to other agents only via plain conversational natural language."

### Active Worker Sanity:
1. **`scripts/vm_agent.py`**:
   - **GET Endpoint (Lines 242-244)**:
     ```python
     content = f"The GPU worker is currently healthy and active. {gpu_desc} {boot_desc} {model_desc}\n\nHere is the latest snippet from the system logs:\n{log_snippet}"
     return Response(content=content, media_type="text/plain")
     ```
     *Status*: **Fully Compliant**. Returns plain conversational status description.
   - **POST Endpoint (Lines 265-266)**:
     ```python
     result = await _agent.run(instruction)
     return Response(content=result.output, media_type="text/plain")
     ```
     *Status*: **Fully Compliant**. Output is deep agent monologue prose containing prefixes such as `RESULT:`, `QUESTION:`, or `ERROR:`.
2. **`scripts/mock_gpu_worker.py`**:
   - **GET Endpoint (Lines 52-53)**:
     ```python
     content = f"The GPU worker is currently healthy and active. {gpu_desc} {boot_desc} {model_desc}\n\nHere is the latest snippet from the system logs:\nWorker started successfully."
     return Response(content=content, media_type="text/plain")
     ```
     *Status*: **Fully Compliant**.
   - **POST Endpoint (Lines 111-112, 136-137, 141-142)**:
     ```python
     result_text = f"RESULT: Generated narration audio. Output: /workspace/output/{filename} ({size_str}, {duration}s)"
     # ...
     result_text = f"RESULT: Generated video clip. Output: /workspace/output/{filename} ({size_str}, {duration}s)"
     # ...
     result_text = "RESULT: Command executed successfully."
     ```
     *Status*: **Fully Compliant**. Outputs are free-text descriptions.

### Non-Compliant Legacy Invariants:
1. **`scripts/gpu_worker.py` (Lines 161-165)**:
   - When running a command falls back to raw bash, it returns structured key-value formatted text:
     ```python
     return Response(
         content=f"exit={result['returncode']}\n{result['stdout']}\n{result['stderr']}",
         media_type="text/plain",
         status_code=status,
     )
     ```
     *Violation*: Emits `exit={result['returncode']}` which is a structured key-value payload. However, this is legacy code.
2. **`scripts/tts_worker.py` (Lines 78-81)**:
   - The health endpoint returns structured keys:
     ```python
     return Response(
         content=f"ok {gpu} vram={vram_used:.1f}/{vram_total:.1f}GB mode=tts",
         media_type="text/plain",
     )
     ```
     *Violation*: Emits `vram=...` and `mode=...` key-value pairs at the agent communication boundary. However, this is legacy code.

### GSA/Orchestrator Parsing:
* **`server/agent_base.py` (Lines 354-356)**:
  Rule 6 under `COMMUNICATION_STYLE` enforces the natural language invariant on orchestrator agents:
  ```text
  6. NEVER USE STRUCTURED FORMATS. No JSON, no XML, no markdown tables, no EFFECT: markers, no labeled sections.
  ```
* **`server/effect_parser.py` (Lines 656-665)**:
  Uses an LLM (`deepseek-chat` / `instructor`) to extract a schema-validated `_SingleEffect` from the agent's raw natural language prose, ensuring that agents communicate strictly in conversational text.

---

## 2. GPU fleet allocation and provisioning script logic verification

### Discrepancy 1: Port Numbers
* **Spec (`05 - Provisioning and GPU Infrastructure.md` §3)**:
  "Workers run as ephemeral `FastAPI` nodes on port `9000+` inside Docker containers."
* **Implementation (`scripts/vm_agent.py` Line 277)**:
  ```python
  parser.add_argument("--port", type=int, default=8880)
  ```
  Active workers and bootstrap scripts run on port `8880` (or `8881` for TTS in legacy configurations) rather than `9000+`.

### Discrepancy 2: Docker / Container Images
* **Spec (`05 - Provisioning and GPU Infrastructure.md` §3)**:
  "...inside Docker containers (`vastai/worker:tts` or `vastai/worker:ltx`)."
* **Implementation (`scripts/provision_central.py` Line 153)**:
  ```python
  "--image", "ubuntu:22.04",
  ```
  Instead of utilizing customized pre-built worker images (`vastai/worker:*`), the active provisioning setups launch raw `ubuntu:22.04` base images and configure them dynamically on boot.

### Discrepancy 3: On-Start Boot Scripts
* **Spec (`05 - Provisioning and GPU Infrastructure.md` §3.1)**:
  The documented `onstart_tts.sh` pulls dependencies and runs `python3 -m worker.main --port 9000 --role tts` out of `/opt/worker/vm_worker`.
* **Implementation (`scripts/vm_onstart_tts.sh` Lines 44-45)**:
  ```bash
  nohup python repo/scripts/vm_agent.py --port 8880 > /workspace/worker.log 2>&1 &
  ```
  The active boot scripts (`vm_onstart_tts.sh`, `vm_onstart_ltx.sh`) clone the control repository into `/workspace/repo`, fetch model weights from HuggingFace (`Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign` or `Lightricks/LTX-2.3`), and start `vm_agent.py` on port `8880`.

### Discrepancy 4: Fleet Escalation / Doubling Policy
* **Spec (`05 - Provisioning and GPU Infrastructure.md` §2.1)**:
  "The Provisioner escalates fleet size via progressive doubling (1 VM → 2 VMs → 4 VMs max soft limit)."
* **Implementation (`server/agent_base.py` Lines 494-523)**:
  No doubling logic exists in the codebase. Instead, the Provisioner agent uses LLM reasoning via deep prompts to decide when to allocate or release single VM instances depending on the current list of pending jobs queried from the GSA.

### Discrepancy 5: Integration Test Database Initialization Bug
* **Location (`scripts/run_integration_test.py` Lines 46, 381-396)**:
  The integration test script instantiates the database manager `event_store` globally at the module level. Under the `main()` execution routine, it deletes existing sqlite database files (`events.db`) to clean up preceding test runs, but fails to re-invoke the database's schema initializer (`event_store._init_db()`). Consequently, the subsequent `event_store.append(...)` attempt crashes with `sqlite3.OperationalError: no such table: events` because the newly created SQLite file has no tables.
* **Proposed Fix**: Re-initialize the database schema by adding `event_store._init_db()` right after cleaning up the DB files.

---

## 3. Comprehensive Source Code Mapping [R1]

### 3.1 Pipeline Directory (`pipeline/`)

| File Path | Description / Purpose | Corresponding Spec Document | Compliance Status |
| :--- | :--- | :--- | :--- |
| `pipeline/otio_timeline.py` | Standalone timeline and track operations using OpenTimelineIO. | `03 - Timeline Projections.md` | **Compliant**. Establishes standard tracks (`V1_Video`, `A1_Narration`, `A2_Music`). |
| `pipeline/swarm_extraction/analyze.py` | Fact-checking, claim extraction, and tree-reactor enrichment subagent pipeline. | `04 - Agent Architecture and Systems.md`, `06 - Data Flows, Config, and Structure.md` | **Compliant**. Runs subagent verification loops, handles FRED/Perplexity queries, and integrates with Obsidian `VaultBuilder`. |
| `pipeline/swarm_extraction/condition_store.py` | Manages atomic conditions, admission criteria, and condition caches. | `02 - Event Store and Effect Schemas.md` | **Compliant**. Implements the semantic cache and condition database structures. |
| `pipeline/swarm_extraction/llm.py` | LLM API client wrapper for DeepSeek chat and deep models. | `04 - Agent Architecture and Systems.md` | **Compliant**. Wraps standard chat API formats. |
| `pipeline/swarm_extraction/models.py` | Pydantic data models for claims, conditions, and entities. | `06 - Data Flows, Config, and Structure.md` | **Compliant**. Implements structured data models. |
| `pipeline/swarm_extraction/scoring.py` | Scoring logic for subagent verification confidence. | `04 - Agent Architecture and Systems.md` | **Compliant**. Evaluates claim verification metrics. |
| `pipeline/swarm_extraction/search_providers.py` | Search query interface (FRED, Perplexity, Tavily, Google). | `05 - Provisioning and GPU Infrastructure.md`, `06 - Data Flows, Config, and Structure.md` | **Compliant**. Implements external tools. |
| `pipeline/swarm_extraction/search_tools2.py` | Extended search execution interface. | `05 - Provisioning and GPU Infrastructure.md` | **Compliant**. Extends agent tools. |
| `pipeline/swarm_extraction/tool_defs.py` | Definitions of Pydantic-AI tools for verification subagents. | `05 - Provisioning and GPU Infrastructure.md` | **Compliant**. Maps available tools. |
| `pipeline/swarm_extraction/tool_executor.py` | Action execution driver for subagents. | `04 - Agent Architecture and Systems.md` | **Compliant**. Standardizes execution. |
| `pipeline/swarm_extraction/tools.py` | Math calculations and basic extraction helper utilities. | `05 - Provisioning and GPU Infrastructure.md` | **Compliant**. Extends capability suite. |
| `pipeline/swarm_extraction/web_fetch.py` | Web fetching and HTML scraping helpers. | `05 - Provisioning and GPU Infrastructure.md` | **Compliant**. Implements network fetches. |

---

### 3.2 Scripts Directory (`scripts/`)

| File Path | Description / Purpose | Corresponding Spec Document | Compliance Status |
| :--- | :--- | :--- | :--- |
| `scripts/vm_agent.py` | Deep worker agent running on the GPU VM. Exposes FastAPI surface. | `05 - Provisioning and GPU Infrastructure.md` | **Compliant**. Communicates in plain natural language. Port is `8880` instead of `9000+`. |
| `scripts/gpu_worker.py` | Legacy/alternative thin HTTP worker node using raw shell wrapper execution. | `05 - Provisioning and GPU Infrastructure.md` | **Non-Compliant**. Returns raw structured `exit=...` lines on fallback. Legacy file. |
| `scripts/mock_gpu_worker.py` | Mock worker endpoints for integration tests. | `05 - Provisioning and GPU Infrastructure.md` | **Compliant**. Exposes health status and dummy results. |
| `scripts/provision_central.py` | Rents central unit CPU VM via Vast.ai CLI. | `05 - Provisioning and GPU Infrastructure.md` | **Compliant**. Spawns Ubuntu 22.04 central instance. |
| `scripts/provision_playground_staging.py` | Rents playground staging environment VM. | `05 - Provisioning and GPU Infrastructure.md` | **Compliant**. |
| `scripts/central_bootstrap.sh` | Sets up dependencies on the central CPU VM. | `01 - Philosophy and Topology.md` | **Compliant**. Configures supervisord/nginx and runs the orchestrator backend. |
| `scripts/gpu_bootstrap.sh` | Sets up packages and model files on GPU VMs. | `05 - Provisioning and GPU Infrastructure.md` | **Compliant**. |
| `scripts/ltx_video_worker_bootstrap.sh` | Legacy video worker bootstrap setting up `infra_agent` and `ltx-video-worker`. | `05 - Provisioning and GPU Infrastructure.md` | **Legacy**. Relies on retired `strands_agents` files. |
| `scripts/qwen3_tts_worker_bootstrap.sh` | Legacy TTS worker bootstrap. | `05 - Provisioning and GPU Infrastructure.md` | **Legacy**. Relies on retired `strands_agents` files. |
| `scripts/vm_onstart_ltx.sh` | Bootstrap shell script run via Vast.ai for LTX-2.3 workers. | `05 - Provisioning and GPU Infrastructure.md` | **Compliant**. Port is `8880`. |
| `scripts/vm_onstart_tts.sh` | Bootstrap shell script run via Vast.ai for Qwen3-TTS workers. | `05 - Provisioning and GPU Infrastructure.md` | **Compliant**. Port is `8880`. |
| `scripts/model_pin.py` | Lists HF repo pins and SHA256 hashes of models. | `07 - Security, Traceability, and Auditing.md` | **Compliant**. Enforces verification before execution. |
| `scripts/append_lesson.py` | Appends post-run learnings to `.provisioner_learnings.jsonl`. | `04 - Agent Architecture and Systems.md` | **Compliant**. |
| `scripts/playground_staging_bootstrap.sh` | Bootstraps local playground environments. | `08 - Testing, Concurrency, and Rollout.md` | **Compliant**. |
| `scripts/pr343_wait_till_terminal.py` | Helper script that waits until a pipeline run completes/fails. | `08 - Testing, Concurrency, and Rollout.md` | **Compliant**. |
| `scripts/replay_pipeline.py` | Event replay utility for debugging projections. | `07 - Security, Traceability, and Auditing.md` | **Compliant**. |
| `scripts/run_debug_gym_infused.py` | Runs the pipeline under `debug_gym_agent.py` oversight. | `08 - Testing, Concurrency, and Rollout.md` | **Compliant**. |
| `scripts/run_endpoint_tests.py` | Connection and sanity endpoint check. | `08 - Testing, Concurrency, and Rollout.md` | **Compliant**. |
| `scripts/run_integration_test.py` | Multi-agent integration test orchestrator. | `08 - Testing, Concurrency, and Rollout.md` | **Compliant**. |
| `scripts/run_ltx_2_3.py` | Wrapper execution script for LTX-2.3 video inference. | `05 - Provisioning and GPU Infrastructure.md` | **Compliant**. |
| `scripts/run_production_pipeline.py` | Drives pipeline orchestrator loop. | `02 - Event Store and Effect Schemas.md` | **Compliant**. |
| `scripts/run_qwen3_tts.py` | Wrapper execution script for Qwen3-TTS narration audio inference. | `05 - Provisioning and GPU Infrastructure.md` | **Compliant**. |
| `scripts/slice1_agui_wire_format_test.py` | Wire-format verification test. | `08 - Testing, Concurrency, and Rollout.md` | **Compliant**. |
| `scripts/test_worker.py` | Simple dummy local worker service. | `08 - Testing, Concurrency, and Rollout.md` | **Compliant**. |
| `scripts/tts_worker.py` | Legacy FastAPI Qwen3-TTS inference worker node. | `05 - Provisioning and GPU Infrastructure.md` | **Legacy**. |
