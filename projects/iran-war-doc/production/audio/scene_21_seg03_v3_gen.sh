#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The economist John Kenneth Galbraith called this dynamic \'private profit, public cost.\' He was writing about environmental externalities — the cost of pollution borne by the public while the profits of production are privately retained. He would recognize the defense procurement version of the argument immediately and without hesitation. The profit of the buyback was private. The cost of the production shortfall is public. The emergency contract that bridges the gap transfers the public cost back to private revenue, at a premium. This is not corruption. This is the market, working precisely as advertised.'''
speaker = 'male_narrator_03'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_21_seg03_v3.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_21_seg03_v3.wav')
"
