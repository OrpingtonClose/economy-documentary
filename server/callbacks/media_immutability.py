"""
Media Immutability enforcer -- a ``before_tool_callback`` that refuses any
tool call which would mutate already-emitted media.

Invariant (#128 / ARCH-F): once media is emitted, it is immutable. Only
REPLACE (regenerate from scratch) and EXTEND (append new media, keeping
prior content byte-identical) are permitted. Forbidden: trim, time-stretch,
frozen-frame fill, silent-fill gap-plugging.

This module is intentionally dependency-light so it can be imported by
tests and telemetry code without pulling in the full ADK / pipeline stack.
The ADK ``ToolContext`` type is only used as a type hint and is imported
lazily so the policy layer stays exercisable in minimal environments.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, Mapping, Optional, Pattern, Tuple

logger = logging.getLogger(__name__)


class MediaImmutabilityViolation(Exception):
    """Raised when a tool call would violate the Media Immutability Invariant.

    Carries structured context (``tool_name``, ``violation`` reason, and the
    offending ``args``) so the supervisor and dashboard can surface the
    exact call that was refused -- no silent pass-through.
    """

    def __init__(
        self,
        tool_name: str,
        violation: str,
        args: Mapping[str, Any],
    ) -> None:
        self.tool_name = tool_name
        self.violation = violation
        # Stored on ``tool_args`` rather than ``args`` to avoid colliding with
        # ``BaseException.args`` (which is the positional-arg tuple).
        self.tool_args = dict(args) if args is not None else {}
        super().__init__(
            f"Media Immutability Violation in tool {tool_name!r}: {violation}. "
            f"Only REPLACE (regenerate from scratch) and EXTEND (append new "
            f"media keeping prior content byte-identical) are permitted. "
            f"Forbidden: trim, time-stretch, frozen-frame fill, silent-fill "
            f"gap-plugging. Args: {_summarise_args(self.tool_args)}"
        )


# ---------------------------------------------------------------------------
# Forbidden patterns
# ---------------------------------------------------------------------------
#
# 1. Forbidden TOOL NAMES: every tool here exists to implement a forbidden
#    mutation and must be refused outright.
# 2. Forbidden ARG PATTERNS: ffmpeg filter fragments or operation names that,
#    if present anywhere in a string-valued arg, make the call forbidden.
# 3. Forbidden ACTION VALUES: canonical action-name strings that may appear
#    as a value in ``action=...`` / ``operation=...`` style args.

_FORBIDDEN_TOOL_NAMES: frozenset[str] = frozenset({
    "trim_narration",
    "speed_up_narration",
    "freeze_frame_fill",
    "silent_fill",
    "time_stretch_narration",
    "time_stretch_video",
})


# Each tuple is (human-readable label, compiled pattern).  Patterns match
# the ffmpeg filter syntax (``name=``) rather than the bare word so that
# tool docstrings referencing ``trim`` or ``freeze-frame`` as English don't
# trip the enforcer.
_FORBIDDEN_ARG_PATTERNS: Tuple[Tuple[str, Pattern[str]], ...] = (
    # Video speed ramp / time-stretch.
    ("setpts", re.compile(r"\bsetpts\s*=", re.IGNORECASE)),
    # Audio tempo stretch.
    ("atempo", re.compile(r"\batempo\s*=", re.IGNORECASE)),
    # Temporal pad -- hold first/last frame to extend duration (freeze-frame).
    ("tpad", re.compile(r"\btpad\s*=", re.IGNORECASE)),
    # Audio silence pad at start/end.
    ("apad", re.compile(r"\bapad\s*=", re.IGNORECASE)),
    # Generic trim filter (video/audio sub-range selection applied as mutation).
    ("trim_filter", re.compile(r"\b(?:a)?trim\s*=", re.IGNORECASE)),
    # Explicit operation-name tokens (keeps parity with FORBIDDEN_TOOL_NAMES
    # so pipelines can't smuggle a forbidden action through a string field).
    ("freeze_frame_fill", re.compile(r"freeze[_\-\s]?frame[_\-\s]?fill", re.IGNORECASE)),
    ("silent_fill", re.compile(r"silent[_\-\s]?fill", re.IGNORECASE)),
    ("time_stretch", re.compile(r"time[_\-\s]?stretch", re.IGNORECASE)),
)


_FORBIDDEN_ACTION_VALUES: frozenset[str] = frozenset({
    "trim_narration",
    "speed_up_narration",
    "freeze_frame_fill",
    "silent_fill",
    "time_stretch",
})


# Arg keys known to carry ffmpeg filter graphs.  When a call sets one of
# these the VALUE is treated as an ffmpeg filter expression and scanned
# against ``_FORBIDDEN_ARG_PATTERNS``.  (All string args get scanned anyway;
# this list is kept so future structured-filter args are enforced by name
# even if the value representation changes.)
_FILTER_ARG_KEYS: frozenset[str] = frozenset({
    "vf", "af", "filter", "filter_complex", "filter:v", "filter:a",
    "video_filter", "audio_filter", "filters",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _summarise_args(args: Any, max_len: int = 400) -> str:
    """Return a short, safe ``repr`` of tool args for violation messages."""
    try:
        rendered = repr(args)
    except Exception:  # pragma: no cover -- defensive
        rendered = f"<unrepr-able {type(args).__name__}>"
    if len(rendered) > max_len:
        return rendered[: max_len - 1] + "\u2026"
    return rendered


def _iter_string_values(obj: Any) -> Iterable[str]:
    """Recursively yield every string value nested inside ``obj``."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, Mapping):
        for v in obj.values():
            yield from _iter_string_values(v)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for v in obj:
            yield from _iter_string_values(v)


