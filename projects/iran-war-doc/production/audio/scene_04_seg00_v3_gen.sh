#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Let us be precise about what \'one-point-five billion dollars per day\' means in historical terms. In 1968, at the height of Vietnam, the war cost approximately twenty-one billion dollars per year — about fifty-eight million dollars per day in then-current dollars. The Iran War of 2026 burned through the equivalent of the entire Vietnam annual war budget in less than two weeks. In twelve days, it spent what the United States spent in an entire year in Southeast Asia. Wars have not gotten cheaper. They have gotten faster.'''
speaker = 'male_narrator_03'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_04_seg00_v3.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_04_seg00_v3.wav')
"
