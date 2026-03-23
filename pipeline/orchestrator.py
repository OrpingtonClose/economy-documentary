#!/usr/bin/env python3
"""
Master Pipeline Orchestrator — OTIO-Centric Audio-First Workflow
=================================================================
Runs the complete documentary production pipeline:

  Phase 1: SCRIPT PARSE → scene list
  Phase 2: AUDIO GENERATION → narration WAV files → OTIO audio track
  Phase 3: PROMPT GENERATION → LTX-2.3 prompts (timing from OTIO audio)
  Phase 4: VIDEO GENERATION → video clips → OTIO video track
  Phase 5: ASSEMBLY → read OTIO → render final MP4
  Phase 6: EXPORT → .otio + FCPXML + EDL

The .otio file is the SINGLE SOURCE OF TRUTH at every stage.

Usage:
  # Full pipeline
  python3 -m pipeline.orchestrator --script narration_script.json \\
                                   --output-dir ./production

  # Individual phases
  python3 -m pipeline.orchestrator --phase audio --script narration_script.json
  python3 -m pipeline.orchestrator --phase prompts --otio timeline.otio
  python3 -m pipeline.orchestrator --phase video --otio timeline.otio --manifest prompts.json
  python3 -m pipeline.orchestrator --phase assemble --otio timeline.otio
  python3 -m pipeline.orchestrator --phase status --otio timeline.otio
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger(__name__)


def phase_audio(args):
    """Phase 2: Generate narration audio and build OTIO audio track."""
    from pipeline.otio_timeline import OTIOTimeline
    from pipeline.audio_generator import AudioGenerator

    otio_path = args.otio or os.path.join(args.output_dir, "war_economy_v8.otio")
    audio_dir = os.path.join(args.output_dir, "audio")

    # Create or load OTIO timeline
    otio_tl = OTIOTimeline(otio_path)
    if os.path.exists(otio_path):
        otio_tl.load()
        log.info(f"Loaded existing OTIO timeline: {otio_path}")
    else:
        otio_tl.create_empty("War Economy V8")
        log.info(f"Created new OTIO timeline: {otio_path}")

    # Load narration script
    with open(args.script) as f:
        narration_script = json.load(f)

    # Generate audio
    gen = AudioGenerator(otio_tl, audio_dir)
    result = gen.generate_all(
        narration_script,
        start_scene=args.start_scene or 0,
    )

    log.info(f"\nAudio generation complete:")
    log.info(f"  Scenes: {result['total_scenes']}")
    log.info(f"  Duration: {result['total_duration_min']} min")
    log.info(f"  OTIO: {otio_path}")

    return result


def phase_prompts(args):
    """Phase 3: Generate video prompts from OTIO audio timing."""
    from pipeline.otio_timeline import OTIOTimeline
    from pipeline.prompt_generator import generate_all_prompts

    otio_path = args.otio or os.path.join(args.output_dir, "war_economy_v8.otio")
    prompts_path = args.manifest or os.path.join(args.output_dir, "video_prompts.json")

    # Load OTIO timeline (must have audio track populated)
    otio_tl = OTIOTimeline(otio_path)
    otio_tl.load()

    # Load narration script for text content
    with open(args.script) as f:
        narration_data = json.load(f)

    # Generate prompts
    all_prompts = generate_all_prompts(otio_tl, narration_data)

    # Save prompts
    with open(prompts_path, "w") as f:
        json.dump(all_prompts, f, indent=2)

    log.info(f"\nPrompt generation complete:")
    log.info(f"  Total prompts: {len(all_prompts)}")
    log.info(f"  Avg words: {sum(p['word_count'] for p in all_prompts) / max(1, len(all_prompts)):.0f}")
    log.info(f"  Saved: {prompts_path}")

    return all_prompts


def phase_video(args):
    """Phase 4: Generate video clips using LTX-2.3."""
    from pipeline.video_generator import VideoGenerator

    otio_path = args.otio or os.path.join(args.output_dir, "war_economy_v8.otio")
    prompts_path = args.manifest or os.path.join(args.output_dir, "video_prompts.json")
    clips_dir = os.path.join(args.output_dir, "clips")

    with open(prompts_path) as f:
        prompts = json.load(f)

    gen = VideoGenerator(
        otio_path=otio_path,
        output_dir=clips_dir,
        b2_key_id=args.b2_key_id,
        b2_app_key=args.b2_app_key,
    )
    results = gen.generate_all(prompts, start_at=args.start_at or 0)

    completed = sum(1 for r in results if r["status"] == "complete")
    log.info(f"\nVideo generation complete: {completed}/{len(prompts)} clips")

    return results


def phase_assemble(args):
    """Phase 5: Assemble final video from OTIO timeline."""
    from pipeline.assembler import Assembler

    otio_path = args.otio or os.path.join(args.output_dir, "war_economy_v8.otio")
    clips_dir = os.path.join(args.output_dir, "clips")
    audio_dir = os.path.join(args.output_dir, "audio")

    asm = Assembler(
        otio_path=otio_path,
        output_dir=args.output_dir,
        clips_dir=clips_dir,
        audio_dir=audio_dir,
    )

    if args.scene:
        asm.assemble_scene(args.scene)
    else:
        final_path = asm.assemble_all(
            upload_final=args.upload,
            b2_key_id=args.b2_key_id,
            b2_app_key=args.b2_app_key,
        )
        return final_path


def phase_status(args):
    """Print OTIO timeline status."""
    from pipeline.otio_timeline import OTIOTimeline

    otio_path = args.otio or os.path.join(args.output_dir, "war_economy_v8.otio")
    otio_tl = OTIOTimeline(otio_path)
    otio_tl.load()
    otio_tl.print_status()

    # Detailed per-scene breakdown
    status = otio_tl.get_timeline_status()
    print(f"\n{'Scene':>6} {'Narration':>10} {'Video':>8} {'Gaps':>8} {'Clips':>6} {'Status'}")
    print("-" * 55)
    for sn in sorted(status["scenes"].keys()):
        s = status["scenes"][sn]
        stat = "OK" if s["video_gaps_sec"] < 0.5 else f"GAP {s['video_gaps_sec']:.1f}s"
        print(f"{sn:6d} {s['narration_sec']:9.1f}s {s['video_sec']:7.1f}s "
              f"{s['video_gaps_sec']:7.1f}s {s['clips']:5d}  {stat}")


def phase_export(args):
    """Phase 6: Export OTIO to additional formats."""
    from pipeline.otio_timeline import OTIOTimeline

    otio_path = args.otio or os.path.join(args.output_dir, "war_economy_v8.otio")
    otio_tl = OTIOTimeline(otio_path)
    otio_tl.load()

    # FCPXML
    fcpxml_path = os.path.join(args.output_dir, "war_economy_v8.fcpxml")
    try:
        otio_tl.export_fcpxml(fcpxml_path)
        log.info(f"Exported FCPXML: {fcpxml_path}")
    except Exception as e:
        log.warning(f"FCPXML export failed: {e}")

    # Assembly JSON
    assembly_path = os.path.join(args.output_dir, "assembly_manifest.json")
    otio_tl.to_assembly_json(assembly_path)
    log.info(f"Exported assembly JSON: {assembly_path}")

    # Status JSON
    status = otio_tl.get_timeline_status()
    status_path = os.path.join(args.output_dir, "timeline_status.json")
    with open(status_path, "w") as f:
        json.dump(status, f, indent=2)
    log.info(f"Exported status JSON: {status_path}")


def main():
    parser = argparse.ArgumentParser(
        description="War Economy — OTIO-Centric Production Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Phases:
  audio     Generate narration audio → OTIO audio track
  prompts   Generate LTX-2.3 video prompts from OTIO audio timing
  video     Generate video clips with LTX-2.3 → OTIO video track
  assemble  Render final video from OTIO timeline
  status    Print OTIO timeline status
  export    Export OTIO to FCPXML/EDL/JSON
  all       Run audio → prompts → video → assemble → export
        """
    )

    parser.add_argument("--phase", default="all",
                        choices=["audio", "prompts", "video", "assemble", "status", "export", "all"],
                        help="Pipeline phase to run")
    parser.add_argument("--script", help="Path to narration_script.json")
    parser.add_argument("--otio", help="Path to .otio timeline file")
    parser.add_argument("--manifest", help="Path to video prompts JSON")
    parser.add_argument("--output-dir", default="./production_v8", help="Output directory")
    parser.add_argument("--scene", type=int, help="Process single scene")
    parser.add_argument("--start-scene", type=int, help="Start from this scene (audio phase)")
    parser.add_argument("--start-at", type=int, help="Start from this clip index (video phase)")
    parser.add_argument("--upload", action="store_true", help="Upload final to B2")
    parser.add_argument("--b2-key-id", default=None, help="B2 key ID")
    parser.add_argument("--b2-app-key", default=None, help="B2 app key")
    parser.add_argument("--gpu", type=int, default=None, help="GPU index")

    args = parser.parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    if args.gpu is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(args.gpu)

    start_time = time.time()

    print(f"{'='*60}")
    print(f"WAR ECONOMY — OTIO-Centric Production Pipeline V8")
    print(f"{'='*60}")
    print(f"Phase: {args.phase}")
    print(f"Output: {args.output_dir}")
    print(f"{'='*60}\n")

    if args.phase == "status":
        phase_status(args)
        return

    if args.phase in ("audio", "all"):
        if not args.script:
            log.error("--script required for audio phase")
            sys.exit(1)
        phase_audio(args)

    if args.phase in ("prompts", "all"):
        if not args.script:
            log.error("--script required for prompts phase")
            sys.exit(1)
        phase_prompts(args)

    if args.phase in ("video", "all"):
        phase_video(args)

    if args.phase in ("assemble", "all"):
        phase_assemble(args)

    if args.phase in ("export", "all"):
        phase_export(args)

    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"PIPELINE COMPLETE — {args.phase} in {elapsed:.0f}s ({elapsed / 60:.1f} min)")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
