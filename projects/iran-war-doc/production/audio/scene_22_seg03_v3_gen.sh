#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The Strategic Petroleum Reserve was established in 1975, in the wake of the 1973 Arab oil embargo, as a permanent lesson in strategic foresight — the idea that a great power should maintain energy independence sufficient to outlast a supply disruption without economic collapse. In 2026, its use as a domestic political instrument — a tool for managing pump prices during an election-sensitive vacation week — represents, in some sense, the reserve\'s original strategic premise being consumed by a different kind of strategy.'''
speaker = 'male_narrator_03'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_22_seg03_v3.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_22_seg03_v3.wav')
"
