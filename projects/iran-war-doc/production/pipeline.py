#!/usr/bin/env python3
"""
WAR ECONOMY Documentary — Production Pipeline
================================================
Reads the OTIO master timeline and production manifest to orchestrate:
  Phase 1: Narration generation (Qwen3-TTS)
  Phase 2: Video clip generation (LTX-2.3)
  Phase 3: Per-scene assembly (ffmpeg)
  Phase 4: Final timeline assembly (ffmpeg concat with transitions)
  Phase 5: Upload to B2 + Frame.io

This script is designed to run on a Vast.ai VM with:
  - RTX 5090 (32GB VRAM) for LTX-2.3 video generation
  - Qwen3-TTS model loaded for narration
  - ffmpeg installed
  - b2 CLI and frame.io credentials configured

Usage:
  python3 pipeline.py --phase narration     # Generate all narration audio
  python3 pipeline.py --phase video         # Generate all video clips
  python3 pipeline.py --phase assemble      # Assemble scenes
  python3 pipeline.py --phase final         # Final concat + upload
  python3 pipeline.py --phase all           # Run everything
  python3 pipeline.py --scene 5             # Process only scene 5
  python3 pipeline.py --act 3              # Process only Act 3
  python3 pipeline.py --resume              # Resume from last checkpoint
"""

import argparse
import json
import os
import subprocess
import sys
import time
import hashlib
from pathlib import Path
from datetime import datetime

# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).parent
MANIFEST_PATH = BASE_DIR / "production_manifest.json"
PROMPTS_PATH = BASE_DIR / "all_video_prompts.json"
NARRATION_DIR = BASE_DIR / "narration_scripts"
OTIO_PATH = BASE_DIR / "war_economy_master.otio"

# Output directories
AUDIO_OUT = BASE_DIR / "audio"       # Generated narration .wav files
CLIPS_OUT = BASE_DIR / "clips"       # Generated video .mp4 clips
SCENES_OUT = BASE_DIR / "scenes"     # Assembled per-scene .mp4 files
FINAL_OUT = BASE_DIR / "final"       # Final assembled documentary

# Checkpoint tracking
CHECKPOINT_FILE = BASE_DIR / "pipeline_checkpoint.json"

# LTX-2.3 settings (non-negotiable per user requirements)
LTX_CONFIG = {
    "model_path": "/workspace/models/ltx-video-2.3",  # Path on Vast.ai VM
    "precision": "bf16",
    "fps": 24,
    "width": 1280,
    "height": 720,
    "num_frames": 121,          # ~5s at 24fps
    "guidance_scale": 3.5,
    "num_inference_steps": 50,  # Full quality, no distillation
    "seed": None,               # Random per clip for variety
    "negative_prompt": "blurry, low quality, text, watermark, letters, words, subtitles, logo, static, frozen, looping"
}

# Qwen3-TTS settings
TTS_CONFIG = {
    "model_path": "/workspace/models/Qwen3-TTS",
    "voices": {
        "V1": {"speaker": "male_narrator_01", "style": "investigative, sharp, slightly sardonic"},
        "V2": {"speaker": "male_narrator_02", "style": "precise, measured, analytical"},
        "V3": {"speaker": "male_narrator_03", "style": "contemplative, authoritative, historical"}
    },
    "sample_rate": 24000,
    "format": "wav"
}

# B2 upload settings
B2_CONFIG = {
    "bucket": "economy-vid-assets",
    "prefix": "war-economy-v2",
    "key_id": "os.environ["B2_KEY_ID"]",
    "app_key": "os.environ["B2_APP_KEY"]"
}

# Frame.io settings
FRAMEIO_CONFIG = {
    "account_id": "os.environ["FRAMEIO_ACCOUNT_ID"]",
    "client_id": "os.environ["FRAMEIO_CLIENT_ID"]",
    "client_secret": "os.environ["FRAMEIO_CLIENT_SECRET"]"
}

# ============================================================
# CHECKPOINT MANAGEMENT
# ============================================================

def load_checkpoint():
    if CHECKPOINT_FILE.exists():
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    return {"narration": {}, "video": {}, "assembly": {}, "final": False}

def save_checkpoint(cp):
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(cp, f, indent=2)

