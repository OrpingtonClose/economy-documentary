#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Think about what that means structurally. A member of the Armed Services Committee receives classified briefings on US military readiness, on potential conflict scenarios, on the specific weapons systems that would be used in a specific regional conflict. That member is not legally barred from holding stock in the companies that make those weapons systems. The information in the briefing — even if it strongly implies a coming conflict — is not \'material non-public information\' in the SEC\'s definition, because it is about military planning, not about corporate earnings. The law has a gap in it. The gap is where the money goes.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_07_seg01_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_07_seg01_v1.wav')
"
