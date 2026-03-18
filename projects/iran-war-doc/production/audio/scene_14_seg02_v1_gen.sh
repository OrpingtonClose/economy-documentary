#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Which brings us to the fundamental tension this documentary returns to throughout: who is the decision-maker optimizing for? Not rhetorically. Mechanically. In each major decision of this war — the decision to strike, the decision to lift Russian sanctions, the decision to release the SPR, the decision to invite defense CEOs to the White House — the answer to \'who benefits\' is consistent. It is not the family at the gas station. It is not the NATO ally in Warsaw or Warsaw. The beneficiaries are specific, identifiable, and positioned. The costs are diffuse, distributed, and absorbed by people who had no seat at the table.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_14_seg02_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_14_seg02_v1.wav')
"