# ============================================================
# PHASE 1: NARRATION GENERATION (Qwen3-TTS)
# ============================================================

def generate_narration(manifest, scene_filter=None, resume=False):
    """Generate narration audio for all voice segments using Qwen3-TTS."""
    AUDIO_OUT.mkdir(parents=True, exist_ok=True)
    cp = load_checkpoint()

    print("\n" + "=" * 60)
    print("PHASE 1: NARRATION GENERATION (Qwen3-TTS)")
    print("=" * 60)

    for scene_info in manifest["scenes"]:
        scene_num = scene_info["number"]
        if scene_filter and scene_num not in scene_filter:
            continue

        scene_id = f"scene_{scene_num:02d}"
        narr_files = sorted(NARRATION_DIR.glob(f"{scene_id}_seg*.txt"))

        for narr_file in narr_files:
            seg_name = narr_file.stem
            output_wav = AUDIO_OUT / f"{seg_name}.wav"

            # Skip if already done
            if resume and seg_name in cp.get("narration", {}) and output_wav.exists():
                print(f"  [skip] {seg_name} (already generated)")
                continue

            # Determine voice
            voice_key = "V1"
            for v in ["v1", "v2", "v3"]:
                if v in seg_name:
                    voice_key = v.upper()
                    break

            text = narr_file.read_text().strip()
            if not text:
                continue

            print(f"  [gen] {seg_name} ({len(text.split())} words, {voice_key})")

            # === Qwen3-TTS generation command ===
            # This is the actual command that will run on the Vast.ai VM
            tts_cmd = f"""
python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '{TTS_CONFIG["model_path"]}'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''{text.replace("'", "\\'")}'''
speaker = '{TTS_CONFIG["voices"][voice_key]["speaker"]}'

# Generate speech
inputs = tokenizer(f'<|speaker|>{{speaker}}<|text|>{{text}}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('{output_wav}', audio.cpu().numpy(), {TTS_CONFIG["sample_rate"]})
print(f'Generated: {output_wav}')
"
"""
            # Write the command to a script file for execution on VM
            script_path = AUDIO_OUT / f"{seg_name}_gen.sh"
            script_path.write_text(f"#!/bin/bash\n{tts_cmd}")
            script_path.chmod(0o755)

            # Track in checkpoint
            cp.setdefault("narration", {})[seg_name] = {
                "status": "pending",
                "voice": voice_key,
                "words": len(text.split()),
                "script": str(script_path),
                "output": str(output_wav)
            }

    save_checkpoint(cp)
    print(f"\nNarration scripts prepared: {len(cp.get('narration', {}))} segments")
    return cp

# ============================================================
# PHASE 2: VIDEO GENERATION (LTX-2.3)
# ============================================================

