#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Let us talk about tankers. Specifically, a publicly traded exchange-traded fund called BWET — the Breakwave Tanker Shipping ETF. It tracks the stocks of companies that own crude oil supertankers. In a normal market environment, a tanker ETF trades in a fairly narrow range — it is an operational shipping business, not a speculative vehicle. You do not normally double your money in a tanker ETF in two months. Between January first and February twenty-eighth, 2026, BWET gained ninety-eight percent. Almost doubled. In fifty-nine days. While no war had been declared. While tensions, by official diplomatic accounts, were \'managed.\' While the Secretary of State was still making statements about diplomatic channels.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_06_seg00_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_06_seg00_v1.wav')
"
