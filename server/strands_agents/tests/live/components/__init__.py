"""Per-component live-judge proof-of-robustness tests.

Every module under this package tests exactly one of the 15 pipeline
components against real cloud-API judges (Google Gemini, Alibaba Qwen,
Anthropic Claude).  The tests are never skipped for flakiness: if the
required credentials are set, the test runs and must pass every time.

Two kinds of tests live here:

1. **Live-judge semantic tests** — the component has LLM-backed
   behaviour.  We call the real LLM, produce a real artifact, and
   have a *different* LLM family judge whether the artifact meets a
   clear-cut rubric (on-topic, correct language, matches narration).

2. **Deterministic proof tests** — the component is mechanistic
   (OTIO compose, structural evaluator, interrupt/resume).  We feed
   known-good and known-bad inputs and assert the component's
   verdict.  No LLM judge needed; the check is deterministic.

Mixing the two under one live directory is deliberate: the goal is
per-component proof of robustness, not a single kind of proof.
"""
