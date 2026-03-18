#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The mechanics of the tanker trade are straightforward to anyone who has spent time in commodity markets. If you know that a conflict in the Persian Gulf is imminent, you know two things simultaneously. First: oil prices will rise, because supply will be disrupted. Second: oil will need to be rerouted around the Strait of Hormuz, vastly increasing the ton-miles traveled by every barrel going from the Gulf to Europe or East Asia. The rerouting via the Cape of Good Hope adds approximately fifteen days to a Gulf-to-Europe transit. More days at sea means more day-rate revenue for the tanker. More revenue means higher stock price. A ninety-eight percent gain in less than two months, in a normally stable operational fund, is not an accident. It is the market\'s expression of specific, directional information.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_06_seg01_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_06_seg01_v2.wav')
"
