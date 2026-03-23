#!/usr/bin/env python3
"""
Audio-First Pipeline — Qwen3-TTS Narration Generator
======================================================
Generates narration audio FIRST, then places it on the OTIO timeline.
All subsequent pipeline stages derive timing from the audio track.

Flow:
  1. Parse narration script (JSON with scenes + voice segments)
  2. Generate audio per segment using Qwen3-TTS VoiceDesign
  3. Add each audio clip to the OTIO timeline narration track
  4. Add placeholder gaps on video track (to be filled later)
  5. Save the OTIO timeline — it's now the source of truth for timing

Usage (standalone):
  python3 audio_generator.py --script narration_script.json \\
                              --otio war_economy_v8.otio \\
                              --output-dir ./audio

Usage (from pipeline):
  from pipeline.audio_generator import AudioGenerator
  gen = AudioGenerator(otio_path, audio_dir)
  gen.generate_all(narration_script)
"""

import json
import logging
import os
import re
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger(__name__)

# Voice design instructions for each narrator
VOICE_DESIGNS = {
    "V1": {
        "name": "V1_Financial_Journalist",
        "instruct": (
            "Male, early 40s, baritone voice. Speaks like a seasoned financial journalist "
            "delivering a hard-hitting documentary. Sharp, confident, data-driven but human. "
            "Measured pace with emphasis on key numbers. Slight gravitas. "
            "Think of a PBS Frontline narrator — authoritative yet accessible. "
            "Clear enunciation, moderate pace around 145 words per minute."
        ),
    },
    "V2": {
        "name": "V2_Intelligence_Analyst",
        "instruct": (
            "Male, late 30s, slightly deeper voice than V1. Speaks like an intelligence analyst "
            "briefing senior officials — precise, methodical, connecting dots others miss. "
            "Calm and controlled, with subtle intensity when revealing key findings. "
            "Slightly faster pace than V1, around 155 words per minute. "
            "Think of a serious investigative reporter. Clinical but compelling."
        ),
    },
    "V3": {
        "name": "V3_Historian",
        "instruct": (
            "Male, early 50s, warm tenor-baritone. Speaks like a distinguished historian — "
            "contemplative, authoritative, taking the long view. Draws on centuries of context. "
            "Slightly slower, more deliberate pace around 140 words per minute. "
            "Think of Ken Burns documentary narration — thoughtful pauses, "
            "weight on historical parallels. Measured gravitas."
        ),
    },
}


def parse_narration_segments(narration_text):
    """
    Parse narration text into segments by voice.

    Supports formats:
      V1 (Financial Journalist): "text..."
      V1: "text..."
      V1: text...

    Returns list of (voice_id, text) tuples.
    """
    segments = []
    pattern = r'(V[123])\s*(?:\([^)]*\))?\s*:\s*"?(.*?)(?:"|(?=\nV[123]\s*(?:\([^)]*\))?\s*:)|\Z)'
    matches = list(re.finditer(pattern, narration_text, re.DOTALL))

    if not matches:
        clean = narration_text.strip().strip('"')
        if clean:
            segments.append(("V1", clean))
        return segments

    for m in matches:
        voice = m.group(1)
        text = m.group(2).strip().strip('"').strip()
        text = re.sub(r'\s+', ' ', text)
        if text:
            segments.append((voice, text))

    return segments


