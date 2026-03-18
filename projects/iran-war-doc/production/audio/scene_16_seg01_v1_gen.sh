#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The key individual in this architecture: Ali Ansari. A UK-sanctioned Iranian businessman with a Cypriot passport — Cyprus being a EU member state with a controversial investment-for-citizenship program. The UK sanctions designation exists on paper. The Cypriot passport was under investigation for potential revocation as of Bloomberg\'s reporting. Yet the shell company network Ansari is connected to continues to operate. The sanction designates the individual. The shell company holds the asset. The individual does not hold the asset. The sanction is the designation. The shell company is the delivery system. These two things exist in parallel, legally.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_16_seg01_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_16_seg01_v1.wav')
"
