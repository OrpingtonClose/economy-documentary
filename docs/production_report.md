# Economy 2025 Documentary — Production Report

## Final Output
- **B2 URL**: https://f004.backblazeb2.com/file/economy-vid-assets/documentary/economy_2025_documentary_final.mp4
- **Duration**: ~99 minutes (42 scenes)
- **File size**: 966 MB
- **Resolution**: 768x512, 24fps
- **Codec**: H.264 video, AAC audio (192kbps)

## Production Pipeline
| Component | Model/Tool | Details |
|-----------|-----------|---------|
| Video generation | LTX-2.3-22B-dev | BF16 full, no quantization, no distillation, no upscaling |
| Text encoder | Gemma-3-12B-IT | QAT Q4_0 unquantized, subprocess isolation for VRAM management |
| Audio narration | Qwen3-TTS VoiceDesign | 3 distinct voices (V1: young male, V2: 50s British male, V3: 40s female) |
| Diffusion | 30 steps, Euler scheduler | CFG 3.0 video / 7.0 audio, STG 1.0, rescale 0.7 |
| Infrastructure | 6x NVIDIA A100 80GB | Vast.ai (Oklahoma, Montana, Czechia, Massachusetts, UK-1, UK-2) |

## Generation Stats
- **Total clips generated**: 260 unique clips
- **Clips per scene**: 4-14 (avg ~6)
- **Clip duration**: 5.04s each (121 frames @ 24fps)
- **Time per clip**: ~3 minutes (text encode ~30s, denoise ~150s, decode ~20s)
- **Total generation time**: ~2.5 hours across 6 VMs
- **TTS narration**: 42 scenes with 3-voice dialogue, ~99 minutes total

## Scene Breakdown
| Scenes | VM | Location | Duration |
|--------|-----|----------|----------|
| 1-7 | Oklahoma | US | 17.3 min |
| 8-14 | Montana | US | 16.0 min |
| 15-21 | Czechia | EU | 17.3 min |
| 22-28 | Massachusetts | US | 16.7 min |
| 29-35 | UK-1 | UK | 17.0 min |
| 36-42 | UK-2 | UK | 14.8 min |

## Technical Notes
- Subprocess isolation solved CUDA OOM: text encoder (Gemma-3-12B ~24GB) runs in subprocess, exits to fully free GPU, then transformer (22B ~44GB) loads
- Peak VRAM during denoising: ~42.8 GB
- Video clips looped to match narration length during mixing phase
- Metadata embedded in MP4 file (title, model info, production parameters)

## Uploads
- **B2**: ✅ Uploaded to `economy-vid-assets/documentary/economy_2025_documentary_final.mp4` with production metadata in file info
- **Frame.io**: ⏳ Requires OAuth browser authorization (Web App credentials need authorization code flow)
