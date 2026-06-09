# plan.md — Codebase Compliance Audit Plan

## Mission
Perform a comprehensive paragraph-to-paragraph compliance audit of all python and shell script files in `server/`, `pipeline/`, `scripts/`, and `tests/` against the specs in `obsidian-vault/`, and write a report `codebase_compliance_report.md` in the workspace root.

## Milestones
| Milestone | Description | Assigned Subagents | Target Output | Status |
|-----------|-------------|--------------------|---------------|--------|
| M1 | Setup & Planning | Orchestrator | plan.md, BRIEFING.md | DONE |
| M2 | Exploration of `server/` | Explorer 1 | server_audit_report.md | DONE (Conv: 449991f9-e666-408b-8c9c-d1de837ce10d) |
| M3 | Exploration of `pipeline/` and `scripts/` | Explorer 2 | pipeline_scripts_audit_report.md | DONE (Conv: 83436f8e-d5af-4cff-a82c-8798c2190038) |
| M4 | Exploration of `tests/` | Explorer 3 | tests_audit_report.md | DONE (Conv: 3fe12fa2-53bd-4cd3-84d9-be9b2f7f056c) |
| M5 | Synthesis & Audit Report Compilation | Worker | codebase_compliance_report.md | DONE (Conv: 743b6bcb-c394-4757-a378-038f9c7097e4) |
| M6 | Review & Verification | Reviewer 3 & 4 | Review verdicts | IN_PROGRESS |

## Key Requirements to Check
- **R1: Comprehensive Source Code Mapping**: Map files under `server/`, `pipeline/`, `scripts/`, and `tests/` to specifications.
- **R2: REST Endpoint Control Protocols**:
  - `GET /` and `POST /` must serialize execution using the loop-bound locks, performing no heavy inline processing.
  - `PUT /` must immediately cancel running execution tasks and launch the new payload in the background, returning `204 No Content`.
  - No disallowed sub-endpoints (e.g., `/health`, `/status`).
- **R3: Natural Language Invariant Check**: VM worker agent and GSA communicate using only plain conversational natural language. No structured key-value status payloads (like `ltx=yes`, `tts=yes`, `tts_loaded: true`) generated, processed, or expected.
- **R4: Event Store & Schema Alignment**: `server/effects.py` and `server/event_store.py` must match `02 - Event Store and Effect Schemas.md`. No undocumented subclasses/structural mismatch.
- **R5: Complete NoOp Elimination Check**: Verify `noop` events are blocked at `EventStore` append boundary.
- **R6: Audit Report Generation**: Generate `codebase_compliance_report.md` in workspace root.
