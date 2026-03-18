#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''In 2026, the US government was simultaneously: bombing Iran\'s military infrastructure at a rate of one-point-five billion dollars per day; and providing the financial rails through which one-point-seven billion dollars reached the IRGC. Not the same departments. Not the same people. Not any kind of coordination. The left hand and the right hand of the most powerful financial system in the world were operating in direct, structural opposition to each other. The Department of Defense was transferring value to one side. The financial system — through the Tether-Tron-Binance architecture — was transferring value to the other.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_11_seg02_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_11_seg02_v1.wav')
"
