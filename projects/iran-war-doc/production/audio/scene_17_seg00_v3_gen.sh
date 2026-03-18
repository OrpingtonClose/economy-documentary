#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The total estimated Khamenei family wealth hidden outside Iran: between ninety-five and two hundred billion dollars. The range is wide because the secrecy is effective, and because it has been maintained across multiple changes in Iranian leadership, across multiple rounds of US and EU sanctions, across multiple years of financial investigation by multiple intelligence agencies. The US dropped Tomahawk cruise missiles on Iranian military infrastructure for twelve consecutive days. The London property is still there. The Frankfurt hotel is still open for business. The Dubai villa is still occupied. No missile has a guidance system programmed for The Bishops Avenue. Military power and financial power are not the same power.'''
speaker = 'male_narrator_03'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_17_seg00_v3.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_17_seg00_v3.wav')
"
