"""Media fixtures for strands_evals Experiments.

This package owns every piece of real media used by the evaluation
harness:

- :mod:`.generators` holds the deterministic ffmpeg / espeak / sox
  producers. Each generator takes a small, typed spec and emits a
  byte-identical artifact every time (same input -> same sha256).
- The ``video/`` and ``audio/`` directories hold committed artifacts
  keyed by fixture id. Artifacts are small (usually <50 KB) so they
  live in git directly; larger artifacts are mirrored to B2 and
  referenced by URL via the manifest.
- ``manifest.json`` is the single source of truth for every fixture:
  id, axis label, expected binary verdict, sha256 of the bytes,
  optional public B2 URL, and the generator spec that produced it.

The purpose of the split is twofold:

1. Judge tests are built as strands_evals ``Experiment`` cases. Each
   case carries a fixture id; the ``task`` function resolves it
   through the manifest and hands the bytes / url to the
   :class:`LiveVideoJudgeEvaluator` or :class:`LiveAudioJudgeEvaluator`.
2. Fixtures are reproducible from code. If a fixture drifts (different
   ffmpeg version, different espeak voice), the determinism test
   fails loudly and points at the generator — never the judge.
"""

from __future__ import annotations

from .manifest import (
    FixtureEntry,
    FixtureManifest,
    load_manifest,
    resolve_fixture_path,
)

__all__ = [
    "FixtureEntry",
    "FixtureManifest",
    "load_manifest",
    "resolve_fixture_path",
]
