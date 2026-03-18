#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Now for the part that is legal. Under the STOCK Act of 2012, members of Congress are prohibited from trading on material non-public information. They are required to disclose trades within forty-five days. What they are not prohibited from doing — what the law explicitly allows — is holding stocks in industries they oversee. Defense contractors. Aerospace companies. Oil. So when members of the House Foreign Affairs Committee, the House Armed Services Committee, and the Senate Intelligence Committee added positions in Lockheed Martin, RTX, L3Harris, and Northrop Grumman in early 2025 — and then held those positions through the eight-week war run-up — and then watched those positions hit all-time highs in the first week of March 2026 — every single one of those trades was legal. Disclosed, in some cases. Legal, in all of them.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_07_seg00_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_07_seg00_v1.wav')
"
