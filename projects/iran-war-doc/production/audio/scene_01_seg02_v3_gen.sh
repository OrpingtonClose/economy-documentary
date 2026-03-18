#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Every war has a price tag. What changes, across centuries, is who gets to see it. In 1914, the British public had no idea what the Somme cost in sterling per day. In 1968, the American public did not receive a daily invoice for Vietnam. In 2026, the numbers exist. They are published. They are in CSIS spreadsheets and Goldman Sachs research notes and CFTC filings. The information is not hidden. What is missing is the argument. Nobody assembled the missile cost alongside the family\'s gas bill. Nobody put the tanker ETF return beside the small business owner\'s margin compression. Nobody connected the defense CEO\'s net worth to the SPR drawdown.'''
speaker = 'male_narrator_03'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_01_seg02_v3.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_01_seg02_v3.wav')
"
