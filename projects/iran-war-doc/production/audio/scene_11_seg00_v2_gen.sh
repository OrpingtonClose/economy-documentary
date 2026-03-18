#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Let us be precise about what this means and does not mean. This is not a claim that Binance intentionally funded the IRGC. It is a claim that the regulatory architecture for large-scale stablecoin flows was insufficient to prevent it. It is a claim that the gap between the legal standard — know your customer, anti-money laundering, OFAC screening — and the operational reality of a high-volume global crypto exchange processing billions of transactions per day was wide enough to drive one-point-seven billion dollars through. It is not a conspiracy claim. It is a systems claim.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_11_seg00_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_11_seg00_v2.wav')
"
