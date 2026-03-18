#!/usr/bin/env python3
"""
WAR ECONOMY — VM Runner
========================
Executes the generation scripts produced by pipeline.py on the Vast.ai GPU VM.
This actually runs LTX-2.3 and Qwen3-TTS, not just generates scripts.

Usage:
  python3 vm_runner.py --phase narration    # Run all narration TTS
  python3 vm_runner.py --phase video        # Run all video generation
  python3 vm_runner.py --phase assemble     # Run scene assembly
  python3 vm_runner.py --phase final        # Final concat + upload
  python3 vm_runner.py --phase all          # Everything
  python3 vm_runner.py --scene 5            # Only scene 5
  python3 vm_runner.py --act 3             # Only act 3
  python3 vm_runner.py --resume            # Skip already-completed
"""

import argparse
import json
import os
import subprocess
import sys
import time
import glob as glob_mod
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).parent
CHECKPOINT = BASE_DIR / "runner_checkpoint.json"

# ============================================================
# CHECKPOINT
# ============================================================

def load_cp():
    if CHECKPOINT.exists():
        return json.load(open(CHECKPOINT))
    return {"narration": {}, "video": {}, "assembly": {}, "final": False, "started": datetime.now().isoformat()}

def save_cp(cp):
    cp["last_updated"] = datetime.now().isoformat()
    json.dump(cp, open(CHECKPOINT, "w"), indent=2)

def run_cmd(cmd, desc="", timeout=1800):
    """Run a shell command with live output."""
    print(f"\n{'─'*50}")
    print(f"  {desc}")
    print(f"{'─'*50}")
    start = time.time()
    try:
        proc = subprocess.run(
            cmd, shell=True, timeout=timeout,
            capture_output=False  # Show live output
        )
        elapsed = time.time() - start
        if proc.returncode == 0:
            print(f"  ✓ Done in {elapsed:.1f}s")
            return True
        else:
            print(f"  ✗ FAILED (exit {proc.returncode}) after {elapsed:.1f}s")
            return False
    except subprocess.TimeoutExpired:
        print(f"  ✗ TIMEOUT after {timeout}s")
        return False

# ============================================================
# PHASE 1: NARRATION (Qwen3-TTS)
# ============================================================

