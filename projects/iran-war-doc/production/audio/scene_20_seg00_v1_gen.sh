#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The House Armed Services Committee has jurisdiction over defense policy, authorization of weapons systems, and the legal framework for military operations. The Senate Defense Appropriations Subcommittee controls the money — the actual budget authorizations that turn White House production mandates into funded contracts. Their members vote on defense budgets, contractor authorizations, supplemental appropriations, and war powers resolutions. In the weeks before and after the Iran War began, multiple members of these committees held significant positions in the defense companies whose stocks were rising to all-time highs.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_20_seg00_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_20_seg00_v1.wav')
"
