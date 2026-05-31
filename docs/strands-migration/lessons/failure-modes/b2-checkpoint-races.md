> [!WARNING]
> **NON-AUTHORITATIVE / SECONDARY DOCUMENTATION**
> Only the files inside the `obsidian-vault/` directory are the authoritative, up-to-date documentation for this project. This file is secondary and may be outdated.

# B2 checkpoint races

Knowledge page. The B2 checkpoint helper
(`server/strands_agents/b2_checkpoint/`) is the pipeline's resumability
lever — AGENTS.md invariant 6, *every artifact to B2 immediately*, is
what it exists to honour. The store is intentionally narrow: three
entry points (`checkpoint_artifact`, `load_manifest`, `resume`) backed
by a per-run append-only manifest. This page documents the three
invariants that keep it honest and the failure modes they defend
against.

## Invariant 1 — Idempotent upload

Every upload is content-addressed by the tuple
`(run_id, kind, revision_tag, sha256)`. A second upload that produces
the same key returns the existing manifest entry, unchanged. This is
what lets a crashed worker retry the same scene without corrupting
the ledger with a duplicate row.

**Failure it defends against**: a scene worker completes a render,
pushes to B2, then crashes before acknowledging success. On recovery
the worker re-runs from its last scheduled task, pushes the same
bytes again. Without idempotency the manifest grows a second entry
with a different `artifact_id`, the resume loop sees two "latest"
scene_json entries for scene 3, and downstream stages pick the wrong
one or, worse, both. The store returns the first entry's
`artifact_id` and the manifest has exactly one row.

**Test**: `duplicate_idempotency` Case under `infra_b2_checkpoint`.

## Invariant 2 — Monotonic revision tags

An upload whose `revision_tag` is strictly older than the run's
current latest raises `StaleRevisionError`. AGENTS.md invariant 8 —
*revision tags are sacred* — is enforced here. Revision tags advance
each time the preference ledger changes; a worker that tries to write
against a stale tag is almost certainly a worker that missed a ledger
update while it was mid-render.

**Failure it defends against**: a long-running TTS job starts under
revision `r0003`, the orchestrator refines to `r0004` mid-render, the
job finishes and tries to checkpoint its WAV against `r0003`. Without
this guard the stale WAV lands in the manifest and the timing loop
reads it back as if it were current. The fail-fast exception forces
the orchestrator to drop the stale bytes and rerun under `r0004`.

**Test**: `stale_revision_rejected` Case.

## Invariant 3 — Fail-closed on checksum mismatch

`resume(run_id, store)` downloads every artifact and re-verifies its
sha256 before returning. A single mismatch aborts the whole resume
with `ChecksumMismatchError` — no partial `ResumeState` is returned
to the caller. A bit-flipped artifact cannot be safely promoted back
into the pipeline; better to force the orchestrator to rebuild the
affected kind from scratch than to timeline a subtly-wrong payload.

**Failure it defends against**: network-layer corruption between the
worker's successful upload and a later resume read (the classic
"multi-part upload finalised on one replica but not the other" B2
race). Without fail-closed, `resume` would hand back the corrupted
`ManifestEntry` alongside the good ones and the assembly stage would
silently render garbage into the master MP4.

**Test**: `resume_checksum_mismatch_fails_closed` Case.

## Not-yet-covered incidents

Real failures observed in production belong below this line. Empty
while the helper is in-memory only; first entries land when
`LiveB2CheckpointStore` is wired into the orchestrator (a later
slice).

<!-- Append real incidents here with a dated header -->
