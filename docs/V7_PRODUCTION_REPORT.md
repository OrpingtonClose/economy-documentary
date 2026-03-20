# V7 Production Report — War Economy Documentary (Full Coverage)

**Date:** 2026-03-20
**Duration:** 76.2 minutes (4,571 seconds)
**Resolution:** 768×512, 24fps H.264
**Scenes:** 26
**Total clips:** 912 (167 original + 268 fill v1 + 477 fill v2)
**Final file:** `v7_war_economy/FINAL_war_economy_v2.mp4` on B2

---

## Summary

V7 is the third and final generation round for the "War Economy — The Real Cost" documentary. The original v5/v6 pipeline produced 167 clips covering only ~48% of the 76.2-minute narration. Two additional fill-clip rounds (268 + 494 clips) were needed to achieve full video coverage across all 26 scenes. The final assembly uses 912 unique clips with no looping or repetition.

---

## Generation Rounds

### Round 1 — Original 167 clips
- **VMs:** 10× mixed (A100 80GB, H100 80GB)
- **Script:** `a100_generate.py`
- **Duration clips:** 8–20s each (variable)
- **Total video:** ~25 min → 48% coverage
- **Upload:** Inline to B2 during generation

### Round 2 — Fill v1: 268 clips
- **VMs:** 10× (H100, some A100)
- **Script:** `a100_generate_v2.py` with fill manifest from `generate_fill_clips.py`
- **Duration clips:** 5.04s each (121 frames @ 24fps)
- **Coverage after:** ~64%
- **Issues:** Several VMs hit `ltx_core` pip install errors (uv-build RuntimeError)

### Round 3 — Fill v2: 494 clips (477 completed)
- **VMs:** 8× H100 80GB
- **Script:** `a100_generate_v2.py` with per-VM manifests (`fill_v2_vm_[a-h].json`)
- **Duration clips:** 5.04s each
- **LTX config:** 768×512, 121 frames, 30 inference steps, cfg=3.0, stg=1.0/block28
- **Coverage after:** 153.5% (more video than narration for every scene)
- **17 clips lost:** Vast.ai credit exhausted ($0) before VM-f finished its last batch
- **Missing clips:** `scene_17_fill17`, `scene_19_fill33`, `scene_20_fill07–fill19` (13), `scene_23_fill28`, `scene_25_fill16`

---

## LTX-2.3 Setup on Vast.ai — Critical Path

### The `ltx_core` Blocker

The LTX-2 repository (`Lightricks/LTX-2`) restructured its packages into `packages/ltx-core/` and `packages/ltx-pipelines/`. Running `pip install -e packages/ltx-core` triggers a `uv-build` RuntimeError on many Vast.ai images because the build backend fails to resolve internal dependencies.

**Fix (PYTHONPATH injection instead of pip install):**
```bash
export PYTHONPATH="/workspace/LTX-2/packages/ltx-core/src:/workspace/LTX-2/packages/ltx-pipelines/src"
```

**Additional dependencies still needed after PYTHONPATH fix:**
```bash
pip install scipy einops torchaudio av sentencepiece protobuf
```

### HuggingFace Model Name
The correct repo is `Lightricks/LTX-2.3` (not `Lightricks/LTX-Video` which is the old v0.9).

### VM Bootstrap Sequence (proven working)
```bash
# 1. Clone repo
git clone https://github.com/Lightricks/LTX-2.git /workspace/LTX-2

# 2. Install deps (NOT the packages themselves)
pip install scipy einops torchaudio av sentencepiece protobuf diffusers transformers accelerate safetensors

# 3. PYTHONPATH instead of pip install
export PYTHONPATH="/workspace/LTX-2/packages/ltx-core/src:/workspace/LTX-2/packages/ltx-pipelines/src"

# 4. Download model
huggingface-cli download Lightricks/LTX-2.3 --local-dir /workspace/ltx23_model

# 5. Verify
python3 -c "from ltx_core import LTXVideoPipeline; print('OK')"
```

### Cross-VM Package Transfer
When new VMs have a broken pip environment, copy the working packages from a healthy VM:
```bash
# On working VM
cd /workspace/LTX-2 && tar czf /tmp/ltx_packages.tar.gz packages/
scp /tmp/ltx_packages.tar.gz newvm:/workspace/LTX-2/

# On new VM
cd /workspace/LTX-2 && tar xzf ltx_packages.tar.gz
```

