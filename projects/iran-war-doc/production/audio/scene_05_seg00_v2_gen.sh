#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''January fifth, 2026. Eight weeks before the first strike. Something begins to appear in the CFTC\'s weekly Commitments of Traders filings — the public database in which all major futures positions must be disclosed. Hedge funds and money managers begin systematically accumulating long positions in Brent crude oil futures. Not opportunistically. Systematically. Week by week, the net-long position grows. The analyst who first documented this pattern in detail was Navnoor Bawa, writing on March second. By February twenty-fourth — four days before the war begins — the total net-long Brent crude position held by managed money had reached three hundred twenty thousand, nine hundred and fifty-two lots. One week alone saw an increase of fifty-seven thousand, seven hundred and sixty-six lots — a single-week accumulation representing roughly four billion dollars in notional exposure. Built over eight weeks.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_05_seg00_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_05_seg00_v2.wav')
"
