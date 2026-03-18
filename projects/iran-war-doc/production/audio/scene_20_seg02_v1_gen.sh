#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The iShares Defense ETF was up fourteen percent year to date by early March 2026. Lockheed: plus forty percent since June 2025. Northrop: all-time high above seven hundred and five dollars. RTX: up one hundred and ten percent over three years. These are not hypothetical or speculative conflicts of interest. These are measurable, documented financial gains accrued to specific identified decision-makers during the precise period of their decision-making authority over the industry generating those gains. The numbers are in the forty-five-day disclosure filings. The filings are public. Nobody went to prison. Nobody lost their committee chairmanship. The hearing rooms remained occupied. The stocks went up.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_20_seg02_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_20_seg02_v1.wav')
"
