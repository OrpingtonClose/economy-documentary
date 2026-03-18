#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Twenty-five percent probability of recession. That is one in four. That means: the family who absorbed seventy cents more per gallon, who watched their grocery bill rise, who noticed that the credit card balance was not going down this month — they were standing in a one-in-four probability space of living through the beginning of a recession. Not caused by their decisions. Not announced in advance. Not voted on. Transmitted to them through the oil price, which was transmitted from the Strait of Hormuz, which was transmitted from a decision made in a room they never entered, by people whose financial positions were arranged to benefit from the answer being yes.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_24_seg02_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_24_seg02_v1.wav')
"
