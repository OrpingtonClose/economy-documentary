---
{
  "title": "Event Store and Effect Schemas",
  "section": "2",
  "tags": [
    "architecture",
    "v7.1",
    "event-store",
    "schemas"
  ]
}
---

[[00 - Index|◀ Back to Index]]

# 🗄️ Event Store & Effect Schemas

This module defines the complete family of **Pydantic Effect Schemas** and the implementation details of the **SQLite WAL Event Store** used as the pipeline's sole source of truth.

---

## 1. Pydantic Effect Schemas

#### Strict Pydantic schemas derived from base Effect
All mutations in the system must be represented by typed Pydantic models derived from a common `Effect` base class. Every event appended to the event store inherits from `Effect`, containing metadata fields including `effect_id` (a UUIDv7 generated client-side for idempotent retries), `kind` (the literal discriminant string), `agent` (the component that produced the effect), and `timestamp` (the epoch time at creation). Never instantiate the base `Effect` class directly.

---

### 1.2 Script Effects

These effects dictate the logical structure and textual narration of the documentary.

---

### 1.3 Job Effects

These effects manage the job queue for media compilation (TTS and Video rendering).

---

### 1.4 Reconciliation Effects

These effects handle synchronization and duration validation of TTS tracks.

---

### 1.5 VM Effects

These effects monitor VM health and state.

---

### 1.6 OTIO / Timeline Effects

These effects manipulate clip placement in the timeline stack.

---

### 1.7 Pipeline & Budget Effects

These effects manage run lifecycles and cost-limit constraints.

---

### 1.8 Human, Loop & Production Failures

These effects handle exceptions, manual redirection, and fallback loops.

---

### 1.9 Discriminator Union

---

## 2. Event Store

#### Single-process WAL direct-writes for durability
⚡ All event store writes must be direct synchronous writes in SQLite WAL mode executed within the single coordinator process

The event store must use a single SQLite database file configured in Write-Ahead Logging (WAL) mode. To ensure simplicity and durability, all writes are direct and synchronous, executed within the single coordinator process, eliminating background writer loops or queue-based execution layers.

#### Idempotent event writes via unique effect identifiers
Every event must possess a client-side generated UUIDv7 (`effect_id`). The SQLite table must enforce a `UNIQUE(effect_id)` constraint on this field to guarantee idempotency and silently reject or handle duplicate inserts on retries.

### 2.1 Schema

The database contains a single primary table named `events`:

### 2.2 EventStore Class

---

## 3. Alternative Coordinate-Based Timeline Schema (Timespan Keys & Stable Anchors)

For deployments requiring explicit range-conformance and strict duration checking directly in the database layer, the timeline can be modeled using track-specific coordinate ranges as natural keys, bound to stable screenplay blueprint identifiers.

#### Separation of logical screenplay blueprint from physical media tracks
The coordinate-based timeline must separate the logical screenplay blueprint (representing the stable narration script blocks using surrogate keys such as `block_id` or `scenario_id`) from the physical media tracks (which represent media placement using track-level coordinate ranges `[start_ms, end_ms]` as natural keys and check constraints to prevent overlapping clips).

#### Nanosecond precision timeline arithmetic via sqlean-time
To prevent floating-point precision issues and rounding errors during duration calculation, timeline queries and constraints must use the SQLite `sqlean-time` extension to perform high-precision nanosecond date-time duration arithmetic.

---

## A. Appendix: EventStoreDB Migration Path

For large scale distributed deployments, the SQLite backend can be seamlessly swapped with **EventStoreDB** using client-side streams.

### A.1 Distributed ESDB Client Implementation