def run_narration(scene_filter=None, resume=False):
    """Load Qwen3-TTS once, then generate all narration."""
    cp = load_cp()
    audio_dir = BASE_DIR / "audio"
    narr_dir = BASE_DIR / "narration_scripts"
    
    print("\n" + "=" * 60)
    print("PHASE 1: NARRATION — Qwen3-TTS")
    print("=" * 60)
    
    # Get all narration segment files
    seg_files = sorted(narr_dir.glob("scene_*_seg*.txt"))
    if scene_filter:
        seg_files = [f for f in seg_files if any(f"scene_{s:02d}" in f.name for s in scene_filter)]
    
    total = len(seg_files)
    done = 0
    failed = 0
    
    # Instead of loading model per-segment, we create a batch script
    # that loads the model once and processes all segments
    batch_script = f"""#!/usr/bin/env python3
import torch
import sys
import os
import json
import time
import soundfile as sf
from pathlib import Path

print("Loading Qwen3-TTS model...")
t0 = time.time()

from transformers import AutoModelForCausalLM, AutoTokenizer, AutoProcessor

model_path = "/workspace/models/Qwen3-TTS"
processor = AutoProcessor.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(
    model_path, 
    torch_dtype=torch.bfloat16, 
    device_map="auto",
    trust_remote_code=True
)
print(f"Model loaded in {{time.time()-t0:.1f}}s")

# Voice mapping
VOICES = {{
    "v1": "Chelsie",
    "v2": "Ethan",  
    "v3": "Maverick"
}}

segments = json.loads('{json.dumps([
    {"file": str(f), "name": f.stem, "voice": next((v for v in ["v1","v2","v3"] if v in f.stem), "v1")}
    for f in seg_files
])}')

completed = 0
for seg in segments:
    name = seg["name"]
    voice = VOICES.get(seg["voice"], "Chelsie")
    output = f"{audio_dir}/{{name}}.wav"
    
    if os.path.exists(output) and {str(resume).lower()}:
        print(f"  [skip] {{name}}")
        completed += 1
        continue
    
    text = Path(seg["file"]).read_text().strip()
    if not text:
        continue
    
    print(f"  [{{completed+1}}/{total}] {{name}} ({{len(text.split())}} words, {{voice}})")
    t1 = time.time()
    
    try:
        # Qwen3-TTS generation
        prompt = f"<|speaker|>{{voice}}<|text|>{{text}}<|endoftext|>"
        inputs = processor(text=prompt, return_tensors="pt").to(model.device)
        
        with torch.no_grad():
            outputs = model.generate(
                **inputs, 
                max_new_tokens=8192,
                do_sample=True,
                temperature=0.7
            )
        
        # Decode audio from tokens
        audio = processor.decode(outputs[0], skip_special_tokens=True)
        if hasattr(audio, 'numpy'):
            audio_np = audio.cpu().numpy()
        else:
            audio_np = audio
            
        sf.write(output, audio_np, 24000)
        print(f"    ✓ {{time.time()-t1:.1f}}s → {{output}}")
        completed += 1
    except Exception as e:
        print(f"    ✗ Error: {{e}}")

print(f"\\nNarration complete: {{completed}}/{total}")
"""
    
    batch_path = audio_dir / "run_all_narration.py"
    batch_path.write_text(batch_script)
    
    print(f"\nNarration batch script: {batch_path}")
    print(f"Segments to process: {total}")
    
    # Execute
    success = run_cmd(
        f"cd {BASE_DIR} && python3 {batch_path}",
        f"Generating {total} narration segments via Qwen3-TTS",
        timeout=total * 120  # ~2min per segment max
    )
    
    if success:
        cp["narration"]["status"] = "complete"
        cp["narration"]["count"] = total
    save_cp(cp)

# ============================================================
# PHASE 2: VIDEO (LTX-2.3)
# ============================================================

