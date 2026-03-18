#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The Iranian war financiers who moved one-point-seven billion dollars through Binance to the IRGC using a Tether-on-Tron stablecoin architecture that the regulatory system had documented gaps in and had, for reasons both technical and political, not closed. The ghost tanker operators sitting on one hundred and seventy to two hundred million barrels of Iranian crude off the coast of Malaysia — fourteen to seventeen billion dollars in accessible oil revenue, dark, untracked, waiting for the optimal moment. Mojtaba Khamenei, whose one-hundred-million-pound London property empire on Billionaire\'s Row sat untouched throughout twelve days of Tomahawk strikes against his country\'s military infrastructure — the missiles did not have a guidance system for The Bishops Avenue.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_26_seg02_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_26_seg02_v2.wav')
"
