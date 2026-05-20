"""
TTS tools -- Qwen3-TTS generation wrapper.

Generates narration WAV files using Qwen3-TTS on a GPU worker.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import wave
from urllib.request import Request, urlopen
from urllib.error import URLError

logger = logging.getLogger(__name__)

_OUTPUT_BASE = os.environ.get(
    "TTS_OUTPUT_DIR", "/tmp/documentary-pipeline/audio"
)

# Approximate speech rate: ~0.3s per word (for test duration estimation)
_SECONDS_PER_WORD = 0.3
_SAMPLE_RATE = 24000


def _estimate_duration(text: str) -> float:
    """Estimate speech duration from text length."""
    word_count = len(text.split())
    return max(1.0, word_count * _SECONDS_PER_WORD)


def _generate_silent_wav(output_path: str, duration: float) -> None:
    """Generate a silent WAV file with the specified duration."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    num_frames = int(_SAMPLE_RATE * duration)
    with wave.open(output_path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_SAMPLE_RATE)
        # Write silence — 16-bit PCM silence is simply zero bytes
        silent_data = b'\x00' * (num_frames * 2)
        wf.writeframes(silent_data)


def generate_narration(
    scene_num: int,
    voice_role: str,
    text: str,
    output_dir: str = "",
    language: str = "",
    tool_context=None,
) -> str:
    """Generate narration WAV file using Qwen3-TTS.

    Args:
        scene_num: Scene number (1-based).
        voice_role: Voice role identifier (e.g., "V1", "V2", "V3").
        text: Narration text to synthesize.
        output_dir: Optional output directory override.
        language: Explicit language code ("en" or "ru"). If empty,
                  inferred from voice_role suffix (e.g., "V1_RU" -> "ru").

    Returns:
        JSON string with WAV path and duration.
    """
    out_dir = output_dir or _OUTPUT_BASE
    os.makedirs(out_dir, exist_ok=True)

    filename = f"scene_{scene_num:03d}_{voice_role}.wav"
    wav_path = os.path.join(out_dir, filename)

    duration = _estimate_duration(text)

    # Skip regeneration if WAV already exists, is non-empty, and text is equivalent
    # FIX: Compare stripped text content instead of hash. Prevents regeneration
    # when agent rephrases with minor whitespace/punctuation differences.
    sidecar_path = wav_path.replace(".wav", ".txt")
    normalized_text = text.strip()
    if os.path.isfile(wav_path) and os.path.getsize(wav_path) > 0:
        text_matches = False
        if os.path.isfile(sidecar_path):
            try:
                with open(sidecar_path, "r", encoding="utf-8") as sf:
                    cached_text = sf.read()
                # Backward-compat: old sidecars stored 12-char hash
                if len(cached_text.strip()) == 12 and all(c in hashlib.sha256(b"").hexdigest() for c in cached_text.strip()[:8]):
                    # Old hash format — treat as mismatch to regenerate once
                    text_matches = False
                else:
                    text_matches = cached_text.strip() == normalized_text
            except (OSError, UnicodeDecodeError):
                pass
        if not text_matches:
            logger.info("Text changed for %s, regenerating", wav_path)
        else:
            # Read actual duration from WAV header
            try:
                with wave.open(wav_path, "r") as wf:
                    actual_duration = wf.getnframes() / wf.getframerate()
                    actual_sr = wf.getframerate()
                logger.info(
                    "Skipping existing WAV %s (%.2fs)", wav_path, actual_duration
                )
                return json.dumps(
                    {
                        "status": "skipped",
                        "mode": "cached",
                        "wav_path": wav_path,
                        "duration": round(actual_duration, 2),
                        "sample_rate": actual_sr,
                        "text_length": len(text),
                        "word_count": len(text.split()),
                    }
                )
            except wave.Error:
                logger.warning("Corrupt WAV %s, regenerating", wav_path)

    def _write_sidecar(path: str, content: str) -> None:
        """Write text sidecar so cache can detect stale content.

        FIX: Stores full normalized text instead of hash. This prevents
        regeneration when the agent rephrases with minor changes.
        """
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError:
            pass

    # Production mode: call Qwen3-TTS on GPU worker.
    # ARCHITECTURE INVARIANT: TTS must produce real audio with real durations.
    # Downstream timing (visual direction, video generation) depends on real
    # narration durations.  Silent audio is NEVER acceptable.
    #
    # TTS must use the Qwen3-TTS GPU worker. No edge-tts fallback.
    # The provisioner allocates a TTS worker when it receives the task
    # list from the audio stage. If no worker is available, the tool
    # fails honestly.
    from worker_provisioner import get_provisioner
    provisioner = get_provisioner()
    try:
        spec = provisioner.ensure_available("tts")
        gpu_worker_url = spec.worker_url or f"http://localhost:{spec.local_port}"
    except Exception as exc:
        return json.dumps({
            "scene_num": scene_num,
            "status": "failed",
            "error": f"TTS worker unavailable: {exc}",
        })

    # Determine language: explicit param takes priority, then suffix convention
    voice = voice_role
    if voice_role.endswith("_RU"):
        voice = voice_role[:-3]  # Strip _RU suffix
        lang = language if language else "ru"
    elif voice_role.endswith("_EN"):
        voice = voice_role[:-3]  # Strip _EN suffix
        lang = language if language else "en"
    else:
        lang = language if language else "en"

    payload = json.dumps({
        "text": text,
        "voice": voice,
        "language": lang,
        "scene_num": scene_num,
    }).encode("utf-8")

    tts_url = f"{gpu_worker_url.rstrip('/')}/tts"
    req = Request(tts_url, data=payload, headers={"Content-Type": "application/json"})

    # Use graduated recovery middleware instead of bare RuntimeError.
    # The middleware handles: retry → creative amendment → env assessment → human escalation.
    # Build payload from logical params inside the function so creative amendments
    # (e.g. _tts_amend_chunk shortening text) actually reach the TTS worker.
    def _call_tts_worker(url=tts_url, text=text, voice=voice, language=lang, scene_num=scene_num):
        inner_payload = json.dumps({
            "text": text,
            "voice": voice,
            "language": language,
            "scene_num": scene_num,
        }).encode("utf-8")
        req_inner = Request(url, data=inner_payload, headers={"Content-Type": "application/json"})
        try:
            with urlopen(req_inner) as resp:
                result_bytes = resp.read()
                return {
                    "wav_bytes": result_bytes,
                    "actual_duration": float(resp.headers.get("X-Audio-Duration", str(duration))),
                    "actual_sample_rate": int(resp.headers.get("X-Sample-Rate", str(_SAMPLE_RATE))),
                    "gen_time": float(resp.headers.get("X-Gen-Time", "0")),
                    "actual_text": text,
                }
        except URLError as e:
            # Worker unreachable — fail honestly.  The pipeline's recovery
            # system will handle this; tools must NOT provision VMs.
            raise RuntimeError(
                f"TTS worker unreachable at {url}: {e}"
            ) from e

    from recovery import execute_with_recovery, TTS_POLICY
    tts_result = execute_with_recovery(
        operation=_call_tts_worker,
        operation_name=f"tts_scene{scene_num}_{voice_role}",
        kwargs={"url": tts_url, "text": text, "voice": voice, "language": lang, "scene_num": scene_num},
        policy=TTS_POLICY,
        context={"scene_num": scene_num, "voice": voice_role, "text_len": len(text)},
    )

    # If recovery returned None (human chose "skip"), raise so pipeline stops
    if tts_result is None:
        raise RuntimeError(
            f"TTS generation for scene {scene_num} {voice_role} skipped by human. "
            "Pipeline cannot continue without narration — all video timing depends on it."
        )

    wav_bytes = tts_result["wav_bytes"]
    actual_duration = tts_result["actual_duration"]
    actual_sample_rate = tts_result["actual_sample_rate"]
    gen_time = tts_result["gen_time"]

    os.makedirs(os.path.dirname(wav_path) or ".", exist_ok=True)
    with open(wav_path, "wb") as f:
        f.write(wav_bytes)
    # Only write sidecar if the text wasn't amended by recovery (e.g. _tts_amend_chunk).
    # If recovery shortened the text, the WAV doesn't match the original — don't cache it
    # or the next run would falsely serve truncated audio as a cache hit.
    actual_text = tts_result.get("actual_text", text)
    if actual_text.strip() == normalized_text:
        _write_sidecar(sidecar_path, normalized_text)
    else:
        logger.warning(
            "TTS text was amended by recovery (orig=%d chars, actual=%d chars) — skipping sidecar",
            len(text), len(actual_text),
        )
        # Remove stale sidecar if it exists
        if os.path.isfile(sidecar_path):
            os.remove(sidecar_path)

    logger.info(
        "Generated narration WAV %s (%.2fs, gen=%.1fs, %d words)",
        wav_path, actual_duration, gen_time, len(text.split()),
    )

    # Upload TTS clip to B2 immediately after creation
    try:
        from tools.b2_checkpoint import upload_tts_clip
        upload_tts_clip(wav_path, sidecar_path)
    except Exception as b2_err:
        logger.warning("B2 upload failed for TTS clip %s: %s", wav_path, b2_err)

    return json.dumps(
        {
            "status": "generated",
            "mode": "production",
            "wav_path": wav_path,
            "duration": round(actual_duration, 2),
            "sample_rate": actual_sample_rate,
            "text_length": len(text),
            "word_count": len(text.split()),
            "gen_time": round(gen_time, 2),
        }
    )

