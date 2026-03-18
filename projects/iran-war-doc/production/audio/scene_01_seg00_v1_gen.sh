#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Five point six billion dollars. That is how much the United States spent in the first forty-eight hours of the Iran War. Not borrowed against. Spent. Ignited. Transformed into kinetic energy and falling buildings and a column of smoke you could see from orbit. Five-point-six billion dollars. To put that in context: that is more than NASA\'s entire annual budget. It is more than the GDP of Iceland. It is more than the entire annual operating budget of every public school district in the state of Texas combined. In forty-eight hours.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_01_seg00_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_01_seg00_v1.wav')
"
