---
{
  "title": "Discarded Propositions and Rationale",
  "section": "19",
  "tags": [
    "architecture",
    "v7.1"
  ]
}
---

<- [[18 - Authoring Workflow for Quasi-Deterministic Agents|Authoring Workflow for Quasi-Deterministic Agents]] | [[00 - Index|Index]] | [[20 - Glossary|Glossary]] ->

# Discarded Propositions and Rationale


This section records design alternatives that were considered and rejected during V7.1 development, with rationale.

### 19.1 Discarded: Custom HTTP Services as "Deterministic Agents"

**Proposition:** Build lightweight HTTP services for each pipeline step that execute authored Python scripts directly, with LLM used only for natural language generation (not decision-making).

**Why discarded:**
- Too rigid: Every edge case requires code changes
- Loses agentic adaptability: Cannot handle novel situations
- Maintenance burden: N services × N scripts = combinatorial explosion
- Violates "prompt-based rules" principle: Rules would be in code, not prompts

**What we kept instead:** Real pydantic-deep agents with `bash_command` as their only tool. Scripts are read by the handler and injected into prompts. Task sequencing is enforced by prompt structure, not todo tools [[18 - Authoring Workflow for Quasi-Deterministic Agents|§18]].

### 19.2 Discarded: SQLite Event Store

**Proposition:** Continue using the V6 SQLite event store with `BEGIN IMMEDIATE` locking and `_writer_loop`.

**Why discarded:**
- Operational complexity: Custom WAL, checkpointing, vacuuming
- Single-point bottleneck: All agents contend for one database connection
- Not swappable: Tied to SQLite schema; migration to distributed store would require full rewrite
- SQLite is simpler: Append-only file, no schema migrations, trivial backup

**What we kept instead:** SQLite file store with `EventStore` interface designed for ESDB swap ([[05 - Event Store|§5]], Appendix A).

### 19.3 Discarded: Regex + Marker-Based Parser

**Proposition:** Use regex patterns (e.g., `EFFECT:QueueJob`) or XML/JSON markers in LLM output for fast-path parsing.

**Why discarded:**
- Fragile: Regex breaks on creative formatting
- Marker pollution: Agents produce worse natural language when trained to produce markers in their output
- Dual paths: Fast path vs slow path creates testing burden and divergence risk
- Parser invariant: Single-phase semantic extraction via instructor ONLY [[09.5 - Effect Parser Semantic Extraction Pipeline|§9.5]]

**What we kept instead:** Pure instructor semantic extraction — agent produces natural language, parser extracts structured effects.

### 19.4 Discarded: Custom Causal Logging System

**Proposition:** Build a custom causal logging framework tracking execution DAGs, citations, and agent attribution.

**Why discarded:**
- `pydantic-ai-provenance` already exists and provides this
- Building it ourselves duplicates tested code
- Maintaining compatibility with pydantic-ai ecosystem is easier than rolling our own

**What we kept instead:** Use `ProvenanceCapability` from `pydantic-ai-provenance` (§17.2.4).

### 19.5 Discarded: EventStoreDB on macOS

**Proposition:** Run EventStoreDB via Docker on macOS for local development.

**Why discarded:**
- No macOS binary exists (checked v24.10.14, v26.0.3, v26.1.0)
- Docker on macOS (Colima) adds complexity: VM layer, port forwarding, volume mounts
- Development friction: Every restart requires Docker container management
- SQLite satisfies all requirements for single-machine development

**What we kept instead:** SQLite for development, ESDB protocol interface for future Linux deployment (Appendix A).

### 19.6 Discarded: Timeout-Based Guardrails

**Proposition:** Add timeouts to agent turns, HTTP requests, and async operations to prevent hangs.

**Why discarded (Architecture Guard):**
- Timeouts cause silent failures and data loss
- If a process hangs, the operator intervenes manually
- No timeout parameters of any kind in the codebase
- This is a non-negotiable guard rule enforced by DeepSeek v4-flash

**What we kept instead:** No timeouts anywhere. Global `LoopBoundLock` serialization prevents concurrent handler hangs. Operator monitors via `GET /` and intervenes.

### 19.7 Discarded: Mock-Based Testing

**Proposition:** Use mocks for LLM, parser, and event store in unit tests.

**Why discarded (Architecture Guard):**
- Mocks create fantasy behavior that diverges from production
- Tests are mini production runs: real LLM, real parser, real event store, real bash
- "A test that passes with mocks but fails in production is worse than no test"

**What we kept instead:** Integration tests that run real agents with real models (using cheap/fast models like deepseek-v4-flash for cost control).

### 19.8 Discarded: Environment Variable Configuration

**Proposition:** Use `.env` files and `os.environ` for API keys, model names, and endpoint URLs.

**Why discarded (Architecture Guard):**
- Environment variables are invisible state that changes between runs
- Configuration must be explicit: passed as parameters, stored in Config objects
- API keys belong in secret management, not environment

**What we kept instead:** `Config` Pydantic model passed explicitly to `create_pipeline_agent()` and all handlers.

---

