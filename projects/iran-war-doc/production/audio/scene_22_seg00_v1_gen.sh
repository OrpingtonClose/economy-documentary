#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Here is what happened to the Strategic Petroleum Reserve in the first two weeks of the Iran War. Before the conflict began: roughly four hundred and fifteen million barrels of crude oil stored in underground salt caverns along the Gulf Coast of Louisiana and Texas. The reserve exists for strategic purposes — supply disruption, national emergency, energy security during conflict. In the first two weeks of the war, the administration released one hundred and seventy-two million barrels — the largest single-nation SPR release in US history, coordinated with thirty-two IEA member countries releasing a combined four hundred million barrels globally. After the release: two hundred and forty-three million barrels remaining in the US Strategic Petroleum Reserve. Thirty-five percent of total SPR capacity. The lowest level since 1982 — forty-four years.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_22_seg00_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_22_seg00_v1.wav')
"
