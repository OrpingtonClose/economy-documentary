#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The Strategic Petroleum Reserve release — one hundred seventy-two million barrels, the largest coordinated IEA release in history, across thirty-two countries releasing a combined four hundred million barrels — was designed to blunt this. It worked. Partially. For a while. But every barrel released from the SPR is a barrel that will cost approximately one hundred dollars or more to replace. That is not a solved problem. That is a deferred bill. Deferred bills do not disappear. They compound. The deferred refill cost of the SPR release is approximately twenty billion dollars, payable at a future date, by a future taxpayer, for a decision made in March 2026.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_03_seg02_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_03_seg02_v2.wav')
"