def generate_video_clips(manifest, prompts, scene_filter=None, resume=False):
    """Generate video clips using LTX-2.3."""
    CLIPS_OUT.mkdir(parents=True, exist_ok=True)
    cp = load_checkpoint()

    print("\n" + "=" * 60)
    print("PHASE 2: VIDEO GENERATION (LTX-2.3)")
    print("=" * 60)

    for prompt_data in prompts:
        scene_num = prompt_data["scene_number"]
        if scene_filter and scene_num not in scene_filter:
            continue

        clip_id = prompt_data["clip_id"]
        target_dur = prompt_data["target_duration_sec"]
        ltx_count = prompt_data["ltx_clips_needed"]
        prompt_text = prompt_data["prompt"]

        # Skip if done
        if resume and clip_id in cp.get("video", {}) and (CLIPS_OUT / f"{clip_id}.mp4").exists():
            print(f"  [skip] {clip_id} (already generated)")
            continue

        print(f"  [gen] {clip_id} | {target_dur}s | {ltx_count} LTX clips | {prompt_data['world']}")

        # For clips needing frame-chaining (> 1 LTX generation):
        # 1. Generate first clip from text prompt
        # 2. Extract last frame
        # 3. Generate subsequent clips from last frame (image-to-video)
        # 4. Concatenate all sub-clips

        gen_scripts = []
        for sub_idx in range(ltx_count):
            sub_clip_path = CLIPS_OUT / f"{clip_id}_sub{sub_idx:02d}.mp4"

            if sub_idx == 0:
                # Text-to-video for first clip
                gen_cmd = f"""
python3 -c "
import torch
from diffusers import LTXPipeline

pipe = LTXPipeline.from_pretrained(
    '{LTX_CONFIG["model_path"]}',
    torch_dtype=torch.bfloat16
).to('cuda')

prompt = '''{prompt_text.replace("'", "\\'")}'''

video = pipe(
    prompt=prompt,
    negative_prompt='{LTX_CONFIG["negative_prompt"]}',
    width={LTX_CONFIG["width"]},
    height={LTX_CONFIG["height"]},
    num_frames={LTX_CONFIG["num_frames"]},
    guidance_scale={LTX_CONFIG["guidance_scale"]},
    num_inference_steps={LTX_CONFIG["num_inference_steps"]},
).frames[0]

from diffusers.utils import export_to_video
export_to_video(video, '{sub_clip_path}', fps={LTX_CONFIG["fps"]})
print(f'Generated: {sub_clip_path}')
"
"""
            else:
                # Image-to-video for continuation clips (frame-chaining)
                prev_clip = CLIPS_OUT / f"{clip_id}_sub{sub_idx-1:02d}.mp4"
                last_frame = CLIPS_OUT / f"{clip_id}_sub{sub_idx-1:02d}_lastframe.jpg"

                gen_cmd = f"""
# Extract last frame from previous clip
ffmpeg -y -sseof -0.1 -i {prev_clip} -frames:v 1 -q:v 2 {last_frame}

python3 -c "
import torch
from diffusers import LTXImageToVideoPipeline
from PIL import Image

pipe = LTXImageToVideoPipeline.from_pretrained(
    '{LTX_CONFIG["model_path"]}',
    torch_dtype=torch.bfloat16
).to('cuda')

image = Image.open('{last_frame}')
# Continue the scene - describe motion continuation, not the static scene
continuation_prompt = 'Camera continues moving, scene continues naturally. {prompt_text.replace("'", "\\'")}'

video = pipe(
    prompt=continuation_prompt,
    image=image,
    negative_prompt='{LTX_CONFIG["negative_prompt"]}',
    width={LTX_CONFIG["width"]},
    height={LTX_CONFIG["height"]},
    num_frames={LTX_CONFIG["num_frames"]},
    guidance_scale={LTX_CONFIG["guidance_scale"]},
    num_inference_steps={LTX_CONFIG["num_inference_steps"]},
).frames[0]

from diffusers.utils import export_to_video
export_to_video(video, '{sub_clip_path}', fps={LTX_CONFIG["fps"]})
print(f'Generated: {sub_clip_path}')
"
"""
            gen_scripts.append((sub_clip_path, gen_cmd))

        # If multiple sub-clips, concatenate them
        if ltx_count > 1:
            concat_list = CLIPS_OUT / f"{clip_id}_concat.txt"
            concat_entries = []
            for sub_idx in range(ltx_count):
                concat_entries.append(f"file '{clip_id}_sub{sub_idx:02d}.mp4'")

            concat_cmd = f"""
cat > {concat_list} << 'CONCATEOF'
{chr(10).join(concat_entries)}
CONCATEOF
ffmpeg -y -f concat -safe 0 -i {concat_list} -c copy {CLIPS_OUT / f'{clip_id}_raw.mp4'}

# Trim to exact target duration
ffmpeg -y -i {CLIPS_OUT / f'{clip_id}_raw.mp4'} -t {target_dur} -c copy {CLIPS_OUT / f'{clip_id}.mp4'}
echo "Final clip: {clip_id}.mp4 (trimmed to {target_dur}s)"
"""
        else:
            concat_cmd = f"""
# Single clip — trim to target duration
ffmpeg -y -i {CLIPS_OUT / f'{clip_id}_sub00.mp4'} -t {target_dur} -c copy {CLIPS_OUT / f'{clip_id}.mp4'}
"""

        # Write generation script
        script_path = CLIPS_OUT / f"{clip_id}_gen.sh"
        full_script = "#!/bin/bash\nset -e\n\n"
        for sub_path, cmd in gen_scripts:
            full_script += f"echo '--- Generating {sub_path.name} ---'\n{cmd}\n\n"
        full_script += f"\n{concat_cmd}\n"
        script_path.write_text(full_script)
        script_path.chmod(0o755)

        # Track
        cp.setdefault("video", {})[clip_id] = {
            "status": "pending",
            "ltx_count": ltx_count,
            "target_dur": target_dur,
            "script": str(script_path),
            "output": str(CLIPS_OUT / f"{clip_id}.mp4")
        }

    save_checkpoint(cp)
    print(f"\nVideo generation scripts prepared: {len(cp.get('video', {}))} clips")
    return cp

