"""Component 04 — ``render_audio`` deterministic Strands tool.

The ADK implementation (``server/callbacks/deterministic_steps.py ::
deterministic_audio_callback``) skips the LLM entirely and runs TTS +
WhisperX + B2 upload inline — the ``audio_agent`` LLM exists only to
satisfy the ADK "an agent must be present" requirement.

The Strands port collapses that to a single deterministic ``@tool``
function: the DeepAgent orchestrator (component 14) will call it via
``tool()`` instead of spinning up an LLM agent.

**Design invariants** (carried over from the ADK source):

1. **Fail loud.** TTS, WhisperX, or B2 upload failure raises
   :class:`RuntimeError` — never silently falls back to synthetic
   durations or placeholder audio. All downstream timing decisions
   depend on real spoken durations.
2. **Strict input validation.** Empty scenes, missing voice entries,
   or empty narration text raise :class:`ValueError` *before* any
   helper is invoked. Helper failures are the only path to
   :class:`RuntimeError`.
3. **Injected helpers.** TTS, WhisperX, loudness-normalization, and B2
   upload are plumbed via :func:`set_audio_helpers`; production wiring
   lands with component 14. Unit tests inject deterministic fakes.
4. **Per-voice block layout.** Returns one :class:`NarrationBlock`-like
   dict per ``(scene, voice)`` pair — the exact shape
   ``AudioInvariantEvaluator`` expects.
5. **Contract enforcement is orchestrator's job.** Component 14 wraps
   this tool in a :class:`ContractEnforcer` hook bound to
   :data:`AUDIO_CONTRACT`; the tool itself deliberately does *not*
   hit the HTTP TTS health-check or glob ``audio/*.wav`` so it is
   trivially callable from unit tests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from strands import tool
from strands.tools.decorator import ToolContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper registry — injected by production wiring (component 14) or tests.
# ---------------------------------------------------------------------------


class TtsGenerate(Protocol):
    """Synthesize one narration clip.

    Args:
        scene_num: 1-based scene number.
        voice_role: Voice role identifier (``"V1"``, ``"V2"``, ``"V3"``).
        text: Narration text to synthesize.
        language: Language code (``"en"``, ``"ru"``).

    Returns:
        Absolute path to the generated WAV file.

    Raises:
        RuntimeError: On any TTS failure. Must never return a placeholder
            or synthetic clip.
    """

    def __call__(
        self, scene_num: int, voice_role: str, text: str, language: str
    ) -> str: ...


class WhisperxAlign(Protocol):
    """Run WhisperX alignment on a WAV.

    Args:
        wav_path: Path to the narration WAV.
        text: Original text that was synthesized.
        language: Language code.

    Returns:
        Dict with ``total_duration`` (float seconds), ``word_count`` (int),
        and ``words`` (list of ``{word, start, end}`` dicts).

    Raises:
        RuntimeError: On alignment failure. Must never return a synthetic
            per-word timing fallback.
    """

    def __call__(
        self, wav_path: str, text: str, language: str
    ) -> dict[str, Any]: ...


class LoudnessNormalize(Protocol):
    """Normalize a WAV to a fixed LUFS target in place.

    Args:
        wav_path: Path to the narration WAV (mutated in place).
        target_lufs: Integrated loudness target (typically ``-23.0``).

    Raises:
        RuntimeError: On normalization failure.
    """

    def __call__(self, wav_path: str, target_lufs: float) -> None: ...


class B2Upload(Protocol):
    """Upload a WAV to Backblaze B2 object storage.

    Args:
        wav_path: Path to the narration WAV.

    Returns:
        Public B2 URL for the uploaded clip.

    Raises:
        RuntimeError: On upload failure. Must never return a placeholder
            URL — the caller relies on this URL being a real artifact.
    """

    def __call__(self, wav_path: str) -> str: ...


@dataclass(frozen=True)
class _AudioHelpers:
    tts_generate: TtsGenerate
    whisperx_align: WhisperxAlign
    loudness_normalize: LoudnessNormalize
    b2_upload: B2Upload


_HELPERS: _AudioHelpers | None = None


class AudioHelpersNotConfigured(RuntimeError):
    """Raised when ``render_audio`` is invoked before helpers are registered."""


def set_audio_helpers(
    *,
    tts_generate: TtsGenerate,
    whisperx_align: WhisperxAlign,
    loudness_normalize: LoudnessNormalize,
    b2_upload: B2Upload,
) -> None:
    """Register the four production helpers used by :func:`render_audio`."""
    global _HELPERS
    _HELPERS = _AudioHelpers(
        tts_generate=tts_generate,
        whisperx_align=whisperx_align,
        loudness_normalize=loudness_normalize,
        b2_upload=b2_upload,
    )


def clear_audio_helpers() -> None:
    """Clear the helper registry. Intended for test isolation."""
    global _HELPERS
    _HELPERS = None


def _get_helpers() -> _AudioHelpers:
    if _HELPERS is None:
        raise AudioHelpersNotConfigured(
            "render_audio helpers not configured — call set_audio_helpers "
            "before invoking the tool"
        )
    return _HELPERS


# ---------------------------------------------------------------------------
# Target loudness (matches ``critique.audio_invariants.LUFS_TARGET``).
# ---------------------------------------------------------------------------

TARGET_LUFS: float = -23.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _block_id(scene_num: int, voice_role: str) -> str:
    return f"scene_{scene_num:03d}_{voice_role}"


def _voice_role_of(voice: dict[str, Any]) -> str:
    for key in ("voice_id", "voice_role", "role"):
        raw = voice.get(key)
        if raw is not None and str(raw) != "":
            return str(raw)
    raise ValueError("voice entry missing voice_id / voice_role / role")


def _voice_text_of(voice: dict[str, Any]) -> str:
    text = voice.get("text") or voice.get("narration")
    if text is None:
        return ""
    return str(text)


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


@tool(context=True)
def render_audio(
    scenes: list[dict[str, Any]],
    voice_map: dict[str, str] | None = None,
    language: str = "en",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Synthesize per-voice narration, normalize loudness, align via WhisperX, upload to B2.

    Preconditions and postconditions are validated against
    :data:`AUDIO_CONTRACT` so the tool can be invoked from the DeepAgent
    task tool without being wrapped in a :class:`ContractEnforcer` hook
    stack.

    Args:
        scenes: List of scene dicts from the scenario stage. Each must
            carry ``id`` (int) and ``voices`` (non-empty list of
            ``{voice_id, text}`` dicts).
        voice_map: Optional ``voice_role -> concrete voice_id`` override
            (e.g. ``{"V1": "qwen3-tts:male_01"}``). When absent the
            voice role itself is used as the ``voice_id``.
        language: Language code (``"en"`` or ``"ru"``); forwarded to both
            TTS and WhisperX helpers.
        tool_context: Provided by the Strands runtime (context=True).

    Returns:
        A dict with:

        - ``whisperx_alignment`` — dict with ``total_duration_sec``,
          ``per_clip`` (block_id -> align dict), and ``language``.
        - ``narration_blocks`` — list of dicts, one per
          ``(scene, voice)`` pair, with the fields
          :class:`critique.audio_invariants.NarrationBlock` expects.
        - ``scene_count``, ``block_count``.

    Raises:
        ValueError: On empty ``scenes`` or malformed scene entries.
        AudioHelpersNotConfigured: When :func:`set_audio_helpers` has not
            been called.
        RuntimeError: On any TTS, WhisperX, loudness-normalization, or
            B2 upload failure (re-raised from the underlying helper).
    """
    if not scenes:
        raise ValueError("render_audio requires a non-empty scenes list")

    helpers = _get_helpers()

    narration_blocks: list[dict[str, Any]] = []
    per_clip: dict[str, dict[str, Any]] = {}

    for scene in scenes:
        scene_num = _scene_num(scene)
        voices = scene.get("voices") or []
        if not voices:
            raise ValueError(
                f"scene {scene_num} has no voices — render_audio cannot "
                "synthesize a silent scene"
            )

        for voice in voices:
            voice_role = _voice_role_of(voice)
            text = _voice_text_of(voice)
            if not text.strip():
                raise ValueError(
                    f"scene {scene_num} voice {voice_role!r} has empty text"
                )
            concrete_voice_id = (voice_map or {}).get(voice_role, voice_role)

            wav_path = helpers.tts_generate(scene_num, voice_role, text, language)
            if not isinstance(wav_path, str) or not wav_path:
                raise RuntimeError(
                    f"tts_generate returned invalid wav_path={wav_path!r} "
                    f"for scene {scene_num} {voice_role}"
                )

            helpers.loudness_normalize(wav_path, TARGET_LUFS)

            align = helpers.whisperx_align(wav_path, text, language)
            if not isinstance(align, dict):
                raise RuntimeError(
                    f"whisperx_align returned non-dict for scene {scene_num} "
                    f"{voice_role}: {type(align).__name__}"
                )
            duration = float(align.get("total_duration") or 0.0)
            if duration <= 0.0:
                raise RuntimeError(
                    f"whisperx_align produced non-positive duration for "
                    f"scene {scene_num} {voice_role}"
                )

            b2_url = helpers.b2_upload(wav_path)
            if not isinstance(b2_url, str) or not b2_url:
                raise RuntimeError(
                    f"b2_upload returned invalid url={b2_url!r} for "
                    f"scene {scene_num} {voice_role}"
                )

            block_id = _block_id(scene_num, voice_role)
            narration_blocks.append(
                {
                    "block_id": block_id,
                    "wav_path": wav_path,
                    "scene_num": scene_num,
                    "voice_role": voice_role,
                    "language": language,
                    "voice_id": concrete_voice_id,
                    "b2_url": b2_url,
                    "duration_sec": duration,
                }
            )
            per_clip[block_id] = {
                "total_duration": duration,
                "word_count": int(align.get("word_count", 0)),
                "words": list(align.get("words") or []),
            }
            logger.debug(
                "scene=<%d>, voice=<%s>, duration=<%.2f> | rendered narration block",
                scene_num,
                voice_role,
                duration,
            )

    total_duration = sum(b["duration_sec"] for b in narration_blocks)
    whisperx_alignment = {
        "total_duration_sec": round(total_duration, 3),
        "per_clip": per_clip,
        "language": language,
    }

    _persist_state(
        tool_context,
        whisperx_alignment=whisperx_alignment,
        narration_blocks=narration_blocks,
    )

    logger.info(
        "scene_count=<%d>, block_count=<%d>, total_duration_sec=<%.2f> | "
        "render_audio complete",
        len(scenes),
        len(narration_blocks),
        total_duration,
    )
    return {
        "whisperx_alignment": whisperx_alignment,
        "narration_blocks": narration_blocks,
        "scene_count": len(scenes),
        "block_count": len(narration_blocks),
    }


