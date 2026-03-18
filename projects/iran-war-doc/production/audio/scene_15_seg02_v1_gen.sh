#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The ten-minute drive from Billionaire\'s Row to the Israeli embassy is not incidental. The Bloomberg reporting notes it directly. The geographical proximity of Mojtaba Khamenei\'s London real estate to the Israeli diplomatic mission is an illustration of a principle: economic warfare and conventional warfare operate on different maps. The missile flies toward Iranian military infrastructure. The wealth sits in north London. The gap between those two geographies is the gap in which impunity lives.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_15_seg02_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_15_seg02_v1.wav')
"
