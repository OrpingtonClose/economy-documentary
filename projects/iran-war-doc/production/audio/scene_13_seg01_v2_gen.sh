#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The deeper problem was not the contradiction — contradictions in US foreign policy are not historically unusual. The deeper problem was the signal. The European sanctions architecture against Russia had been built painstakingly over three years, with enormous economic cost to European energy markets and European industrial competitiveness. It was built on the premise that the United States was a reliable, consistent partner. The Bessent decision, made for domestic American political reasons, demonstrated that that premise was conditional — conditional on whether the cost to American consumers was tolerable. When the cost became politically uncomfortable, the architecture could be adjusted. That is a different alliance than the one Europeans thought they were in.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_13_seg01_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_13_seg01_v2.wav')
"
