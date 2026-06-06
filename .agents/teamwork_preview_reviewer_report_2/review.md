## Review Summary

**Verdict**: REQUEST_CHANGES

The generated codebase compliance report (`codebase_compliance_report.md`) is a very thorough, well-written, and structured document. It correctly identifies the core architectural violations (such as the event store bypass for agent memories, GET/POST blocking deadlocks, static check violations, and macOS loopback binding issues). It also includes a detailed remediation roadmap.

However, the report has a few mathematical inconsistencies in its summary metrics, fails to cover active shell scripts in the scripts mapping, and omits file/line/snippet details for key VM provisioning and port discrepancies. Therefore, changes are required to ensure compliance with the user acceptance criteria.

---

## Findings

### [Major] Finding 1: Factual Inaccuracies in Executive Summary Metrics
- **What**: The summary metrics for file counts in Section 1 (Executive Summary) are mathematically incorrect and do not match the data tables in Section 2.
- **Where**: `codebase_compliance_report.md` Section 1, lines 12-14:
  - Scanned files is stated as 70, but the tables actually list 80 files.
  - Fully compliant files is stated as 36, but counting the tables yields 52 compliant files.
  - Non-compliant files is stated as 34, but counting the tables yields 28 non-compliant files.
- **Why**: This is a direct factual inaccuracy. The summary metrics are inconsistent with the detailed tables, violating the integrity of the audit report.
- **Suggestion**: Recalculate the numbers from the source code mapping tables (80 files scanned: 52 compliant, 28 non-compliant) and update Section 1 to match.

### [Major] Finding 2: Coverage Gap for Shell Scripts under `scripts/`
- **What**: The report completely omits active shell scripts from the mapping.
- **Where**: `codebase_compliance_report.md` Section 2.3.
- **Why**: Requirement R1 explicitly asks to: "Verify every python file and shell script under `server/`, `pipeline/`, `scripts/`, and `tests/`". There are 8 active shell scripts in the `scripts/` directory that were not mapped or analyzed.
- **Suggestion**: Update the scripts directory table in Section 2.3 to include the 8 active `.sh` bootstrap and on-start scripts:
  - `scripts/central_bootstrap.sh`
  - `scripts/gpu_bootstrap.sh`
  - `scripts/langfuse_bootstrap.sh`
  - `scripts/ltx_video_worker_bootstrap.sh`
  - `scripts/playground_staging_bootstrap.sh`
  - `scripts/qwen3_tts_worker_bootstrap.sh`
  - `scripts/vm_onstart_ltx.sh`
  - `scripts/vm_onstart_tts.sh`
  Map them to their corresponding specifications (primarily `05 - Provisioning and GPU Infrastructure.md`) and verify their compliance status.

### [Major] Finding 3: Incomplete Discrepancy Detailing for GPU Provisioning & Ports
- **What**: A top discrepancy mentioned in the summary is not analyzed with the requested level of detail in a dedicated section.
- **Where**: `codebase_compliance_report.md` Section 1, line 25 (Key discrepancy 5), and Section 2.3.
- **Why**: Acceptance criteria states: "Each reported discrepancy contains the file path, line number, code snippet, and the specific obsidian-vault section it violates." While GPU Provisioning and Ports mismatch is mentioned as a key discrepancy, there is no dedicated section (unlike Sections 3, 4, and 5) that provides line numbers and code snippets showing the violations.
- **Suggestion**: Add a new section detailing the specific file, line number, code snippet, and vault spec section for:
  - `scripts/vm_agent.py` line 277: default port set to `8880` (violates port 9000+ requirement in `05 - Provisioning and GPU Infrastructure.md` Section 3).
  - `scripts/provision_central.py` line 153: image set to `ubuntu:22.04` (violates `05 - Provisioning and GPU Infrastructure.md` Section 3 / 3.1 templates).

### [Minor] Finding 4: Incomplete Code Snippets for macOS Loopback Binding Violations
- **What**: Code snippets are missing for the 10 test files violating macOS localhost bindings.
- **Where**: `codebase_compliance_report.md` Section 7.1.
- **Why**: Section 7.1 lists file paths and line numbers but does not show code snippets, which is a minor discrepancy against the reporting criteria.
- **Suggestion**: Add a representative code snippet or detailed example (e.g., from `tests/units/test_longform_readiness_bdd.py` line 64) to illustrate the pattern of hardcoded `localhost` bindings.

---

## Verified Claims

- **Lock serialization blocks `GET /` and `POST /` on the active turn lock** → Verified via inspection of `server/agent_base.py` lines 941-942 and 976-977 → **PASS**
- **Unit test `test_post_handler_rejects_with_409_when_busy` fails due to lock blocking** → Verified via running `.venv/bin/pytest tests/units/test_concurrency_and_intervention.py` → **PASS**
- **BDD test in `test_concurrency_intervention_bdd.py` asserts incorrect blocking behavior and passes** → Verified via inspection of `test_concurrency_intervention_bdd.py` and running `.venv/bin/pytest tests/units/test_concurrency_intervention_bdd.py` → **PASS**
- **Swallowed exceptions and timeout policy violations exist in the codebase** → Verified via running `.venv/bin/python server/cheat_check.py server pipeline scripts tests` which reported 213 total violations → **PASS**
- **Event store has been bypassed to write agent memories directly to the sqlite `agent_memories` table** → Verified via inspection of `server/event_store.py` lines 46-51 and lines 184-211 → **PASS**
- **VM worker agent default port is 8880** → Verified via inspection of `scripts/vm_agent.py` line 277 → **PASS**
- **`scripts/provision_central.py` boots a base `ubuntu:22.04` image instead of pre-built worker images** → Verified via inspection of `scripts/provision_central.py` line 153 → **PASS**

---

## Coverage Gaps

- **Missing shell scripts mapping** — *risk level: Medium* — *recommendation: Investigate*
  - The 8 active shell scripts in the `scripts/` directory must be mapped and reviewed to ensure they comply with the V7.1 specifications.
- **Incomplete detail on GPU VM provisioning image/ports** — *risk level: Low* — *recommendation: Investigate*
  - Document the exact files, line numbers, and snippets for port `8880` and `ubuntu:22.04` image usage in a dedicated discrepancy section.

---

## Unverified Items

- **Inactive legacy workers (`scripts/gpu_worker.py` and `scripts/tts_worker.py`) natural language violation details** — *Reason not verified*: Checked report snippets, which seem correct and align with the filenames, but did not read the entire legacy worker scripts since their inactive/legacy status was already established.
