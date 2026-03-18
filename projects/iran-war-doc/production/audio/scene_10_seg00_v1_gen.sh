#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''One more fact. Zhao Changpeng — known in crypto markets as CZ, the founder and former CEO of Binance, the largest cryptocurrency exchange in the world — had previously pled guilty to US money laundering charges in November 2023. He stepped down as CEO. He agreed to a substantial personal fine. In early 2025, he was pardoned. By the Trump administration. The exchange he founded — the exchange through which, according to Fortune\'s March twelfth investigation, one-point-seven billion dollars had flowed to Iranian Revolutionary Guard-linked entities — continued operating throughout this period. The DOJ launched its investigation into those specific flows on March eleventh, 2026. Eleven days into the war.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_10_seg00_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_10_seg00_v1.wav')
"