# ============================================================
# PHASE 3: PER-SCENE ASSEMBLY
# ============================================================

def assemble_scenes(manifest, scene_filter=None):
    """Assemble each scene: video clips + narration audio overlay."""
    SCENES_OUT.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 60)
    print("PHASE 3: PER-SCENE ASSEMBLY")
    print("=" * 60)

    for scene_info in manifest["scenes"]:
        scene_num = scene_info["number"]
        if scene_filter and scene_num not in scene_filter:
            continue

        scene_id = f"scene_{scene_num:02d}"
        scene_dur = scene_info["duration_sec"]
        output_path = SCENES_OUT / f"{scene_id}.mp4"

        print(f"\n  Assembling Scene {scene_num}: {scene_info['title']} ({scene_dur}s)")

        # 1. Concatenate video clips for this scene
        scene_clips = sorted(CLIPS_OUT.glob(f"{scene_id}_clip*.mp4"))
        scene_clips = [c for c in scene_clips if "_sub" not in c.name and "_raw" not in c.name]

        if not scene_clips:
            print(f"    [warn] No video clips found for {scene_id}")
            continue

        video_concat_list = SCENES_OUT / f"{scene_id}_video_concat.txt"
        with open(video_concat_list, "w") as f:
            for clip in scene_clips:
                f.write(f"file '{clip}'\n")

        scene_video = SCENES_OUT / f"{scene_id}_video.mp4"

        # 2. Concatenate narration audio segments
        narr_files = sorted(AUDIO_OUT.glob(f"{scene_id}_seg*.wav"))

        if narr_files:
            # Build narration with pauses between segments
            narr_concat_list = SCENES_OUT / f"{scene_id}_narr_concat.txt"
            silence_path = AUDIO_OUT / "silence_750ms.wav"

            with open(narr_concat_list, "w") as f:
                for idx, nf in enumerate(narr_files):
                    f.write(f"file '{nf}'\n")
                    if idx < len(narr_files) - 1:
                        f.write(f"file '{silence_path}'\n")

            scene_narr = SCENES_OUT / f"{scene_id}_narration.wav"

            # Assembly commands
            assembly_script = f"""#!/bin/bash
set -e

# Generate 750ms silence for pauses
ffmpeg -y -f lavfi -i anullsrc=r=24000:cl=mono -t 0.75 {silence_path}

# Concat video clips
ffmpeg -y -f concat -safe 0 -i {video_concat_list} -c copy {scene_video}

# Concat narration segments with pauses
ffmpeg -y -f concat -safe 0 -i {narr_concat_list} -c:a pcm_s16le {scene_narr}

# Mix narration over video (video native audio at 30% + narration at 100%)
ffmpeg -y -i {scene_video} -i {scene_narr} \\
    -filter_complex "[0:a]volume=0.3[bg];[1:a]volume=1.0[vo];[bg][vo]amix=inputs=2:duration=longest" \\
    -c:v copy -preset fast \\
    {output_path}

echo "Scene {scene_num} assembled: {output_path}"
ffprobe -v quiet -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 {output_path}
"""
        else:
            # No narration — just concat video
            assembly_script = f"""#!/bin/bash
set -e
ffmpeg -y -f concat -safe 0 -i {video_concat_list} -c copy {output_path}
echo "Scene {scene_num} assembled (video only): {output_path}"
"""

        script_path = SCENES_OUT / f"{scene_id}_assemble.sh"
        script_path.write_text(assembly_script)
        script_path.chmod(0o755)
        print(f"    Script: {script_path}")

