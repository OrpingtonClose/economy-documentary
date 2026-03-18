#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The cargo rates followed immediately. Asia-to-Europe container rates — the commercial blood supply of the global supply chain, the price signal that determines when goods move from factory to store shelf — jumped nineteen percent. Air freight rates soared as shippers diverted from maritime to airborne transport for time-sensitive cargo. The cost of that diversion passed directly to consumers in the form of higher product prices. Companies that had built just-in-time supply chains assuming stable ocean shipping faced margin compression across every product category: electronics, pharmaceuticals, automotive parts, textiles, industrial components. The war found them in their shipping lanes.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_23_seg01_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_23_seg01_v1.wav')
"
