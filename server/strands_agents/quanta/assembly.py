"""Component 11 atom — assembly pre-flight input check.

One pure atom: :func:`check_assembly_inputs`. Validates that every
scene has a rendered clip, every clip has an ``mp4_path``, the
WhisperX alignment is present, and the OTIO timeline path is set.
Raises :class:`RuntimeError` on any contract violation.

The rest of the assembly tool (OTIO compose, ffmpeg render, B2 upload)
is IO-heavy and is therefore a connector, not an atom.
"""

from __future__ import annotations

from typing import Any

from strands_agents.tools.assembly_tool import _check_inputs


def check_assembly_inputs(
    scenes: list[dict[str, Any]],
    clip_artifacts: list[dict[str, Any]],
    whisperx_alignment: dict[str, Any],
    timeline_path: str,
) -> None:
    """Fail-closed pre-flight check before final assembly.

    Raises:
        RuntimeError: On any of:
            * empty ``scenes``
            * missing or empty ``whisperx_alignment``
            * empty ``timeline_path``
            * any scene missing ``id`` or ``target_duration_sec``
            * any scene with no matching ``clip_artifacts`` entry
            * any clip artifact missing ``mp4_path``
    """
    _check_inputs(scenes, clip_artifacts, whisperx_alignment, timeline_path)


__all__ = ["check_assembly_inputs"]