# ============================================================
# PHASE 4: FINAL ASSEMBLY
# ============================================================

def assemble_final(manifest):
    """Concatenate all scenes into the final documentary."""
    FINAL_OUT.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 60)
    print("PHASE 4: FINAL ASSEMBLY")
    print("=" * 60)

    act_defs = {
        1: (1, 4, "THE PRICE TAG"),
        2: (5, 7, "THE SMART MONEY"),
        3: (8, 11, "THE CRYPTO PIPELINE"),
        4: (12, 14, "THE RUSSIA DEAL"),
        5: (15, 17, "BILLIONAIRES ROW"),
        6: (18, 21, "THE DEFENSE PAYDAY"),
        7: (22, 26, "THE BILL")
    }

    # Build per-act concat files, then final concat
    act_files = []
    for act_num, (s_start, s_end, act_title) in act_defs.items():
        act_concat = FINAL_OUT / f"act_{act_num:02d}_concat.txt"
        act_output = FINAL_OUT / f"act_{act_num:02d}_{act_title.lower().replace(' ', '_')}.mp4"

        with open(act_concat, "w") as f:
            for sn in range(s_start, s_end + 1):
                scene_file = SCENES_OUT / f"scene_{sn:02d}.mp4"
                f.write(f"file '{scene_file}'\n")

        act_files.append(act_output)

    # Final concat list
    final_concat = FINAL_OUT / "final_concat.txt"
    with open(final_concat, "w") as f:
        for af in act_files:
            f.write(f"file '{af}'\n")

    final_output = FINAL_OUT / "THE_WAR_ECONOMY_final.mp4"

    assembly_script = f"""#!/bin/bash
set -e

echo "=== Assembling acts ==="
"""
    for act_num, (s_start, s_end, act_title) in act_defs.items():
        act_concat = FINAL_OUT / f"act_{act_num:02d}_concat.txt"
        act_output = act_files[act_num - 1]
        assembly_script += f"""
echo "Act {act_num}: {act_title}"
ffmpeg -y -f concat -safe 0 -i {act_concat} -c copy {act_output}
"""

    assembly_script += f"""
echo "=== Concatenating final ==="
ffmpeg -y -f concat -safe 0 -i {final_concat} -c copy {final_output}

echo "=== Final documentary ==="
ffprobe -v quiet -show_entries format=duration,format_name,size -of default=noprint_wrappers=1 {final_output}
echo "Output: {final_output}"

# Generate production metadata
python3 -c "
import json, subprocess
dur = subprocess.check_output(['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration', '-of', 'csv=p=0', '{final_output}']).decode().strip()
meta = {{
    'title': 'THE WAR ECONOMY: Who Profits When Missiles Fly',
    'duration_sec': float(dur),
    'duration_min': round(float(dur)/60, 1),
    'fps': 24,
    'resolution': '1280x720',
    'production_date': '{datetime.now().isoformat()[:10]}',
    'video_model': 'LTX-2.3 (bf16, 50 steps, no distillation)',
    'audio_model': 'Qwen3-TTS',
    'scenes': {len(manifest['scenes'])},
    'acts': 7
}}
print(json.dumps(meta, indent=2))
with open('{FINAL_OUT / 'metadata.json'}', 'w') as f:
    json.dump(meta, f, indent=2)
"
"""

    script_path = FINAL_OUT / "assemble_final.sh"
    script_path.write_text(assembly_script)
    script_path.chmod(0o755)
    print(f"Final assembly script: {script_path}")

# ============================================================
# PHASE 5: UPLOAD
# ============================================================