def _scan_string(tool_name: str, value: str, args: Mapping[str, Any]) -> None:
    """Raise if ``value`` contains any forbidden pattern or action name."""
    stripped = value.strip()
    if stripped in _FORBIDDEN_ACTION_VALUES:
        raise MediaImmutabilityViolation(
            tool_name=tool_name,
            violation=f"arg value {stripped!r} names a forbidden action",
            args=args,
        )
    for label, pattern in _FORBIDDEN_ARG_PATTERNS:
        if pattern.search(value):
            snippet = value if len(value) <= 200 else value[:200] + "\u2026"
            raise MediaImmutabilityViolation(
                tool_name=tool_name,
                violation=(
                    f"arg contains forbidden ffmpeg operation "
                    f"{label!r} (matched in: {snippet!r})"
                ),
                args=args,
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_media_immutability(
    tool_name: str,
    args: Optional[Mapping[str, Any]],
) -> None:
    """Raise :class:`MediaImmutabilityViolation` if the call is forbidden.

    Args:
        tool_name: The ADK tool name being invoked.
        args: The tool arguments dict (may be ``None`` or empty).

    Raises:
        MediaImmutabilityViolation: when the tool name or any arg value
            matches a forbidden pattern from the invariant.

    The function is intentionally a pure check with no side effects, so
    tests and the composed ``before_tool_callback`` can share a single
    enforcement surface.
    """
    args_map: Mapping[str, Any] = args or {}

    if tool_name in _FORBIDDEN_TOOL_NAMES:
        raise MediaImmutabilityViolation(
            tool_name=tool_name,
            violation=(
                f"tool name {tool_name!r} is a forbidden mutation "
                f"(only REPLACE / EXTEND are permitted)"
            ),
            args=args_map,
        )

    for val in _iter_string_values(args_map):
        _scan_string(tool_name, val, args_map)


def media_immutability_before_tool_callback(
    tool: Any,
    args: Dict[str, Any],
    tool_context: Any,
) -> Optional[Dict[str, Any]]:
    """ADK ``before_tool_callback`` enforcing the Media Immutability Invariant.

    Inspects the pending tool call and raises
    :class:`MediaImmutabilityViolation` if it would trim, time-stretch, or
    fill gaps with frozen frames / silence.  Returns ``None`` on success so
    the tool call proceeds normally.

    No silent pass-through: every violation becomes a structured exception
    with the tool name, violation reason, and offending args preserved on
    the exception instance.
    """
    tool_name: str = (
        tool.name if hasattr(tool, "name") and isinstance(tool.name, str)
        else str(tool)
    )
    try:
        check_media_immutability(tool_name, args)
    except MediaImmutabilityViolation as exc:
        logger.error(
            "Media Immutability Violation refused tool call: tool=%s reason=%s args=%s",
            exc.tool_name, exc.violation, _summarise_args(exc.tool_args),
        )
        raise
    return None


__all__ = [
    "MediaImmutabilityViolation",
    "check_media_immutability",
    "media_immutability_before_tool_callback",
]