def run_video(scene_filter=None, resume=False):
    """Generate all video clips using LTX-2.3."""
    cp = load_cp()
    clips_dir = BASE_DIR / "clips"
    
    print("\n" + "=" * 60)
    print("PHASE 2: VIDEO GENERATION — LTX-2.3")
    print("=" * 60)
    
    # Load prompts
    with open(BASE_DIR / "all_video_prompts.json") as f:
        prompts = json.load(f)
    
    if scene_filter:
        prompts = [p for p in prompts if p["scene_number"] in scene_filter]
    
    total_clips = len(prompts)
    total_gens = sum(p["ltx_clips_needed"] for p in prompts)
    
    print(f"Clips to generate: {total_clips}")
    print(f"Total LTX-2.3 generations: {total_gens}")
    
    # Create a master video generation script that loads the model once
    batch_script = f"""#!/usr/bin/env python3
import torch
import json
import os
import time
import subprocess
from pathlib import Path

print("Loading LTX-Video 2.3...")
t0 = time.time()

from diffusers import LTXPipeline, LTXImageToVideoPipeline
from diffusers.utils import export_to_video
from PIL import Image

MODEL_PATH = "/workspace/models/ltx-video-2.3"

# Load text-to-video pipeline
t2v_pipe = LTXPipeline.from_pretrained(MODEL_PATH, torch_dtype=torch.bfloat16).to("cuda")
print(f"T2V pipeline loaded in {{time.time()-t0:.1f}}s")

# Will load i2v pipeline on demand (first frame-chain)
i2v_pipe = None

NEGATIVE = "blurry, low quality, text, watermark, letters, words, subtitles, logo, static, frozen, looping"
CLIPS_DIR = Path("{clips_dir}")

with open("{BASE_DIR / 'all_video_prompts.json'}") as f:
    all_prompts = json.load(f)

# Filter
scene_filter = {scene_filter if scene_filter else 'None'}
if scene_filter:
    all_prompts = [p for p in all_prompts if p["scene_number"] in scene_filter]

total = len(all_prompts)
completed = 0
failed = 0

for pidx, pdata in enumerate(all_prompts):
    clip_id = pdata["clip_id"]
    target_dur = pdata["target_duration_sec"]
    ltx_count = pdata["ltx_clips_needed"]
    prompt = pdata["prompt"]
    
    final_clip = CLIPS_DIR / f"{{clip_id}}.mp4"
    
    if final_clip.exists() and {str(resume).lower()}:
        print(f"  [skip] {{clip_id}}")
        completed += 1
        continue
    
    print(f"\\n[{{pidx+1}}/{{total}}] {{clip_id}} | {{target_dur}}s | {{ltx_count}} gens")
    clip_start = time.time()
    
    try:
        sub_clips = []
        for sub_idx in range(ltx_count):
            sub_path = CLIPS_DIR / f"{{clip_id}}_sub{{sub_idx:02d}}.mp4"
            
            if sub_idx == 0:
                # Text-to-video
                print(f"    T2V gen {{sub_idx+1}}/{{ltx_count}}...")
                video = t2v_pipe(
                    prompt=prompt,
                    negative_prompt=NEGATIVE,
                    width=1280,
                    height=720,
                    num_frames=121,
                    guidance_scale=3.5,
                    num_inference_steps=50,
                ).frames[0]
                export_to_video(video, str(sub_path), fps=24)
            else:
                # Image-to-video (frame chaining)
                if i2v_pipe is None:
                    print("    Loading I2V pipeline...")
                    i2v_pipe = LTXImageToVideoPipeline.from_pretrained(
                        MODEL_PATH, torch_dtype=torch.bfloat16
                    ).to("cuda")
                
                # Extract last frame
                prev_clip = sub_clips[-1]
                last_frame_path = CLIPS_DIR / f"{{clip_id}}_sub{{sub_idx-1:02d}}_lastframe.jpg"
                subprocess.run([
                    "ffmpeg", "-y", "-sseof", "-0.1", "-i", str(prev_clip),
                    "-frames:v", "1", "-q:v", "2", str(last_frame_path)
                ], capture_output=True)
                
                print(f"    I2V gen {{sub_idx+1}}/{{ltx_count}}...")
                image = Image.open(str(last_frame_path))
                video = i2v_pipe(
                    prompt=f"Camera continues moving, scene continues naturally. {{prompt}}",
                    image=image,
                    negative_prompt=NEGATIVE,
                    width=1280,
                    height=720,
                    num_frames=121,
                    guidance_scale=3.5,
                    num_inference_steps=50,
                ).frames[0]
                export_to_video(video, str(sub_path), fps=24)
            
            sub_clips.append(sub_path)
            print(f"    ✓ sub{{sub_idx:02d}} done")
        
        # Concatenate sub-clips if needed, then trim to target duration
        if ltx_count > 1:
            concat_txt = CLIPS_DIR / f"{{clip_id}}_concat.txt"
            with open(concat_txt, "w") as cf:
                for sc in sub_clips:
                    cf.write(f"file '{{sc}}'\\n")
            raw_clip = CLIPS_DIR / f"{{clip_id}}_raw.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", str(concat_txt), "-c", "copy", str(raw_clip)
            ], capture_output=True)
            subprocess.run([
                "ffmpeg", "-y", "-i", str(raw_clip),
                "-t", str(target_dur), "-c", "copy", str(final_clip)
            ], capture_output=True)
        else:
            subprocess.run([
                "ffmpeg", "-y", "-i", str(sub_clips[0]),
                "-t", str(target_dur), "-c", "copy", str(final_clip)
            ], capture_output=True)
        
        elapsed = time.time() - clip_start
        print(f"  ✓ {{clip_id}}.mp4 ({{target_dur}}s) in {{elapsed:.1f}}s")
        completed += 1
        
    except Exception as e:
        print(f"  ✗ {{clip_id}} FAILED: {{e}}")
        failed += 1

print(f"\\n{'='*50}")
print(f"Video generation complete: {{completed}}/{{total}} ({{failed}} failed)")
print(f"{'='*50}")
"""
    
    batch_path = clips_dir / "run_all_video.py"
    batch_path.write_text(batch_script)
    
    print(f"\nVideo batch script: {batch_path}")
    
    # Execute (very long — ~2-5 min per LTX generation)
    est_hours = total_gens * 3 / 60  # ~3 min per gen
    print(f"Estimated time: {est_hours:.1f} hours for {total_gens} generations")
    
    success = run_cmd(
        f"cd {BASE_DIR} && python3 {batch_path}",
        f"Generating {total_clips} clips ({total_gens} LTX-2.3 gens)",
        timeout=total_gens * 600  # 10 min per gen max
    )
    
    if success:
        cp["video"]["status"] = "complete"
        cp["video"]["count"] = total_clips
    save_cp(cp)

