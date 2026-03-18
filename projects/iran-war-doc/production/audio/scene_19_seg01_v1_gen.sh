#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Let me be precise about what this is and is not. This is not a claim of illegal conduct. The recusal process is the legally prescribed mechanism for managing exactly this kind of situation, and Feinberg followed it. What this is a description of is a structural condition: a man who built his considerable personal wealth through investment in defense-sector companies was appointed to the most senior acquisition role in the Department of Defense, at the precise moment when that department was issuing the largest emergency defense contracts in a decade. The system designed to manage the conflict — the recusal — manages specific named conflicts. It does not, and cannot, manage the systemic fact of a procurement official whose professional network, whose fund investors, whose former colleagues, and whose general frame of reference are embedded in the defense investment world.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_19_seg01_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_19_seg01_v1.wav')
"
