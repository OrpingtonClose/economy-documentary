"""ARCH-G — Preview assembly package.

- :mod:`previews.builder` (ARCH-G1, issue #153): deterministic
  preview builder producing an OTIO / mp4 with honest placeholders.
- :mod:`previews.consumers` (ARCH-G3, issue #155): agent-lane
  ``evaluate_preview`` + human-lane SSE emission and dislike
  escalation.

The trigger points (ARCH-G2, issue #154) live in
:mod:`callbacks.preview_triggers` beside the other pipeline
callbacks.
"""
