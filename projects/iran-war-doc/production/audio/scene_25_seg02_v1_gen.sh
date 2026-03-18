#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The knock-on effects reached further than energy markets. Sulfur prices rose sixteen percent in the weeks following the conflict\'s start — a consequence of reduced sulfur-content crude throughput affecting the feedstocks used in fertilizer production. Fertilizer production is the foundation of global food supply. A sixteen percent rise in sulfur prices means a rise in fertilizer costs. A rise in fertilizer costs, applied at planting season across Asia, means a rise in food production costs. The war\'s economic radius extended from the Strait of Hormuz to the rice paddies of Asia. In the food inflation data of autumn 2026, the March missile strikes will be visible as a price signal.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_25_seg02_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_25_seg02_v1.wav')
"