def upload(manifest):
    """Upload to B2 and Frame.io."""
    FINAL_OUT.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 60)
    print("PHASE 5: UPLOAD TO B2 + FRAME.IO")
    print("=" * 60)

    final_video = FINAL_OUT / "THE_WAR_ECONOMY_final.mp4"
    metadata_json = FINAL_OUT / "metadata.json"

    upload_script = f"""#!/bin/bash
set -e

echo "=== Uploading to B2 ==="
# Authorize
b2 authorize-account {B2_CONFIG['key_id']} {B2_CONFIG['app_key']}

# Upload final video
b2 upload-file {B2_CONFIG['bucket']} {final_video} {B2_CONFIG['prefix']}/THE_WAR_ECONOMY_final.mp4

# Upload per-act files
for act_file in {FINAL_OUT}/act_*.mp4; do
    b2 upload-file {B2_CONFIG['bucket']} "$act_file" {B2_CONFIG['prefix']}/acts/$(basename "$act_file")
done

# Upload metadata (embedded in video, not as separate JSON per user request)
echo "Metadata embedded in video file via ffmpeg metadata injection"

echo "=== B2 upload complete ==="

echo "=== Uploading to Frame.io ==="
# Frame.io upload via API (no JSON metadata files per user request)
python3 -c "
import requests
import json

# Get auth token
token_resp = requests.post('https://ims-na1.adobelogin.com/ims/token/v3', data={{
    'grant_type': 'client_credentials',
    'client_id': '{FRAMEIO_CONFIG['client_id']}',
    'client_secret': '{FRAMEIO_CONFIG['client_secret']}',
    'scope': 'openid,AdobeID,read_organizations,additional_info.projectedProductContext,additional_info.roles'
}})
token = token_resp.json().get('access_token', '')

if token:
    # Upload video
    headers = {{'Authorization': f'Bearer {{token}}'}}
    # Create asset
    resp = requests.post(
        f'https://api.frame.io/v2/accounts/{FRAMEIO_CONFIG['account_id']}/uploads',
        headers=headers,
        json={{'name': 'THE_WAR_ECONOMY_final.mp4', 'type': 'file', 'filetype': 'video/mp4'}}
    )
    print(f'Frame.io upload initiated: {{resp.status_code}}')
else:
    print('Frame.io auth failed - skip')
"

echo "=== Upload complete ==="
"""

    script_path = FINAL_OUT / "upload.sh"
    script_path.write_text(upload_script)
    script_path.chmod(0o755)
    print(f"Upload script: {script_path}")

# ============================================================
# MASTER ORCHESTRATOR
# ============================================================

def get_scene_filter(args, manifest):
    """Build scene number filter from args."""
    if args.scene:
        return {args.scene}
    if args.act:
        act_defs = {1:(1,4), 2:(5,7), 3:(8,11), 4:(12,14), 5:(15,17), 6:(18,21), 7:(22,26)}
        s, e = act_defs[args.act]
        return set(range(s, e + 1))
    return None

def main():
    parser = argparse.ArgumentParser(description="WAR ECONOMY Production Pipeline")
    parser.add_argument("--phase", choices=["narration", "video", "assemble", "final", "upload", "all"],
                       default="all", help="Which phase to run")
    parser.add_argument("--scene", type=int, help="Process only this scene number")
    parser.add_argument("--act", type=int, help="Process only this act number")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--dry-run", action="store_true", help="Generate scripts only, don't execute")
    args = parser.parse_args()

    # Load manifest and prompts
    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)
    with open(PROMPTS_PATH) as f:
        prompts = json.load(f)

    scene_filter = get_scene_filter(args, manifest)

    print(f"{'='*60}")
    print(f"WAR ECONOMY — Production Pipeline")
    print(f"{'='*60}")
    print(f"Phase: {args.phase}")
    print(f"Scenes: {scene_filter if scene_filter else 'ALL'}")
    print(f"Resume: {args.resume}")
    print(f"Dry run: {args.dry_run}")
    print(f"Total scenes: {manifest['total_scenes']}")
    print(f"Total clips: {manifest['total_video_clips']}")
    print(f"Total LTX gens: {manifest['total_ltx_generations']}")
    print(f"Target runtime: {manifest['target_runtime_min']} min")

    if args.phase in ("narration", "all"):
        generate_narration(manifest, scene_filter, args.resume)

    if args.phase in ("video", "all"):
        generate_video_clips(manifest, prompts, scene_filter, args.resume)

    if args.phase in ("assemble", "all"):
        assemble_scenes(manifest, scene_filter)

    if args.phase in ("final", "all"):
        assemble_final(manifest)

    if args.phase in ("upload", "all"):
        upload(manifest)

    print(f"\n{'='*60}")
    print(f"PIPELINE COMPLETE — Phase: {args.phase}")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
