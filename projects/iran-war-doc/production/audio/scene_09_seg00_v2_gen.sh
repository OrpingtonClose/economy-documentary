#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The Binance pipeline was one component of Iran\'s war financing. The Chainalysis 2026 Crypto Crime Report documented the full scope. Iran\'s total crypto war chest at the start of the conflict: seven-point-seven-eight billion dollars. Of that, the IRGC controls more than fifty percent — approximately three-point-nine billion — in crypto assets that can be moved, converted, and spent outside the reach of US sanctions. These are not hypothetical or estimated assets. They are documented on the blockchain — transparent, traceable, and yet legally difficult to seize because of jurisdictional gaps between where the assets sit, where the beneficiaries are, and where the enforcement authority resides.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_09_seg00_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_09_seg00_v2.wav')
"
