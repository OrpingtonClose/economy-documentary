#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Here is the question that drives this entire documentary. Not: was it worth it? That is a political question, and politics will outlast the answer. The real question is: who decided it was affordable? Who was in the room? Who had their financial positions arranged to benefit from the answer being yes? Because somebody knew. And the paper trail — in the CFTC filings, in the tanker ETF returns, in the congressional disclosure forms — says they knew eight weeks in advance. That story begins in Act Two.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_04_seg03_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_04_seg03_v1.wav')
"
