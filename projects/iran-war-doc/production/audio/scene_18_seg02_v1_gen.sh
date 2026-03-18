#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''On the day of the meeting, Lockheed Martin\'s stock had already gained forty percent since June 2025 — a thirty-four-point-seven percent year-to-date gain at the time of the meeting. Northrop Grumman had hit an all-time high above seven hundred and five dollars per share. RTX had gained one hundred and ten percent over three years. The iShares Defense ETF — a broad basket of US defense companies — was up fourteen percent year-to-date, outperforming the broader market significantly. The CEOs arrived at the White House wealthy. They left with a mandate to become substantially wealthier, backed by emergency government production contracts at premium pricing.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_18_seg02_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_18_seg02_v1.wav')
"
