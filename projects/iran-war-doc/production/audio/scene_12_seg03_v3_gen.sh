#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The political economy here is precise and important. The Trump administration was simultaneously fighting a war against Iran — Russia\'s strategic partner — and paying Russia — Iran\'s strategic partner — to moderate domestic gas prices. The two decisions are not contradictory in the logic of domestic American politics. A war twelve thousand kilometers away is abstract. Gas at three-seventy-two is tangible. Democratic leaders respond to tangible. And three-seventy-two, during spring break, is very tangible indeed.'''
speaker = 'male_narrator_03'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_12_seg03_v3.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_12_seg03_v3.wav')
"
