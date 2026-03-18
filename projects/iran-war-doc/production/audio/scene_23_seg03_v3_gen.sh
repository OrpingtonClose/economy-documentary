#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''There is a concept in systems theory called chokepoint amplification — the disproportionate systemic consequence of disrupting a single critical node. The Strait of Hormuz is the world\'s most consequential chokepoint. A single twenty-one-mile waterway carries one-fifth of global energy. That concentration of consequence in a single geography is not an accident of nature — it is the result of decades of infrastructure investment decisions made in a period of relative stability. The world built the system assuming the Strait would remain open. The war demonstrated the assumption.'''
speaker = 'male_narrator_03'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_23_seg03_v3.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_23_seg03_v3.wav')
"
