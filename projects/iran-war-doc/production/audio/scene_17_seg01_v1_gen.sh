#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''This is the documentary\'s deepest uncomfortable fact. The people who bear the costs of war — who pay seventy cents more per gallon, who lose jobs to supply chain disruption, who serve in uniform in hostile seas, who live in the Iranian neighborhoods where the bombs fell — are not the people whose wealth survives the war intact. This is not an accident. It is an architecture. Economic warfare — sanctions, financial isolation, asset freezes — was explicitly designed to change this dynamic, to extend the reach of state power into the private wealth of adversaries. It is a design that has had limited success. The networks are faster than the regulators. The shell companies are cheaper to create than the lawyers it would take to unwind them. And the legal gaps between jurisdictions are precisely what the architecture exploits.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_17_seg01_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_17_seg01_v1.wav')
"
