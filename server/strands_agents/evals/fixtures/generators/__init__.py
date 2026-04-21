"""Deterministic fixture generators.

Each generator takes a small, typed spec and produces a media artifact
whose bytes are byte-identical across runs (same spec -> same sha256).
The invariant is tested by the fixture-determinism suite.

Video generators live in :mod:`.video`; audio generators in
:mod:`.audio`.
"""

from __future__ import annotations

from .audio import AudioSpec, generate_audio
from .video import VideoSpec, generate_video

__all__ = [
    "AudioSpec",
    "VideoSpec",
    "generate_audio",
    "generate_video",
]
