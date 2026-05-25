"""TTS worker :class:`ToolSimulator` per ``SIMULATION.md`` §2.

Provides mocked ``generate_tts`` / ``align_whisperx`` / ``check_tts_health``
tools backed by a shared ``StateRegistry``. Component 04-audio-agent and
05-timing-loop wire this into their experiments.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from strands_evals.simulation.tool_simulator import StateRegistry, ToolSimulator  # type: ignore[import-not-found]

_SHARE_STATE_ID = "audio_pipeline"

_INITIAL_STATE_DESCRIPTION = (
    "Single TTS worker with XTTS-v2 loaded. Generates audio at roughly "
    "realtime (a 10 s scene takes ~10 s to synthesize). WhisperX "
    "alignment runs on the same VM and takes roughly 20% of audio "
    "duration. Voice identity is deterministic per voice_id. 1% of "
    "requests fail with 'model reload in progress'."
)


class TtsResponse(BaseModel):
    """Output of a successful TTS synthesis request."""

    wav_path: str
    duration_sec: float
    voice_id: str
    sample_rate: int = 24000


class WhisperXResponse(BaseModel):
    """Forced-alignment output, matching WhisperX word-level timestamps."""

    word_timestamps: list[dict[str, float | str]]
    total_duration_sec: float
    language: str


class TtsHealth(BaseModel):
    """Health snapshot of the TTS worker."""

    status: Literal["ok", "model_loading", "down"]
    loaded_model: str
    voice_ids_available: list[str]


def build_tts_worker_simulator(
    *,
    state_registry: StateRegistry | None = None,
    model: str | None = None,
) -> ToolSimulator:
    """Construct the TTS worker :class:`ToolSimulator`.

    Args:
        state_registry: Optional shared :class:`StateRegistry`. A fresh
            one is created if omitted.
        model: Model string for the LLM-backed simulator. ``None``
            defers to the :class:`ToolSimulator` default.

    Returns:
        A :class:`ToolSimulator` with the three TTS tools registered.
    """
    sim = ToolSimulator(state_registry=state_registry, model=model)

    @sim.tool(
        output_schema=TtsResponse,
        share_state_id=_SHARE_STATE_ID,
        initial_state_description=_INITIAL_STATE_DESCRIPTION,
    )
    def generate_tts(
        text: str,
        voice_id: str,
        language: str = "en",
    ) -> TtsResponse:
        """Synthesize ``text`` into a wav and return path + duration.

        Args:
            text: SSML-or-plain narration body for one scene.
            voice_id: Deterministic voice selector.
            language: BCP-47 language tag. Defaults to English.
        """
        raise NotImplementedError  # type: ignore[return]

    @sim.tool(
        output_schema=WhisperXResponse,
        share_state_id=_SHARE_STATE_ID,
        initial_state_description=_INITIAL_STATE_DESCRIPTION,
    )
    def align_whisperx(wav_path: str, text: str, language: str = "en") -> WhisperXResponse:
        """Return word-level timestamps from WhisperX forced alignment."""
        raise NotImplementedError  # type: ignore[return]

    @sim.tool(
        output_schema=TtsHealth,
        share_state_id=_SHARE_STATE_ID,
        initial_state_description=_INITIAL_STATE_DESCRIPTION,
    )
    def check_tts_health() -> TtsHealth:
        """Return a health snapshot of the TTS worker."""
        raise NotImplementedError  # type: ignore[return]

    return sim
