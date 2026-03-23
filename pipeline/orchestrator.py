#!/usr/bin/env python3
"""
Master Pipeline Orchestrator v9 — OTIO-Centric Audio-First Workflow
=====================================================================
Runs the complete documentary production pipeline with strict phase
validation gates enforcing audio-first flow.

  Phase 1: SCRIPT PARSE -> scene list
  Phase 2: AUDIO GENERATION -> narration WAV files -> OTIO audio track
  Phase 3: PROMPT GENERATION -> LTX-2.3 prompts -> OTIO metadata (+ JSON export)
  Phase 4: VIDEO GENERATION -> video clips -> OTIO video track + quality metadata
  Phase 5: ASSEMBLY -> read OTIO -> render final MP4
  Phase 6: EXPORT -> .otio + FCPXML + EDL
  VALIDATE: Print pipeline state for each scene

The .otio file is the SINGLE SOURCE OF TRUTH at every stage.

v9 changes:
  - Phase validation gates: prompts REFUSE without audio, video REFUSE without prompts
  - Prompts stored on OTIO timeline (not just JSON file)
  - JSON export is derivative (from OTIO metadata)
  - Video phase reads prompts from OTIO export
  - After video generation, quality metadata written to OTIO
  - New --phase validate command

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
    """
    Phase 3: Generate video prompts from OTIO audio timing.

    VALIDATION GATE: Audio track MUST be populated before prompts can be generated.
    Prompts are stored directly on the OTIO timeline as metadata.
    A JSON export is also written as a derivative for VM deployment.
    """
    from pipeline.otio_timeline import OTIOTimeline
    from pipeline.prompt_generator import generate_all_prompts

    otio_path = args.otio or os.path.join(args.output_dir, "war_economy_v9.otio")
    prompts_path = args.manifest or os.path.join(args.output_dir, "video_prompts.json")

    # Load OTIO timeline
    otio_tl = OTIOTimeline(otio_path)
    otio_tl.load()

    # VALIDATION GATE: audio must exist
    if not otio_tl.validate_audio_complete():
        log.error("VALIDATION FAILED: Audio track is empty. "
                  "Run --phase audio first. Audio-first workflow requires "
                  "narration to be generated before prompts.")
        sys.exit(1)

    log.info("Audio validation passed: narration track is populated")

    # Load narration script for text content
    with open(args.script) as f:
        narration_data = json.load(f)

    # Generate prompts (stores on OTIO timeline + returns list)
    all_prompts = generate_all_prompts(otio_tl, narration_data)

    # Export prompts to JSON (derivative from OTIO — for VM deployment)
    otio_tl.export_prompts_json(prompts_path)

    log.info(f"\nPrompt generation complete:")
    log.info(f"  Total prompts: {len(all_prompts)}")
    log.info(f"  Avg words: {sum(p['word_count'] for p in all_prompts) / max(1, len(all_prompts)):.0f}")
    log.info(f"  Stored on OTIO: {otio_path}")
    log.info(f"  Exported JSON: {prompts_path}")

    return all_prompts


def phase_video(args):
    """
    Phase 4: Generate video clips using LTX-2.3.

    VALIDATION GATE: Prompts MUST exist on OTIO before video can be generated.
    After generation, quality metadata is written back to OTIO.
    """
    from pipeline.otio_timeline import OTIOTimeline
    from pipeline.video_generator import VideoGenerator

    otio_path = args.otio or os.path.join(args.output_dir, "war_economy_v9.otio")
    prompts_path = args.manifest or os.path.join(args.output_dir, "video_prompts.json")
    clips_dir = os.path.join(args.output_dir, "clips")

    # Load OTIO and validate
    otio_tl = OTIOTimeline(otio_path)
    otio_tl.load()

    # VALIDATION GATE: prompts must exist
    if not otio_tl.validate_prompts_complete():
        log.error("VALIDATION FAILED: No prompts found on OTIO timeline. "
                  "Run --phase prompts first. Prompts must be generated "
                  "from audio timing before video can be created.")
        sys.exit(1)

    log.info("Prompt validation passed: prompts are stored on OTIO timeline")

    # Export prompts from OTIO to JSON for the video generator
    # (This ensures video gen always reads from OTIO as source of truth)
    if not os.path.exists(prompts_path) or not args.manifest:
        log.info(f"Exporting prompts from OTIO to {prompts_path}")
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
    """
    Phase 5: Assemble final video from OTIO timeline.

    VALIDATION GATE: Warns about incomplete scenes but does not refuse
    (partial assembly is useful for review).
    """
    from pipeline.otio_timeline import OTIOTimeline
    from pipeline.assembler import Assembler

    otio_path = args.otio or os.path.join(args.output_dir, "war_economy_v9.otio")
    clips_dir = os.path.join(args.output_dir, "clips")
    audio_dir = os.path.join(args.output_dir, "audio")

    # Load OTIO and check video completeness
    otio_tl = OTIOTimeline(otio_path)
    otio_tl.load()

    if not otio_tl.validate_video_complete():
        pipeline_state = otio_tl.get_pipeline_state()
        incomplete = [sn for sn, st in pipeline_state.items() if not st["video"]]
        log.warning(f"WARNING: Video is incomplete for scenes: {incomplete}")
        log.warning("Proceeding with partial assembly. Incomplete scenes will have black gaps.")

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
    print(f"\n{'Scene':>6} {'Narration':>10} {'Video':>8} {'Gaps':>8} {'Clips':>6} {'Status'}")
    print("-" * 55)
    for sn in sorted(status["scenes"].keys()):
        s = status["scenes"][sn]
        stat = "OK" if s["video_gaps_sec"] < 0.5 else f"GAP {s['video_gaps_sec']:.1f}s"
        print(f"{sn:6d} {s['narration_sec']:9.1f}s {s['video_sec']:7.1f}s "
              f"{s['video_gaps_sec']:7.1f}s {s['clips']:5d}  {stat}")


def phase_validate(args):
    """
    Print the pipeline state for each scene: audio, prompts, video status.
    Shows which phases are complete and what needs to run next.
    """
    from pipeline.otio_timeline import OTIOTimeline

    otio_path = args.otio or os.path.join(args.output_dir, "war_economy_v9.otio")
    otio_tl = OTIOTimeline(otio_path)
    otio_tl.load()

    pipeline_state = otio_tl.get_pipeline_state()

    print(f"\n{'='*60}")
    print(f"PIPELINE VALIDATION — {otio_tl.timeline.name}")
    print(f"{'='*60}")

    ok = "\u2713"
    fail = "\u2717"

    print(f"\n{'Scene':>6}  {'Audio':>7}  {'Prompts':>9}  {'Video':>7}  {'Next Step'}")
    print("-" * 60)

    all_complete = True
    for sn in sorted(pipeline_state.keys()):
        st = pipeline_state[sn]
        audio_s = ok if st["audio"] else fail
        prompts_s = ok if st["prompts"] else fail
        video_s = ok if st["video"] else fail

        if not st["audio"]:
            next_step = "Run --phase audio"
            all_complete = False
        elif not st["prompts"]:
            next_step = "Run --phase prompts"
            all_complete = False
        elif not st["video"]:
            next_step = "Run --phase video"
            all_complete = False
        else:
            next_step = "Ready for assembly"

        print(f"{sn:6d}  {audio_s:>7s}  {prompts_s:>9s}  {video_s:>7s}  {next_step}")

    print(f"\n{'='*60}")
    if all_complete:
        print("All scenes complete. Ready for --phase assemble.")
    else:
        incomplete_count = sum(1 for st in pipeline_state.values() if not all(st.values()))
        print(f"{incomplete_count} scene(s) have incomplete phases.")

    # Quality check
    regen_clips = otio_tl.get_clips_needing_regeneration()
    if regen_clips:
        print(f"\n{len(regen_clips)} clip(s) marked for regeneration:")
        for rc in regen_clips:
            print(f"  Scene {rc['scene']:2d} | {rc['clip_id']} | {rc['reason']}")

    print(f"{'='*60}")


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

    # Prompts JSON (from OTIO metadata)
    prompts_path = os.path.join(args.output_dir, "video_prompts.json")
    otio_tl.export_prompts_json(prompts_path)
    log.info(f"Exported prompts JSON (from OTIO): {prompts_path}")

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
  audio     Generate narration audio -> OTIO audio track
  prompts   Generate LTX-2.3 video prompts -> OTIO metadata (+ JSON export)
  video     Generate video clips with LTX-2.3 -> OTIO video track
  assemble  Render final video from OTIO timeline
  validate  Print pipeline state for each scene (audio/prompts/video)
  status    Print OTIO timeline status
  export    Export OTIO to FCPXML/EDL/JSON
  all       Run audio -> prompts -> video -> assemble -> export

Validation Gates:
  prompts phase REQUIRES audio track to be populated
  video phase REQUIRES prompts to be stored on OTIO
  assemble phase WARNS about incomplete video (but proceeds)
        """
    )

    parser.add_argument("--phase", default="all",
                        choices=["audio", "prompts", "video", "assemble",
                                 "validate", "status", "export", "all"],
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