# ---------------------------------------------------------------------------
# Private — state plumbing
# ---------------------------------------------------------------------------


def _scene_num(scene: dict[str, Any]) -> int:
    raw: Any = None
    for key in ("id", "scene_num", "scene_id"):
        if key in scene and scene[key] is not None:
            raw = scene[key]
            break
    if raw is None:
        raise ValueError("scene entry missing id / scene_num / scene_id")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"scene id must be int-convertible, got {raw!r}") from exc


def _persist_state(
    tool_context: ToolContext | None,
    *,
    whisperx_alignment: dict[str, Any],
    narration_blocks: list[dict[str, Any]],
) -> None:
    if tool_context is None:
        return
    agent = getattr(tool_context, "agent", None)
    if agent is None:
        return
    state = getattr(agent, "state", None)
    if state is None or not hasattr(state, "set"):
        return
    state.set("whisperx_alignment", whisperx_alignment)
    state.set("narration_blocks", narration_blocks)
    state.set("_audio_stage_complete", True)
    state.set("_audio_needs_regeneration", False)


__all__ = [
    "AudioHelpersNotConfigured",
    "B2Upload",
    "LoudnessNormalize",
    "TARGET_LUFS",
    "TtsGenerate",
    "WhisperxAlign",
    "clear_audio_helpers",
    "render_audio",
    "set_audio_helpers",
]
