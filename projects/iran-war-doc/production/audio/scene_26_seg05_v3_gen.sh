#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Every war is, in part, a balance sheet. The balance sheet of the Iran War of 2026 — incomplete, still being written, many of its entries still classified — already has one consistent feature. The people in the profit column are not the people who went to war. The people who went to war are in the cost column. This observation is not new. What is new is the documentation. The numbers are in the CFTC filings. In the CSIS spreadsheets. In the Bloomberg property records. In the Chainalysis crypto crime report. In the Goldman Sachs research note. In the Insurance Journal\'s Hormuz premium data. In the Navnoor Bawa analysis of pre-war positioning. The ledger is public. The frame was missing.'''
speaker = 'male_narrator_03'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_26_seg05_v3.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_26_seg05_v3.wav')
"
