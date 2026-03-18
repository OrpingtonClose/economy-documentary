#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The specific shell companies documented in the Bloomberg investigation: Birch Ventures Ltd., Isle of Man. Ziba Leisure Ltd., Saint Kitts and Nevis. A&A Leisure Ltd. Veritas Reales Investment Ltd. Midas Oil Industries FZC, UAE. Midas Oil Trading DMCC, UAE. Six companies. Five jurisdictions. Zero single point at which any one jurisdiction can see the entire chain. This is the architecture of opacity. Each link is, individually, potentially legal. The chain, collectively, moves sanctioned wealth outside the reach of the sanctions regime.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_16_seg02_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_16_seg02_v2.wav')
"
