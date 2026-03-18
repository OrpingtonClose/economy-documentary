#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The Breakwave Tanker Shipping ETF — BWET — was up ninety-eight percent year-to-date as of February twenty-eighth. Ninety-eight percent in less than two months, in a fund that tracks operational shipping companies. Gold ETF inflows hit nineteen billion dollars globally — a record. These numbers, individually, could each be explained by sophisticated macro analysis. Together, they describe something specific and consistent: a market that had already processed the probability of a major Persian Gulf conflict and was expressing it in its position book.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_05_seg03_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_05_seg03_v2.wav')
"
