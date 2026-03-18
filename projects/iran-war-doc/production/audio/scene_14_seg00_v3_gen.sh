#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''In the annals of strategic decision-making, few trade-offs have been as nakedly documented as this one. America lifted sanctions on its adversary\'s major strategic partner, fractured its European alliance relationships, and undermined three years of coordinated economic pressure on a country actively supporting Iran — in exchange for a modest reduction in domestic gasoline prices during a leisure travel week. The cost-benefit analysis, in geopolitical terms, is stark and unfavorable. In domestic political terms, it is entirely rational, if you define \'rational\' as \'optimized for the next news cycle and the next approval poll.\''''
speaker = 'male_narrator_03'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_14_seg00_v3.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_14_seg00_v3.wav')
"
