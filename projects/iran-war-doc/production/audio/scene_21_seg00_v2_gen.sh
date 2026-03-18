#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Between 2020 and 2025, the five largest US defense contractors — Lockheed Martin, RTX, Northrop Grumman, Boeing Defense, and General Dynamics — spent a combined one hundred and ten billion dollars on stock buybacks and dividends. One hundred and ten billion dollars. That is the number documented by TheBoard.world in its March fifteenth analysis of defense sector capital allocation. One hundred and ten billion dollars that could have expanded THAAD production lines from ninety-six per year to the four hundred per year now being demanded on an emergency basis. That could have built the Tomahawk manufacturing capacity that will now require premium-priced emergency investment. That could have prevented the fourteen-point-six percent depletion of Patriot stockpiles in the first forty-eight hours of a regional conflict that had been publicly assessed as a risk for years.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_21_seg00_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_21_seg00_v2.wav')
"
