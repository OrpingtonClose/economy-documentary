---
{"title": "Architecture V7.1 Index", "tags": ["architecture", "v7.1", "index"]}
---

# Architecture V7.1 — Documentary Pipeline


> **Date:** 2026-05-27
> **Status:** ACTIVE — Consolidates V7 canonical architecture + ecosystem audit corrections + authoring workflow + discarded propositions
> **Replaces:** ARCHITECTURE_V7.md (V7 remains canonical base; V7.1 supersedes it for implementation)
> **Location:** `server/`
>
> This document is the canonical V7.1 architecture. It includes the full V7 content with corrections applied inline, plus new sections covering the pydantic ecosystem audit, authoring workflow, and discarded propositions. Pipeline phases are emergent, not enforced. Agents are HTTP services with `GET /` and `POST /`. The watcher has been removed; agents communicate via HTTP and SQLite event store. EventStoreDB is the future scalability path for distributed deployments. The Provisioner is an agent — the most intelligence-requiring part of the architecture — with bash_command as its only tool. There is no state machine, no `RulesEngine` Python class, and no `TransitionState` effect.

---



## Sections

- [[01 - Core Philosophy|1. Core Philosophy]]
- [[02 - System Topology|2. System Topology]]
- [[03 - Effect Type Family Complete Schemas|3. Effect Type Family — Complete Schemas]]
- [[04 - Rules as Prompt No State Machine No Rules Engine Code|4. Rules as Prompt (No State Machine, No Rules Engine Code)]]
- [[05 - Event Store|5. Event Store]]
- [[A. Appendix EventStoreDB Migration Path|A. Appendix: EventStoreDB Migration Path]]
- [[06 - Projections|6. Projections]]
- [[07 - Agent Environment and Tools|7. Agent Environment & Tools]]
- [[08 - Agent Architecture pydantic-deep|8. Agent Architecture — pydantic-deep]]
- [[09 - Agents Per-Agent Implementations|9. Agents — Per-Agent Implementations]]
- [[09.5 - Effect Parser Semantic Extraction Pipeline|9.5 Effect Parser — Semantic Extraction Pipeline]]
- [[10 - Provisioner Agent|10. Provisioner Agent]]
- [[11 - VM Worker|11. VM Worker]]
- [[12 - Data Flows|12. Data Flows]]
- [[13 - Security Model|13. Security Model]]
- [[14 - Configuration|14. Configuration]]
- [[15 - File Structure|15. File Structure]]
- [[16 - Traceability and Observability|16. Traceability and Observability]]
- [[17 - pydantic Ecosystem Deep Audit V7.1 Addendum|17. pydantic Ecosystem Deep Audit (V7.1 Addendum)]]
- [[18 - Authoring Workflow for Quasi-Deterministic Agents|18. Authoring Workflow for Quasi-Deterministic Agents]]
- [[19 - Discarded Propositions and Rationale|19. Discarded Propositions and Rationale]]
- [[20 - Glossary|20. Glossary]]
- [[21 - Unit Agent and Integration Tests|21. Unit Agent and Integration Tests]]
- [[22 - Concurrency and Timeouts Invariants|22. Concurrency and Timeouts Invariants]]

