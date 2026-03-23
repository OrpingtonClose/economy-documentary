"""
War Economy Documentary — OTIO-Centric Production Pipeline v9
===============================================================
Audio-first workflow with strict phase validation gates:
  narration → prompts (stored on OTIO) → video (quality tracked) → assembly

The .otio timeline is the ABSOLUTE single source of truth at every stage.
Prompts, quality scores, and generation params all live in OTIO metadata.
"""

__version__ = "9.0.0"
