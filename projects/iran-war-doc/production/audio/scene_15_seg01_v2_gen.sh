#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Bloomberg\'s March eighth investigation — \'How the Son of Iran\'s Supreme Leader Built a Global Property Empire\' — documents the full scope. The Hilton Frankfurt Gravenbruch, a five-star hotel in Germany\'s financial capital. Hotels in Mallorca. A villa in Dubai\'s most exclusive residential district — described in real estate marketing as \'the Beverly Hills of Dubai.\' A C$10.5 million penthouse in Toronto, sold in 2020 — after the sale, but before the war. And the centerpiece: over one hundred million pounds in UK property holdings, concentrated on The Bishops Avenue, a ten-minute drive from the Israeli embassy in London.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_15_seg01_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_15_seg01_v2.wav')
"
