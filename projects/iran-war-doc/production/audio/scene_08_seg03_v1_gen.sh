#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Three people stand at the center of this specific network. The first: a seventy-nine-year-old Chinese woman, identified in the investigation as a VIP customer of Binance\'s Chinese operations. She moved four hundred and thirty-nine million dollars. A seventy-nine-year-old woman. Four hundred and thirty-nine million dollars. To the IRGC. Using an app on a phone. The second: a thirty-eight-year-old Chinese woman who moved two hundred million dollars through the same architecture. The third: a forty-four-year-old Iranian gold smuggler named in a 2020 UN Security Council report. Three people. Six hundred and thirty-nine million dollars. One Binance account cluster.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_08_seg03_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_08_seg03_v1.wav')
"
