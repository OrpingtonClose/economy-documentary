#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Eleven days. The flows that would become the subject of that investigation had been occurring for months — the Fortune investigation suggests potentially years — before the first strike. The regulatory architecture that might have caught them — the Bank Secrecy Act, OFAC screening requirements, AML compliance obligations, FinCEN monitoring — existed on paper. It had a well-documented gap in enforcement: large-volume, fast-moving stablecoin flows on secondary blockchains like Tron. The transactions are visible on the blockchain. The beneficial ownership is not. The gap between blockchain transparency and beneficial-owner opacity is exactly the gap through which one-point-seven billion dollars passed.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_10_seg01_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_10_seg01_v2.wav')
"
