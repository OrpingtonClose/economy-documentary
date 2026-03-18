#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''On the same timeline: five-point-eight million Brent crude call options were purchased, clustered at strike prices of eighty-five, ninety, and one hundred dollars per barrel. Each of those strikes was above the prevailing market price at the time of purchase. They were bets that oil would go significantly higher — not marginally, but to specific thresholds. They paid off within forty-eight hours of the first strike. In option terms, they went from out-of-the-money to deeply in-the-money in one weekend. The profit on a five-point-eight million contract position moving through those strikes is measured in the billions.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_05_seg02_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_05_seg02_v1.wav')
"
