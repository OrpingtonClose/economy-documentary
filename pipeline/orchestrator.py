#!/usr/bin/env python3
"""
Master Pipeline Orchestrator — OTIO-Centric Audio-First Workflow
=================================================================
Runs the complete documentary production pipeline:

  Phase 1: SCRIPT PARSE → scene list
  Phase 2: AUDIO GENERATION → narration WAV files → OTIO audio track
  Phase 3: PROMPT GENERATION → LTX-2.3 prompts → stored in OTIO metadata
  Phase 4: VIDEO GENERATION → video clips → OTIO video track + quality metadata
  Phase 5: ASSEMBLY → read OTIO → render final MP4
  Phase 6: EXPORT → .otio + FCPXML + EDL
  Phase 7: VALIDATE → print pipeline state per scene

The .otio file is the SINGLE SOURCE OF TRUTH at every stage.
Audio-first flow is ENFORCED — the orchestrator refuses to run phases
out of order.

Usage:
  # Full pipeline
  python3 -m pipeline.orchestrator --script narration_script.json \\
                                   --output-dir ./production

  # Individual phases
  python3 -m pipeline.orchestrator --phase audio --script narration_script.json
  python3 -m pipeline.orchestrator --phase prompts --otio timeline.otio --script narration_script.json
  python3 -m pipeline.orchestrator --phase video --otio timeline.otio
  python3 -m pipeline.orchestrator --phase assemble --otio timeline.otio
  python3 -m pipeline.orchestrator --phase validate --otio timeline.otio
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

    otio_path = args.otio or os.path.join(args.output_dir, "war_economy_v9.otio")
    audio_dir = os.path.join(args.output_dir, "audio")

    # Create or load OTIO timeline
    otio_tl = OTIOTimeline(otio_path)
    if os.path.exists(otio_path):
        otio_tl.load()
        log.info(f"Loaded existing OTIO timeline: {otio_path}")
    else:
        otio_tl.create_empty("War Economy V9")
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
    """Phase 3: Generate video prompts from OTIO audio timing.

    ENFORCES audio-first: refuses to run if the audio track is empty.
    Stores prompts directly in OTIO metadata (single source of truth)
    and also exports a derivative JSON for VM deployment.
    """
    from pipeline.otio_timeline import OTIOTimeline
    from pipeline.prompt_generator import generate_all_prompts

    otio_path = args.otio or os.path.join(args.output_dir, "war_economy_v9.otio")
    prompts_path = args.manifest or os.path.join(args.output_dir, "video_prompts.json")

    # Load OTIO timeline
    otio_tl = OTIOTimeline(otio_path)
    otio_tl.load()

    # GATE: Audio must be complete before generating prompts
    if not otio_tl.validate_audio_complete():
        log.error("AUDIO-FIRST GATE FAILED: No narration audio found on OTIO timeline.")
        log.error("Run --phase audio first to populate the audio track.")
        sys.exit(1)

    # If targeting a specific scene, validate audio for that scene
    if args.scene:
        if not otio_tl.validate_audio_complete(scene_num=args.scene):
            log.error(f"AUDIO-FIRST GATE FAILED: No audio for scene {args.scene}.")
            sys.exit(1)

    # Load narration script for text content
    with open(args.script) as f:
        narration_data = json.load(f)

    # Generate prompts
    all_prompts = generate_all_prompts(otio_tl, narration_data)

    # Store prompts in OTIO metadata (single source of truth)
    scenes_prompts = {}
    for p in all_prompts:
        sn = p["scene_number"]
        if sn not in scenes_prompts:
            scenes_prompts[sn] = []
        scenes_prompts[sn].append(p)

    for sn, prompts_list in sorted(scenes_prompts.items()):
        otio_tl.store_all_scene_prompts(sn, prompts_list)
        log.info(f"  Stored {len(prompts_list)} prompts in OTIO for scene {sn}")

    otio_tl.save()

    # Export derivative JSON for VM deployment (prompts leave OTIO only via this export)
    exported = otio_tl.export_prompts_json(prompts_path)

    log.info(f"\nPrompt generation complete:")
    log.info(f"  Total prompts: {len(all_prompts)}")
    log.info(f"  Avg words: {sum(p['word_count'] for p in all_prompts) / max(1, len(all_prompts)):.0f}")
    log.info(f"  Stored in OTIO: {otio_path}")
    log.info(f"  Exported JSON: {prompts_path}")

    return all_prompts


def phase_video(args):
    """Phase 4: Generate video clips using LTX-2.3.

    ENFORCES prompt-first: refuses to run if no prompts are stored.
    Reads prompts from OTIO (via export), writes quality metadata back.
    """
    from pipeline.otio_timeline import OTIOTimeline
    from pipeline.video_generator import VideoGenerator

    otio_path = args.otio or os.path.join(args.output_dir, "war_economy_v9.otio")
    prompts_path = args.manifest or os.path.join(args.output_dir, "video_prompts.json")
    clips_dir = os.path.join(args.output_dir, "clips")

    # Load OTIO timeline for validation
    otio_tl = OTIOTimeline(otio_path)
    otio_tl.load()

    # GATE: Prompts must exist before generating video
    if not otio_tl.validate_prompts_complete():
        log.error("PROMPT GATE FAILED: No prompts found in OTIO timeline.")
        log.error("Run --phase prompts first to generate and store prompts.")
        sys.exit(1)

    # Export prompts from OTIO to JSON for the video generator
    # (OTIO is the source, JSON is the derivative for VM consumption)
    if not os.path.exists(prompts_path):
        log.info("Exporting prompts from OTIO to JSON...")
        otio_tl.export_prompts_json(prompts_path)

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
    """Phase 5: Assemble final video from OTIO timeline.

    Warns about incomplete scenes if video track has gaps.
    """
    from pipeline.otio_timeline import OTIOTimeline
    from pipeline.assembler import Assembler

    otio_path = args.otio or os.path.join(args.output_dir, "war_economy_v9.otio")
    clips_dir = os.path.join(args.output_dir, "clips")
    audio_dir = os.path.join(args.output_dir, "audio")

    # Check for incomplete scenes
    otio_tl = OTIOTimeline(otio_path)
    otio_tl.load()

    if not otio_tl.validate_video_complete():
        pipeline_state = otio_tl.get_pipeline_state()
        incomplete = [sn for sn, s in pipeline_state.items() if not s["video"]]
        log.warning(f"VIDEO INCOMPLETE: Scenes {incomplete} still have video gaps.")
        log.warning("Assembly will proceed but these scenes will have black sections.")

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

    otio_path = args.otio or os.path.join(args.output_dir, "war_economy_v9.otio")
    otio_tl = OTIOTimeline(otio_path)
    otio_tl.load()
    otio_tl.print_status()

    # Detailed per-scene breakdown
    status = otio_tl.get_timeline_status()
    print(f"\n{'Scene':>6} {'Narration':>10} {'Video':>8} {'Gaps':>8} {'Clips':>6} {'Prompts':>8} {'Status'}")
    print("-" * 70)
    for sn in sorted(status["scenes"].keys()):
        s = status["scenes"][sn]
        prompts = s.get("prompts", 0)
        stat = "OK" if s["video_gaps_sec"] < 0.5 else f"GAP {s['video_gaps_sec']:.1f}s"
        print(f"{sn:6d} {s['narration_sec']:9.1f}s {s['video_sec']:7.1f}s "
              f"{s['video_gaps_sec']:7.1f}s {s['clips']:5d}  {prompts:7d}  {stat}")


def phase_validate(args):
    """Print the pipeline state for each scene: audio/prompts/video completion."""
    from pipeline.otio_timeline import OTIOTimeline

    otio_path = args.otio or os.path.join(args.output_dir, "war_economy_v9.otio")
    otio_tl = OTIOTimeline(otio_path)
    otio_tl.load()

    state = otio_tl.get_pipeline_state()

    def icon(val):
        return "\u2713" if val else "\u2717"

    print(f"\n{'='*50}")
    print(f"PIPELINE STATE VALIDATION")
    print(f"{'='*50}")
    print(f"{'Scene':>6} {'Audio':>7} {'Prompts':>9} {'Video':>7}")
    print("-" * 35)

    all_audio = True
    all_prompts = True
    all_video = True

    for sn in sorted(state.keys()):
        s = state[sn]
        print(f"{sn:6d}   {icon(s['audio']):>3}      {icon(s['prompts']):>3}      {icon(s['video']):>3}")
        if not s["audio"]:
            all_audio = False
        if not s["prompts"]:
            all_prompts = False
        if not s["video"]:
            all_video = False

    print(f"\n{'Summary':>8}: Audio {'COMPLETE' if all_audio else 'INCOMPLETE'} | "
          f"Prompts {'COMPLETE' if all_prompts else 'INCOMPLETE'} | "
          f"Video {'COMPLETE' if all_video else 'INCOMPLETE'}")

    # Report clips needing regeneration
    regen = otio_tl.get_clips_needing_regeneration()
    if regen:
        print(f"\nClips needing regeneration: {len(regen)}")
        for r in regen:
            print(f"  {r['clip_id']} (scene {r['scene']}): {r['reason']}")

    print(f"{'='*50}")


def phase_export(args):
    """Phase 6: Export OTIO to additional formats."""
    from pipeline.otio_timeline import OTIOTimeline

    otio_path = args.otio or os.path.join(args.output_dir, "war_economy_v9.otio")
    otio_tl = OTIOTimeline(otio_path)
    otio_tl.load()

    # FCPXML
    fcpxml_path = os.path.join(args.output_dir, "war_economy_v9.fcpxml")
    try:
        otio_tl.export_fcpxml(fcpxml_path)
        log.info(f"Exported FCPXML: {fcpxml_path}")
    except Exception as e:
        log.warning(f"FCPXML export failed: {e}")

    # Assembly JSON
    assembly_path = os.path.join(args.output_dir, "assembly_manifest.json")
    otio_tl.to_assembly_json(assembly_path)
    log.info(f"Exported assembly JSON: {assembly_path}")

    # Prompts JSON (derivative from OTIO)
    prompts_path = os.path.join(args.output_dir, "video_prompts.json")
    exported = otio_tl.export_prompts_json(prompts_path)
    log.info(f"Exported prompts JSON: {prompts_path} ({len(exported)} prompts)")

    # Status JSON
    status = otio_tl.get_timeline_status()
    status_path = os.path.join(args.output_dir, "timeline_status.json")
    with open(status_path, "w") as f:
        json.dump(status, f, indent=2)
    log.info(f"Exported status JSON: {status_path}")


def main():
    parser = argparse.ArgumentParser(
        description="War Economy — OTIO-Centric Production Pipeline V9",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Phases:
  audio     Generate narration audio → OTIO audio track
  prompts   Generate LTX-2.3 video prompts → OTIO metadata (requires audio)
  video     Generate video clips with LTX-2.3 → OTIO video track (requires prompts)
  assemble  Render final video from OTIO timeline (warns if video incomplete)
  status    Print OTIO timeline status
  validate  Print pipeline state per scene (audio/prompts/video)
  export    Export OTIO to FCPXML/EDL/JSON
  all       Run audio → prompts → video → assemble → export
        """
    )

    parser.add_argument("--phase", default="all",
                        choices=["audio", "prompts", "video", "assemble", "status", "validate", "export", "all"],
                        help="Pipeline phase to run")
    parser.add_argument("--script", help="Path to narration_script.json")
    parser.add_argument("--otio", help="Path to .otio timeline file")
    parser.add_argument("--manifest", help="Path to video prompts JSON")
    parser.add_argument("--output-dir", default="./production_v9", help="Output directory")
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
    print(f"WAR ECONOMY — OTIO-Centric Production Pipeline V9")
    print(f"{'='*60}")
    print(f"Phase: {args.phase}")
    print(f"Output: {args.output_dir}")
    print(f"{'='*60}\n")

    if args.phase == "status":
        phase_status(args)
        return

    if args.phase == "validate":
        phase_validate(args)
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