# ============================================================
# PHASE 3: SCENE ASSEMBLY
# ============================================================

def run_assembly(scene_filter=None):
    """Run all scene assembly scripts."""
    cp = load_cp()
    scenes_dir = BASE_DIR / "scenes"
    
    print("\n" + "=" * 60)
    print("PHASE 3: SCENE ASSEMBLY")
    print("=" * 60)
    
    # First generate assembly scripts via pipeline
    run_cmd(
        f"cd {BASE_DIR} && python3 pipeline.py --phase assemble --dry-run",
        "Generating assembly scripts"
    )
    
    # Then execute each scene's assembly script
    scripts = sorted(scenes_dir.glob("scene_*_assemble.sh"))
    if scene_filter:
        scripts = [s for s in scripts if any(f"scene_{n:02d}" in s.name for n in scene_filter)]
    
    for idx, script in enumerate(scripts):
        scene_name = script.stem.replace("_assemble", "")
        success = run_cmd(
            f"bash {script}",
            f"[{idx+1}/{len(scripts)}] Assembling {scene_name}",
            timeout=300
        )
        cp.setdefault("assembly", {})[scene_name] = "complete" if success else "failed"
        save_cp(cp)

# ============================================================
# PHASE 4: FINAL
# ============================================================

def run_final():
    """Assemble final documentary and upload."""
    cp = load_cp()
    
    print("\n" + "=" * 60)
    print("PHASE 4: FINAL ASSEMBLY + UPLOAD")
    print("=" * 60)
    
    # Generate final scripts
    run_cmd(
        f"cd {BASE_DIR} && python3 pipeline.py --phase final --dry-run",
        "Generating final assembly scripts"
    )
    
    # Run final assembly
    final_script = BASE_DIR / "final" / "assemble_final.sh"
    if final_script.exists():
        run_cmd(f"bash {final_script}", "Assembling final documentary", timeout=600)
    
    # Run upload
    upload_script = BASE_DIR / "final" / "upload.sh"
    if upload_script.exists():
        run_cmd(f"bash {upload_script}", "Uploading to B2 + Frame.io", timeout=1200)
    
    cp["final"] = True
    save_cp(cp)

# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="WAR ECONOMY VM Runner")
    parser.add_argument("--phase", choices=["narration", "video", "assemble", "final", "all"],
                       default="all")
    parser.add_argument("--scene", type=int)
    parser.add_argument("--act", type=int)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    
    act_defs = {1:(1,4), 2:(5,7), 3:(8,11), 4:(12,14), 5:(15,17), 6:(18,21), 7:(22,26)}
    scene_filter = None
    if args.scene:
        scene_filter = {args.scene}
    elif args.act:
        s, e = act_defs[args.act]
        scene_filter = set(range(s, e + 1))
    
    print(f"{'='*60}")
    print(f"WAR ECONOMY — VM Runner")
    print(f"Phase: {args.phase} | Resume: {args.resume}")
    print(f"Scene filter: {scene_filter or 'ALL'}")
    print(f"{'='*60}")
    
    if args.phase in ("narration", "all"):
        run_narration(scene_filter, args.resume)
    
    if args.phase in ("video", "all"):
        run_video(scene_filter, args.resume)
    
    if args.phase in ("assemble", "all"):
        run_assembly(scene_filter)
    
    if args.phase in ("final", "all"):
        run_final()
    
    print(f"\n{'='*60}")
    print(f"VM Runner complete — {args.phase}")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
