"""CLI entrypoint: ``python -m strands_agents.evals.fixtures.generators``.

Regenerates every declared fixture and rewrites ``manifest.json``.
Safe to re-run — deterministic generators produce byte-identical
output for the same spec.

Usage::

    python -m strands_agents.evals.fixtures.generators

Exits non-zero if ffmpeg or espeak-ng is missing or if any generator
fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .registry import build_all


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    manifest = build_all(root)
    for entry in manifest.entries:
        print(f"{entry.id:32s} {entry.sha256[:12]}  {entry.relative_path}")
    print(f"\nwrote {len(manifest.entries)} fixtures + manifest to {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
