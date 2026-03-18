#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''That\'s the digital layer. There is also the physical layer. One hundred and seventy to two hundred million barrels of Iranian oil sitting in what are called \'ghost tankers\' — vessels that have disabled their automatic identification system transponders, relabeled their cargo manifests as \'Malaysian crude,\' and are selling to Chinese refineries at a discount to prevailing market rates. The discount is the cost of the legal ambiguity. The Chinese refineries get cheap oil. Iran gets hard currency. At one hundred dollars per barrel — which is approximately what oil was trading at during the conflict — that is fourteen to seventeen billion dollars in accessible oil revenue sitting offshore. Not frozen. Not seized. Just dark.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_09_seg01_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_09_seg01_v1.wav')
"
