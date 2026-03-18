#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''But the pre-war baseline was already fragile, which is the detail that makes the Goldman call serious. GDP in Q4 2025 had been revised down to zero-point-seven percent annualized growth — half the previous estimate of one-point-three percent. The revision came from CNN\'s reporting on March thirteenth, citing Bureau of Economic Analysis data. Zero-point-seven percent is not a recession, technically. It is not growth that can absorb an external shock. An economy growing at zero-point-seven percent is an economy with no margin. The war provided the shock. The margin was not there.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_24_seg01_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_24_seg01_v2.wav')
"
