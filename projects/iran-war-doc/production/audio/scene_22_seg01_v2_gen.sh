#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Twelve days. Two hundred and forty-three million barrels covers twelve days of US consumption. That is the strategic buffer the United States maintained at the end of two weeks of a regional conflict. If the Strait of Hormuz remained closed for more than twelve days after the SPR was effectively exhausted — or if another supply shock hit simultaneously — the United States would have no strategic petroleum buffer remaining. None. The reserve was designed to last through a crisis. It had been drawn down to where it could only sustain the beginning of the next one.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_22_seg01_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_22_seg01_v2.wav')
"