class AudioGenerator:
    """
    Generates narration audio and places it on the OTIO timeline.

    This is the FIRST stage of the pipeline. After this runs, the OTIO
    timeline contains all narration with precise timing, and placeholder
    gaps on the video track.
    """

    def __init__(self, otio_timeline, audio_output_dir):
        """
        Args:
            otio_timeline: OTIOTimeline instance (already created or loaded)
            audio_output_dir: directory to write .wav files
        """
        self.otio = otio_timeline
        self.audio_dir = str(audio_output_dir)
        os.makedirs(self.audio_dir, exist_ok=True)
        self.model = None

    def _load_model(self):
        """Load Qwen3-TTS model (lazy, once)."""
        if self.model is not None:
            return

        import torch

        log.info("Loading Qwen3-TTS VoiceDesign model...")
        t0 = time.time()
        from qwen_tts import Qwen3TTSModel

        self.model = Qwen3TTSModel.from_pretrained(
            "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
            device_map="cuda:0",
            dtype=torch.bfloat16,
            attn_implementation="flash_attention_2",
        )
        log.info(f"Model loaded in {time.time() - t0:.1f}s")

    def _generate_segment(self, voice_id, text, output_path, max_retries=2):
        """Generate audio for a single narration segment."""
        import torch
        import soundfile as sf

        voice_cfg = VOICE_DESIGNS[voice_id]

        for attempt in range(max_retries + 1):
            try:
                est_tokens = max(2048, int(len(text.split()) * 7.2))
                wavs, sr = self.model.generate_voice_design(
                    text=text,
                    language="English",
                    instruct=voice_cfg["instruct"],
                    max_new_tokens=est_tokens,
                )
                sf.write(output_path, wavs[0], sr)
                duration = len(wavs[0]) / sr
                return duration
            except Exception as e:
                if attempt < max_retries:
                    log.warning(f"  Attempt {attempt + 1} failed: {e}, retrying...")
                    torch.cuda.empty_cache()
                    time.sleep(2)
                else:
                    raise

    def generate_scene(self, scene_data, skip_existing=True):
        """
        Generate narration for a single scene and add to OTIO timeline.

        scene_data: dict with keys:
            - scene_number: int
            - scene_title: str
            - narration_text: str (with V1/V2/V3 markers)

        Returns: dict with scene generation results
        """
        self._load_model()

        scene_num = scene_data["scene_number"]
        scene_title = scene_data.get("scene_title", "")
        scene_dir = os.path.join(self.audio_dir, f"scene_{scene_num:02d}")
        os.makedirs(scene_dir, exist_ok=True)

        # Check if scene already has a manifest (idempotent)
        manifest_path = os.path.join(scene_dir, "manifest.json")
        if skip_existing and os.path.exists(manifest_path):
            log.info(f"[Scene {scene_num}] {scene_title} — already complete, loading from manifest")
            with open(manifest_path) as f:
                manifest = json.load(f)
            # Add to OTIO from manifest
            self._add_manifest_to_otio(scene_num, manifest)
            return manifest

        log.info(f"\n{'='*60}")
        log.info(f"[Scene {scene_num}] {scene_title}")
        log.info(f"{'='*60}")

        segments = parse_narration_segments(scene_data["narration_text"])
        log.info(f"  Segments: {len(segments)}")

        scene_segments = []
        scene_duration = 0.0
        scene_start = time.time()

        for seg_idx, (voice_id, text) in enumerate(segments):
            seg_filename = f"seg_{seg_idx:02d}_{voice_id}.wav"
            seg_path = os.path.join(scene_dir, seg_filename)

            if skip_existing and os.path.exists(seg_path):
                import soundfile as sf
                info = sf.info(seg_path)
                dur = info.duration
                log.info(f"  [{seg_idx + 1}/{len(segments)}] {voice_id} — exists ({dur:.1f}s)")
            else:
                word_count = len(text.split())
                log.info(f"  [{seg_idx + 1}/{len(segments)}] {voice_id} | {word_count} words")
                try:
                    dur = self._generate_segment(voice_id, text, seg_path)
                    log.info(f"    -> {dur:.1f}s audio")
                except Exception as e:
                    log.error(f"    FAILED: {e}")
                    scene_segments.append({
                        "index": seg_idx, "voice": voice_id,
                        "file": seg_filename, "status": "failed", "error": str(e),
                    })
                    continue

            # Add to OTIO timeline
            self.otio.add_narration_clip(
                scene_num=scene_num,
                seg_index=seg_idx,
                audio_path=seg_path,
                duration_sec=dur,
                voice=voice_id,
                text_preview=text[:200],
            )

            scene_duration += dur
            scene_segments.append({
                "index": seg_idx,
                "voice": voice_id,
                "file": seg_filename,
                "text_preview": text[:100] + "..." if len(text) > 100 else text,
                "full_text": text,
                "duration_sec": round(dur, 3),
                "status": "complete",
            })

        # Add placeholder video gap and music gap for this scene
        if scene_duration > 0:
            self.otio.add_narration_scene_placeholder_video(scene_num, scene_duration)
            self.otio.add_music_placeholder(scene_num, scene_duration)

        elapsed = time.time() - scene_start
        manifest = {
            "scene_number": scene_num,
            "scene_title": scene_title,
            "segment_count": len(scene_segments),
            "total_duration": round(scene_duration, 3),
            "generation_time": round(elapsed, 1),
            "segments": scene_segments,
        }

        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

        log.info(f"  Scene {scene_num} complete: {scene_duration:.1f}s audio in {elapsed:.1f}s")
        return manifest

    def _add_manifest_to_otio(self, scene_num, manifest):
        """Add previously generated segments to OTIO from a manifest."""
        scene_dir = os.path.join(self.audio_dir, f"scene_{scene_num:02d}")
        scene_duration = 0.0

        for seg in manifest.get("segments", []):
            if seg.get("status") != "complete":
                continue
            seg_path = os.path.join(scene_dir, seg["file"])
            dur = seg["duration_sec"]

            self.otio.add_narration_clip(
                scene_num=scene_num,
                seg_index=seg["index"],
                audio_path=seg_path,
                duration_sec=dur,
                voice=seg["voice"],
                text_preview=seg.get("text_preview", ""),
            )
            scene_duration += dur

        if scene_duration > 0:
            self.otio.add_narration_scene_placeholder_video(scene_num, scene_duration)
            self.otio.add_music_placeholder(scene_num, scene_duration)

    def generate_all(self, narration_script, start_scene=0, skip_existing=True):
        """
        Generate narration for all scenes and build the OTIO audio track.

        narration_script: list of scene dicts from narration_script.json
        """
        log.info("=" * 60)
        log.info("AUDIO-FIRST PIPELINE — Qwen3-TTS Narration Generation")
        log.info("=" * 60)
        log.info(f"Scenes: {len(narration_script)} | Output: {self.audio_dir}")

        results = []
        total_duration = 0.0

        for scene_data in narration_script:
            scene_num = scene_data["scene_number"]
            if scene_num < start_scene:
                continue

            manifest = self.generate_scene(scene_data, skip_existing=skip_existing)
            results.append(manifest)
            total_duration += manifest.get("total_duration", 0)

            # Add inter-scene gap (except after last scene)
            if scene_num < len(narration_script):
                self.otio.add_scene_gap(scene_num)

        # Save OTIO timeline — it now contains all audio timing
        self.otio.save()

        # Save overall narration manifest
        overall = {
            "total_scenes": len(results),
            "total_segments": sum(r.get("segment_count", 0) for r in results),
            "total_duration_sec": round(total_duration, 2),
            "total_duration_min": round(total_duration / 60, 1),
            "scenes": results,
        }
        overall_path = os.path.join(self.audio_dir, "narration_manifest.json")
        with open(overall_path, "w") as f:
            json.dump(overall, f, indent=2)

        log.info(f"\n{'='*60}")
        log.info(f"NARRATION COMPLETE: {len(results)} scenes, "
                 f"{overall['total_segments']} segments, "
                 f"{overall['total_duration_min']} min")
        log.info(f"OTIO timeline saved: {self.otio.path}")
        log.info(f"{'='*60}")

        return overall


def main():
    """CLI entry point for standalone audio generation."""
    import argparse
    from pipeline.otio_timeline import OTIOTimeline

    parser = argparse.ArgumentParser(description="Audio-first narration generator")
    parser.add_argument("--script", required=True, help="Path to narration_script.json")
    parser.add_argument("--otio", required=True, help="Path for .otio timeline file")
    parser.add_argument("--output-dir", required=True, help="Audio output directory")
    parser.add_argument("--start-scene", type=int, default=0, help="Start from this scene")
    args = parser.parse_args()

    # Load or create OTIO timeline
    otio_tl = OTIOTimeline(args.otio)
    if os.path.exists(args.otio):
        otio_tl.load()
    else:
        otio_tl.create_empty("War Economy V8")

    # Load narration script
    with open(args.script) as f:
        narration_script = json.load(f)

    # Generate all narration
    gen = AudioGenerator(otio_tl, args.output_dir)
    gen.generate_all(narration_script, start_scene=args.start_scene)


if __name__ == "__main__":
    main()
