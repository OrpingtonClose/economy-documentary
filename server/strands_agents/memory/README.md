# `memory/` — MemoryMiddleware mount point

At runtime the orchestrator (component 14) loads
[`docs/strands-migration/AGENTS.md`](../../../docs/strands-migration/AGENTS.md)
via `MemoryMiddleware` on every turn. This directory exists as a mount
point for **local overlays** — run-specific invariants that should not
live in the repo-wide AGENTS.md (e.g. per-topic pronunciation rules,
per-tenant approval-gate policy).

The `create_deep_agent` call will pass both paths:

```python
memory=[
    "docs/strands-migration/AGENTS.md",          # canonical, versioned
    "server/strands_agents/memory/AGENTS.md",    # optional local overlay
]
```

Local overlays are not checked in. Phase 0 leaves this directory empty.
