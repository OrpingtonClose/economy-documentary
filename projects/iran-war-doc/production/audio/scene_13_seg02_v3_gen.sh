#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The Trump administration\'s response — framed through Secretary Bessent — was to invoke market stability and economic sovereignty. \'We are a market-driven economy. This is a market-driven decision.\' It was a response that treated a geopolitical alliance like a vendor contract. Deliverable when convenient, renegotiable under price pressure. The Europeans had no legal recourse. No enforcement mechanism. The Atlantic alliance is not a binding contract with penalties for breach. It is a shared commitment. And shared commitments are only as durable as the perception of shared interest.'''
speaker = 'male_narrator_03'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_13_seg02_v3.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_13_seg02_v3.wav')
"
