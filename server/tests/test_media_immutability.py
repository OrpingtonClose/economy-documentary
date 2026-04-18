"""Tests for the Media Immutability Invariant enforcer (ARCH-F2 / #152).

Invariant (#128 / ARCH-F): once media is emitted, it is immutable. Only
REPLACE (regenerate from scratch) and EXTEND (append new media, keeping
prior content byte-identical) are permitted. Forbidden: trim, time-stretch,
frozen-frame fill, silent-fill gap-plugging.

The enforcer lives in ``server/callbacks/media_immutability.py`` and is
wired into the shared ``before_tool_callback`` in
``server/callbacks/before_tool.py`` so every ADK agent that registers
``before_tool_callback`` gets the enforcement for free.

These tests exercise three surfaces:

1. ``check_media_immutability`` -- the pure policy function.
2. ``media_immutability_before_tool_callback`` -- the ADK callback wrapper.
3. The composed ``before_tool_callback`` in ``callbacks.before_tool`` --
   ensures forbidden calls are refused BEFORE rate-limit semaphores are
   acquired (fail-fast / no silent degradation).
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

# Ensure ``server/`` is on sys.path when pytest is invoked from the repo root.
_SERVER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from callbacks.media_immutability import (  # noqa: E402
    MediaImmutabilityViolation,
    check_media_immutability,
    media_immutability_before_tool_callback,
)


def _tool(name: str) -> SimpleNamespace:
    """Build a stand-in tool object with a ``name`` attribute for the callback."""
    return SimpleNamespace(name=name)


def _ctx() -> SimpleNamespace:
    """Minimal stand-in for ADK ``ToolContext`` -- callback only reads .name."""
    return SimpleNamespace(state={}, function_call_id="call-1", agent_name="test")


# ---------------------------------------------------------------------------
# 1. Forbidden tool names
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tool_name",
    [
        "trim_narration",
        "speed_up_narration",
        "freeze_frame_fill",
        "silent_fill",
        "time_stretch_narration",
        "time_stretch_video",
    ],
)
def test_forbidden_tool_names_are_refused(tool_name: str) -> None:
    """Every tool whose name IS a forbidden mutation is refused outright."""
    with pytest.raises(MediaImmutabilityViolation) as excinfo:
        check_media_immutability(tool_name, {"scene_id": "s001"})
    assert excinfo.value.tool_name == tool_name
    assert "forbidden mutation" in excinfo.value.violation
    # The structured exception carries the offending args.
    assert excinfo.value.tool_args == {"scene_id": "s001"}


# ---------------------------------------------------------------------------
# 2. Forbidden ffmpeg filters in arg values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("arg_value", "expected_label"),
    [
        ("setpts=0.9*PTS", "setpts"),
        ("[0:a]atempo=1.1[out]", "atempo"),
        ("tpad=stop_mode=clone:stop_duration=2", "tpad"),
        ("apad=pad_dur=1.5", "apad"),
        ("trim=start=0:end=3", "trim_filter"),
        ("atrim=start=0:end=1.5", "trim_filter"),
        ("freeze_frame_fill", "freeze_frame_fill"),
        ("freeze-frame-fill", "freeze_frame_fill"),
        ("silent_fill", "silent_fill"),
        ("time_stretch", "time_stretch"),
    ],
)
def test_forbidden_filter_in_arg_refused(
    arg_value: str, expected_label: str,
) -> None:
    """ffmpeg tool args carrying a forbidden filter are refused."""
    with pytest.raises(MediaImmutabilityViolation) as excinfo:
        check_media_immutability(
            "run_ffmpeg",
            {"filter_complex": arg_value},
        )
    assert expected_label in excinfo.value.violation


def test_forbidden_filter_nested_deeply() -> None:
    """Forbidden substrings are detected inside nested dicts/lists."""
    args = {
        "command": [
            "ffmpeg",
            "-i", "in.mp4",
            "-af", "atempo=1.05",
            "-c:v", "copy",
            "out.mp4",
        ],
        "meta": {"reason": "close-enough stretch"},
    }
    with pytest.raises(MediaImmutabilityViolation) as excinfo:
        check_media_immutability("ffmpeg_exec", args)
    assert "atempo" in excinfo.value.violation


def test_forbidden_action_value_in_args() -> None:
    """A bare forbidden action name as an arg value is refused."""
    with pytest.raises(MediaImmutabilityViolation) as excinfo:
        check_media_immutability(
            "apply_escalation",
            {"action": "freeze_frame_fill", "scene_id": "s002"},
        )
    assert "freeze_frame_fill" in excinfo.value.violation


# ---------------------------------------------------------------------------
# 3. Permitted REPLACE / EXTEND calls pass through
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        # REPLACE: regenerate a clip from scratch.
        ("regenerate_clip", {"clip_id": "s003_p002", "prompt_delta": "warmer lighting", "seed_delta": 7}),
        # EXTEND: append a new clip to the existing media.
        ("generate_extension_clip", {"scene_id": "s003", "duration_needed": 1.4}),
        # Standard TTS / video generation tools.
        ("generate_narration", {"scene_num": 3, "voice": "V1", "text": "A short line of narration."}),
        ("generate_video_clip", {"prompt": "A sourdough loaf in warm kitchen light", "duration_sec": 5.0}),
        # ffmpeg concat of existing media (EXTEND semantics, no filter graph).
        ("concat_clips", {"clips": ["a.mp4", "b.mp4"], "output": "joined.mp4"}),
        # ffmpeg loudness normalisation -- permitted audio processing.
        ("normalize_audio", {"filter_complex": "loudnorm=I=-14:TP=-1.5:LRA=11"}),
        # Benign text mentioning the word "trim" in prose (no filter syntax).
        ("rewrite_narration", {"direction": "trim", "text": "We could trim a sentence here."}),
    ],
)
def test_permitted_calls_pass_through(tool_name: str, args: dict) -> None:
    """Permitted REPLACE / EXTEND calls return without raising."""
    # check_media_immutability is a pure policy check; no return value.
    check_media_immutability(tool_name, args)


def test_empty_args_allowed() -> None:
    """Calls with no args at all are permitted (the tool name alone must pass)."""
    check_media_immutability("noop_tool", {})
    check_media_immutability("noop_tool", None)


# ---------------------------------------------------------------------------
# 4. before_tool_callback surface
# ---------------------------------------------------------------------------

def test_before_tool_callback_allows_permitted() -> None:
    """The ADK callback returns ``None`` for permitted calls."""
    result = media_immutability_before_tool_callback(
        _tool("regenerate_clip"),
        {"clip_id": "s003_p002", "prompt_delta": "...", "seed_delta": 3},
        _ctx(),
    )
    assert result is None


def test_before_tool_callback_raises_on_violation() -> None:
    """The ADK callback raises ``MediaImmutabilityViolation`` for forbidden calls."""
    with pytest.raises(MediaImmutabilityViolation) as excinfo:
        media_immutability_before_tool_callback(
            _tool("run_ffmpeg"),
            {"filter_complex": "atempo=1.1"},
            _ctx(),
        )
    # Exception must expose structured context for the dashboard / supervisor.
    assert excinfo.value.tool_name == "run_ffmpeg"
    assert "atempo" in excinfo.value.violation
    assert excinfo.value.tool_args == {"filter_complex": "atempo=1.1"}


def test_violation_message_carries_tool_name_and_reason() -> None:
    """The exception str includes the tool name and a forbidden-op label."""
    with pytest.raises(MediaImmutabilityViolation) as excinfo:
        check_media_immutability("freeze_frame_fill", {"scene_id": "s001"})
    msg = str(excinfo.value)
    assert "freeze_frame_fill" in msg
    assert "forbidden mutation" in msg
    assert "REPLACE" in msg and "EXTEND" in msg


# ---------------------------------------------------------------------------
# 5. Composed before_tool_callback (enforcer runs FIRST in before_tool.py)
# ---------------------------------------------------------------------------

def test_shared_before_tool_callback_refuses_forbidden_before_rate_limit() -> None:
    """The shared ``before_tool_callback`` must enforce immutability FIRST.

    Forbidden calls must never acquire a rate-limit semaphore -- the
    enforcer runs before the provider semaphore logic in
    ``callbacks.before_tool.before_tool_callback``.
    """
    # Importing here so the test file remains runnable even if ADK/dashboard
    # deps aren't installed (in that case the shared callback isn't imported
    # and this test is skipped).
    try:
        from callbacks import before_tool as _shared
    except Exception as exc:  # pragma: no cover -- import-time skip
        pytest.skip(f"callbacks.before_tool not importable: {exc}")

    ctx = _ctx()
    with pytest.raises(MediaImmutabilityViolation):
        _shared.before_tool_callback(
            _tool("trim_narration"),
            {"scene_id": "s001", "max_cut_sec": 0.5},
            ctx,
        )
    # No semaphore key stashed into state -- fail-fast before rate-limiting.
    assert not any(
        k.startswith("_provider_sem_") for k in ctx.state
    ), "Enforcer must run BEFORE rate-limit semaphore acquisition"
