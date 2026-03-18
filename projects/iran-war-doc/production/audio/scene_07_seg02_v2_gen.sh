#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Legal does not mean uninformed. The question the STOCK Act was written to prevent was: did you trade because you knew something the market didn\'t? The question it was not written to answer is: what does it do to the incentive structure of national security decision-making when the people voting on war have a financial stake in war\'s outcome? If a vote for military action will increase the value of your portfolio, and a vote against it will not, is the vote unaffected? The STOCK Act does not think this question is relevant. Behavioral economics disagrees.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_07_seg02_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_07_seg02_v2.wav')
"
