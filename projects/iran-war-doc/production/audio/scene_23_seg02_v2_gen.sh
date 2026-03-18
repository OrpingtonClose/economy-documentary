#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The insurance market is, in some ways, the most honest market in the world — because it prices actual risk in real time, with no political motive. A one-thousand-percent surge in Hormuz premiums is the insurance market saying: the probability of losing a ship in this waterway has increased by a factor of ten. That assessment has direct economic consequences. Every vessel that cannot afford the premium cannot transit. Every vessel that can afford it passes the cost to its cargo owners, who pass it to retailers, who pass it to consumers. The four-million-dollar transit premium is distributed across millions of units of goods, invisibly, as a slightly higher price for everything.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_23_seg02_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_23_seg02_v2.wav')
"
