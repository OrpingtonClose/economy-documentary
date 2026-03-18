#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''March sixth, 2026. Day five of the war. Three days after the first strikes. The President of the United States convened a meeting in the White House. The attendees were not generals. They were not diplomats. They were CEOs. The CEOs of Lockheed Martin, RTX — formerly Raytheon Technologies — Boeing Defense, Northrop Grumman, and General Dynamics. The five largest defense contractors in the United States, by revenue. They were not invited to discuss strategy. They were not invited to discuss foreign policy. They were invited to discuss production expansion. The word that came out of that meeting, according to reporting from CNBC and Defense One: \'quadruple.\''''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_18_seg00_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_18_seg00_v1.wav')
"
