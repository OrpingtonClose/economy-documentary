#!/usr/bin/env python3
"""
Encode text prompts using LTX-2.3 Gemma text encoder.
Run as subprocess so GPU memory is fully freed on exit.

Usage: python3 encode_text.py --checkpoint ... --gemma-root ... --prompt "..." --neg-prompt "..." --output /path/to/encoded.pt
"""
import argparse
import torch
import os

os.environ["PYTORCH_ALLOC_CONF"] = "expandable_segments:True"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--gemma-root", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--neg-prompt", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    
    from ltx_pipelines.utils import ModelLedger, cleanup_memory
    
    device = torch.device("cuda")
    dtype = torch.bfloat16
    
    ledger = ModelLedger(
        dtype=dtype, device=device,
        checkpoint_path=args.checkpoint,
        gemma_root_path=args.gemma_root,
        loras=[], quantization=None,
    )
    
    text_encoder = ledger.text_encoder()
    raw_p = text_encoder.encode(args.prompt)
    raw_n = text_encoder.encode(args.neg_prompt)
    
    torch.cuda.synchronize()
    del text_encoder
    cleanup_memory()
    
    emb_proc = ledger.gemma_embeddings_processor()
    ctx_p = emb_proc.process_hidden_states(*raw_p)
    ctx_n = emb_proc.process_hidden_states(*raw_n)
    del emb_proc
    cleanup_memory()
    
    # Save all encodings to file on CPU
    torch.save({
        "v_context_p": ctx_p.video_encoding.cpu(),
        "a_context_p": ctx_p.audio_encoding.cpu() if ctx_p.audio_encoding is not None else None,
        "v_context_n": ctx_n.video_encoding.cpu(),
        "a_context_n": ctx_n.audio_encoding.cpu() if ctx_n.audio_encoding is not None else None,
        "attn_mask_p": ctx_p.attention_mask.cpu(),
        "attn_mask_n": ctx_n.attention_mask.cpu(),
    }, args.output)
    print(f"ENCODED_OK")

if __name__ == "__main__":
    with torch.inference_mode():
        main()
