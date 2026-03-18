#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The hedge funds who spent eight weeks systematically accumulating three hundred and twenty thousand nine hundred and fifty-two Brent crude lots before the first strike — four billion dollars in notional exposure, built in the public CFTC filings, available to anyone who looked. The tanker ETF that gained ninety-eight percent year-to-date on the pre-positioning of a war that was, apparently, predictable from financial markets even if it was not predictable from the news broadcasts. The defense contractors whose stocks hit all-time highs in the first week of conflict — Lockheed Martin up forty percent since June 2025, Northrop Grumman above seven hundred and five dollars, RTX up over one hundred percent in three years — and who left the White House on Day Five with emergency production mandates to quadruple output of the exact weapons that had just been consumed.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_26_seg01_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_26_seg01_v1.wav')
"
