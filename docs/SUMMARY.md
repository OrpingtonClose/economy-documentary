# LTX-2 Pipeline Files Summary

## Python Files List

All Python files in `https://github.com/Lightricks/LTX-2/tree/main/packages/ltx-pipelines/src/ltx_pipelines`:

1. **__init__.py**
2. **a2vid_two_stage.py** - Audio-to-video two-stage pipeline
3. **distilled.py** - Distilled model pipeline
4. **ic_lora.py** - Image conditioning with LoRA
5. **keyframe_interpolation.py** - Keyframe interpolation pipeline
6. **retake.py** - Retake pipeline
7. **ti2vid_one_stage.py** - Text/Image-to-Video ONE STAGE pipeline ✓
8. **ti2vid_two_stages.py** - Text/Image-to-Video TWO STAGES pipeline ✓
9. **ti2vid_two_stages_hq.py** - High-quality two-stage pipeline variant

Note: There's also a 'utils' subdirectory with additional utility files.

---

## Extracted Files

### 1. ti2vid_two_stages.py
- **Class**: `TI2VidTwoStagesPipeline`
- **Description**: Two-stage text/image-to-video generation pipeline
- **Lines**: 303 lines
- **Stage 1**: Generates video at half the target resolution with CFG guidance
- **Stage 2**: Upsamples by 2x and refines using a distilled LoRA for higher quality
- **File Location**: `/home/user/workspace/ti2vid_two_stages.py`

### 2. ti2vid_one_stage.py
- **Class**: `TI2VidOneStagePipeline`
- **Description**: Single-stage text/image-to-video generation pipeline
- **Lines**: 223 lines
- **Process**: Generates video at target resolution in a single diffusion pass with CFG
- **File Location**: `/home/user/workspace/ti2vid_one_stage.py`

---

## Key Differences Between Pipelines

### Two-Stage Pipeline (ti2vid_two_stages.py)
- Uses two model ledgers (stage_1_model_ledger and stage_2_model_ledger)
- Stage 1 outputs at half resolution (width//2, height//2)
- Stage 2 upsamples using spatial upsampler and applies distilled LoRA
- Uses distilled sigmas (STAGE_2_DISTILLED_SIGMA_VALUES) in stage 2
- More complex but higher quality output

### One-Stage Pipeline (ti2vid_one_stage.py)
- Uses single model ledger
- Generates at full target resolution directly
- Simpler, faster but potentially lower quality
- No upsampling or refinement step

Both pipelines support:
- Image conditioning
- Audio generation
- Prompt enhancement
- CFG and STG guidance
- LoRA support
