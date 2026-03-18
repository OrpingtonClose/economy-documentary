#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''To understand the architecture: Tether stablecoins are issued by a private company, Tether Limited. They circulate on multiple blockchains. They are used extensively in countries with restricted access to the formal US dollar banking system — including Iran, which has been under comprehensive financial sanctions since 2019. The stablecoin was designed as a financial inclusion tool. It became, in parallel, a sanctions circumvention tool. The Chainalysis 2026 Crypto Crime Report documented over one hundred and four billion dollars in total sanctions-busting flows through the crypto system in the year preceding the war.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_08_seg02_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_08_seg02_v2.wav')
"
