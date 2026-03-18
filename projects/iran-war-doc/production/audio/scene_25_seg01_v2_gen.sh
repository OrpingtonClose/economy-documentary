#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The contrast with other major economies is stark and deliberate. South Korea had nine days of LNG storage when the war began — nine days. Europe was at thirty percent of storage capacity in early March, entering a critical period for LNG replenishment before the following winter. The Hormuz closure\'s impact on European gas storage calculations was immediate and severe: LNG that should be flowing from Qatar to European terminals was either stuck in the Gulf or was being rerouted at enormous cost premium. The energy security vulnerability that European governments had spent three years trying to reduce after the 2022 gas crisis was being re-exposed by a conflict ten thousand miles away.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_25_seg01_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_25_seg01_v2.wav')
"
