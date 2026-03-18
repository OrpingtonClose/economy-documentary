#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''By Day Six, the total war bill had reached eleven-point-three billion dollars. By Day Twelve: sixteen-point-five billion. The war was running at approximately one-point-five billion dollars per day — every single day. To be precise: one billion, five hundred million dollars. Every twenty-four hours. That is roughly the annual education budget of the state of Mississippi, burned through each day. That is not a military budget. That is a destruction rate. And the destruction was not symmetric — because munitions production is slow, and munitions consumption in modern conflict is fast. The gap between those two rates is not just a logistics problem. It is a balance sheet problem.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_02_seg01_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_02_seg01_v1.wav')
"
