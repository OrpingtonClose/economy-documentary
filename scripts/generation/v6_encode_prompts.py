#!/usr/bin/env python3
"""
Phase 1: Encode all unique prompts from clip plan.
Loads text encoder once, encodes all prompts (moving raw outputs to CPU),
frees text encoder, loads embeddings processor, processes all, saves.

Key structure:
  text_encoder.encode(prompt) → (hidden_states, attention_mask)
    hidden_states = tuple[torch.Tensor, ...] (per-layer, from output_hidden_states=True)
    attention_mask = torch.Tensor [B, seq_len]
  embeddings_processor.process_hidden_states(hidden_states, attention_mask) → EmbeddingsProcessorOutput
    .video_encoding, .audio_encoding, .attention_mask
"""
import json
import hashlib
import logging
import gc
from pathlib import Path

import torch

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

CHECKPOINT_PATH = "/root/models/ltx-2.3-22b-dev.safetensors"
GEMMA_ROOT = "/root/models/text_encoder"
CLIP_PLAN = "/root/v5_clip_plan.json"
EMBEDDINGS_DIR = Path("/root/embeddings_cache")

DEFAULT_NEGATIVE_PROMPT = (
    "blurry, out of focus, overexposed, underexposed, low contrast, washed out colors, excessive noise, "
    "grainy texture, poor lighting, flickering, motion blur, distorted proportions, unnatural skin tones, "
    "deformed facial features, asymmetrical face, missing facial features, extra limbs, disfigured hands, "
    "wrong hand count, artifacts around text, inconsistent perspective, camera shake, incorrect depth of "
    "field, background too sharp, background clutter, distracting reflections, harsh shadows, inconsistent "
    "lighting direction, color banding, cartoonish rendering, 3D CGI look, unrealistic materials, uncanny "
    "valley effect, incorrect ethnicity, wrong gender, exaggerated expressions, wrong gaze direction, "
    "mismatched lip sync, silent or muted audio, distorted voice, robotic voice, echo, background noise, "
    "off-sync audio, incorrect dialogue, added dialogue, repetitive speech, jittery movement, awkward "
    "pauses, incorrect timing, unnatural transitions, inconsistent framing, tilted camera, flat lighting, "
    "inconsistent tone, cinematic oversaturation, stylized filters, or AI artifacts."
)

def prompt_hash(prompt: str) -> str:
    return hashlib.md5(prompt.encode()).hexdigest()[:12]

@torch.inference_mode()
def main():
    EMBEDDINGS_DIR.mkdir(parents=True, exist_ok=True)
    
    with open(CLIP_PLAN) as f:
        plan = json.load(f)
    
    # All unique prompts + negative
    unique_prompts = sorted(set(c["prompt"] for c in plan["clips"]))
    all_prompts = unique_prompts + [DEFAULT_NEGATIVE_PROMPT]
    
    # Filter already cached
    to_encode = [p for p in all_prompts 
                 if not (EMBEDDINGS_DIR / f"{prompt_hash(p)}_v.pt").exists()]
    
    if not to_encode:
        log.info(f"All {len(all_prompts)} prompts already cached")
        return
    
    log.info(f"Encoding {len(to_encode)} of {len(all_prompts)} total prompts...")
    
    from ltx_pipelines.utils import ModelLedger, cleanup_memory
    
    ledger = ModelLedger(
        dtype=torch.bfloat16,
        device=torch.device("cuda"),
        checkpoint_path=CHECKPOINT_PATH,
        gemma_root_path=GEMMA_ROOT,
        loras=(),
        quantization=None,
    )
    
    # Step 1: Load text encoder, encode all prompts, move raw outputs to CPU
    text_encoder = ledger.text_encoder()
    log.info(f"Text encoder loaded: {torch.cuda.memory_allocated()/1e9:.1f}GB VRAM")
    
    # text_encoder.encode(prompt) returns:
    #   (hidden_states: tuple[torch.Tensor, ...], attention_mask: torch.Tensor)
    # hidden_states is a tuple of per-layer tensors from Gemma
    raw_outputs = []  # list of (prompt, (hs_tuple_cpu, mask_cpu))
    for i, prompt in enumerate(to_encode):
        hs_tuple, mask = text_encoder.encode(prompt)
        # hs_tuple is tuple of tensors — move each to CPU
        hs_tuple_cpu = tuple(t.cpu() for t in hs_tuple)
        mask_cpu = mask.cpu()
        raw_outputs.append((prompt, (hs_tuple_cpu, mask_cpu)))
        
        if (i + 1) % 50 == 0:
            log.info(f"  Encoded {i+1}/{len(to_encode)}, VRAM: {torch.cuda.memory_allocated()/1e9:.1f}GB")
    
    log.info(f"All encoded ({len(raw_outputs)} prompts). Freeing text encoder...")
    del text_encoder
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.synchronize()
    log.info(f"After free: {torch.cuda.memory_allocated()/1e9:.1f}GB VRAM")
    
    # Step 2: Load embeddings processor, process all raw outputs
    emb_proc = ledger.gemma_embeddings_processor()
    log.info(f"Embeddings processor loaded: {torch.cuda.memory_allocated()/1e9:.1f}GB VRAM")
    
    for i, (prompt, (hs_tuple_cpu, mask_cpu)) in enumerate(raw_outputs):
        # Move back to GPU for processing
        hs_tuple_gpu = tuple(t.cuda() for t in hs_tuple_cpu)
        mask_gpu = mask_cpu.cuda()
        
        processed = emb_proc.process_hidden_states(hs_tuple_gpu, mask_gpu)
        
        h = prompt_hash(prompt)
        torch.save(processed.video_encoding.cpu(), EMBEDDINGS_DIR / f"{h}_v.pt")
        torch.save(processed.audio_encoding.cpu(), EMBEDDINGS_DIR / f"{h}_a.pt")
        
        del hs_tuple_gpu, mask_gpu, processed
        
        if (i + 1) % 50 == 0:
            log.info(f"  Processed {i+1}/{len(raw_outputs)}")
    
    del emb_proc, raw_outputs
    cleanup_memory()
    log.info(f"Done! Embeddings saved to {EMBEDDINGS_DIR}")

if __name__ == "__main__":
    main()