---

## Generation Script Details

### `a100_generate_v2.py`
The workhorse script for rounds 2 and 3. Key features:

- **Inline B2 upload:** Each clip uploads immediately after generation (not batched)
- **Checkpoint/resume:** Tracks completed clips in a JSON tracker file
- **Metadata embedding:** ffmpeg writes prompt, scene info, and LTX params into MP4 metadata
- **Error recovery:** Retries failed generations up to 3 times
- **Memory management:** Explicit `torch.cuda.empty_cache()` between clips

### Generation Parameters (immutable — do NOT change)
```python
width = 768
height = 512
num_frames = 121          # = 5.04 seconds at 24fps
num_inference_steps = 30
guidance_scale = 3.0
stg_scale = 1.0
stg_applied_block = 28    # default STG block
seed = -1                 # random per clip
```

### Important Constraints
- **No FP8, no distillation, no quantization** — full BF16 only
- **No upscalers** — raw model output
- **No video stretching** — generate longer, then trim
- **No looping** — every clip must be unique
- **80GB VRAM minimum** for H100/A100
- **250GB disk minimum** per VM

---

## Assembly Pipeline

### The Concat Bug (v1–v3)
Using `ffmpeg -f concat` with `-vf scale` re-encoding causes silent clip drops when input clips have varying codecs or encoder parameters. Scenes that should be 180s come out as 60–90s.

**Root cause:** The concat demuxer with a video filter chain re-encodes all inputs through a single output encoder. When input clips have different H.264 profiles, levels, or timing parameters, ffmpeg silently skips incompatible clips rather than erroring.

### The Zombie Process Bug
Old assembly script runs (`assemble_local.py`, `assemble_v3.py`) spawning `ThreadPoolExecutor` workers that outlive the parent process. These zombies continue reading and deleting files from shared `tmp/` and `clips/` directories, causing new assembly runs to see "missing" files that were actually downloaded successfully.

**Fix:** Always `pkill -9 -f "assemble"` and `pkill -9 -f "ffmpeg"` before starting a new assembly run.

