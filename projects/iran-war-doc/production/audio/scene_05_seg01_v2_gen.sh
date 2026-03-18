#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''To understand the significance, you need to understand what the CFTC filing reveals and what it doesn\'t. It reveals the aggregate position — the total bet on higher oil prices — but not who, specifically, built it, or why, or what information they were trading on. The filing is a consequence. The cause is elsewhere. What we know is that between January and February 2026, managed money — hedge funds and commodity trading advisors — systematically accumulated a multi-billion-dollar bet that oil prices would rise sharply. They were correct. To a degree of precision that is either extraordinary skill, extraordinary luck, or something else.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_05_seg01_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_05_seg01_v2.wav')
"
