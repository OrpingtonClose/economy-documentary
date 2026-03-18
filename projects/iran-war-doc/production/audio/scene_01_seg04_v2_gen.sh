#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The most useful question in financial journalism is not \'who did this?\' It is \'who benefited?\' Not who claimed to benefit. Not who was supposed to. Who actually, measurably, in the filings and the portfolio reports and the earnings calls, benefited. The answer, in this war, involves eight weeks of pre-positioning in crude oil futures, a Tether stablecoin pipeline through Binance, a London property empire on Billionaire\'s Row, and an emergency production mandate signed at the White House on Day Five. We begin with the invoice.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_01_seg04_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_01_seg04_v2.wav')
"