### Working Assembly Strategy (`assemble_v4.py`)
1. **Download** all clips for one scene (parallel curl, 8 workers)
2. **Normalize** each clip individually and sequentially (no parallel ffmpeg — 2 vCPU sandbox can't handle it):
   ```
   ffmpeg -vf "scale=768:512:...,pad=768:512:-1:-1,fps=24,setsar=1"
          -c:v libx264 -preset fast -crf 23 -pix_fmt yuv420p -an
   ```
3. **Concat** normalized clips with `-c copy` (fast, no re-encoding)
4. **Trim** to narration duration
5. **Mux** with narration audio (`-c:v copy -c:a aac -b:a 192k -ar 44100 -ac 2`)
6. **Clean up** all temp files before next scene

### Audio Format Gotcha
Early scenes (1–3, 6, 11) were built with 24kHz mono audio while later scenes use 44.1kHz stereo. The final concat requires uniform audio. Fix: re-mux non-standard scenes with `-c:v copy -c:a aac -ar 44100 -ac 2`.

---

## Cost & Infrastructure

### Vast.ai Spending
- Round 1 (167 clips, 10 VMs): ~$40–60
- Round 2 (268 clips, 10 VMs): ~$30–40
- Round 3 (477/494 clips, 8 VMs): ~$50–70 (credit exhausted at 97%)
- **Total estimate: ~$130–170** for all video generation

### VM Specifications Used
- H100 80GB SXM5 — primary generation GPU
- A100 80GB PCIe — fallback when H100 unavailable
- RTX 5070 Ti 16GB (ID: 32876887) — user's persistent VM, DO NOT TOUCH
- Minimum: 80GB VRAM, 250GB disk

### B2 Storage
- Bucket: `economy-vid-assets`
- Path: `v7_war_economy/`
- Total clips stored: 912
- Final video: `FINAL_war_economy_v2.mp4` (570 MB)
- Estimated total storage: ~3–4 GB

---

## File Inventory

### Core Scripts
| File | Purpose |
|------|---------|
| `a100_generate_v2.py` | Generation script with inline B2 upload |
| `generate_fill_clips.py` | Analyzes coverage gaps, generates fill clip prompts |
| `generate_fill_clips_v2.py` | V2 gap analysis with improved prompt generation |
| `assemble_v4.py` | Working assembly script (normalize → concat → mux) |
| `assemble_final.py` | VM-based assembly (deprecated — requires GPU VM) |
| `tts_generate.py` | Qwen3-TTS narration generation |
| `pipeline.py` | End-to-end pipeline orchestrator |
| `upload_pipeline.py` | B2 upload utilities |

### Key Manifests
| File | Contents |
|------|----------|
| `assembly_manifest_v2.json` | Final manifest: 912 clips mapped to 26 scenes |
| `all_video_prompts.json` | Original 167 clip prompts |
| `fill_clips_final.json` | V1 fill: 268 clip prompts |
| `fill_clips_v2.json` | V2 fill: 494 clip prompts |
| `fill_v2_vm_[a-h].json` | Per-VM split manifests for 8-VM round |
| `missing_clips.json` | 17 clips that weren't generated (credit ran out) |
| `narration_script.json` | Full narration text + timing for all 26 scenes |

---

## Scene Breakdown

| Scene | Narration | Clips | Notes |
|-------|-----------|-------|-------|
| 1 | 173.5s | 35 | |
| 2 | 187.0s | 38 | |
| 3 | 170.3s | 35 | |
| 4 | 161.3s | 33 | |
| 5 | 180.4s | 37 | |
| 6 | 165.9s | 34 | |
| 7 | 165.0s | 33 | |
| 8 | 192.7s | 39 | |
| 9 | 171.1s | 35 | |
| 10 | 177.6s | 36 | |
| 11 | 140.9s | 29 | Shortest scene |
| 12 | 187.4s | 38 | |
| 13 | 164.3s | 33 | |
| 14 | 129.9s | 26 | Fewest clips |
| 15 | 149.8s | 30 | |
| 16 | 192.0s | 39 | |
| 17 | 173.8s | 34 | 1 fill clip missing |
| 18 | 181.2s | 37 | |
| 19 | 197.6s | 39 | 1 fill clip missing |
| 20 | 161.0s | 20 | 13 fill clips missing (still full coverage from rounds 1+2) |
| 21 | 180.7s | 37 | |
| 22 | 175.7s | 36 | |
| 23 | 202.0s | 40 | 1 fill clip missing |
| 24 | 160.1s | 33 | |
| 25 | 178.8s | 35 | 1 fill clip missing |
| 26 | 250.6s | 51 | Longest scene (finale) |
| **Total** | **4570.6s** | **912** | **76.2 min** |

---

## Lessons Learned

1. **PYTHONPATH > pip install** for packages with broken build backends. Especially on Vast.ai where system Python environments are fragile.

2. **Kill zombie processes before every assembly run.** ThreadPoolExecutor workers from crashed/timed-out scripts persist and silently corrupt subsequent runs.

3. **Normalize clips individually, then concat with `-c copy`.** Never use `-f concat` with `-vf` filters — it silently drops clips.

4. **Sequential ffmpeg on small machines.** Parallel ffmpeg on 2 vCPU causes resource exhaustion (rc=254 kills). Download in parallel (curl is lightweight), encode sequentially.

5. **Upload each clip immediately after generation.** Don't batch uploads — if the VM dies or credit runs out, completed clips are lost.

6. **Uniform audio format before final concat.** Mixed sample rates (24kHz vs 44.1kHz) cause concat failures. Standardize early.

7. **Vast.ai credit monitoring.** Set alerts. The platform doesn't gracefully handle zero-balance — VMs just exit, losing any in-progress work.

8. **Cross-VM tarball transfer** is faster and more reliable than re-cloning + re-installing when multiple VMs need identical environments.

---

## Open Issues

- **17 missing clips** — not critical (all scenes still have full coverage from earlier rounds), but generates slightly less visual variety for scenes 17, 19, 20, 23, 25
- **No Frame.io upload** — deferred per user instruction ("ignore frames for now")
- **Video quality at 768×512** — LTX-2.3 raw output; no upscaling per hard constraint
- **Clip transitions** — currently hard cuts between clips within each scene; crossfades could improve flow
- **YouTube upload** — not yet done for v7; previous v5 was uploaded as unlisted
