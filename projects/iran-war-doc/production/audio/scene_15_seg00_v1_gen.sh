#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The Bishops Avenue, north London. Locally known as Billionaire\'s Row. It is one of the most expensive residential streets in the world — multi-million-pound mansions set back behind stone walls and security gates, properties from the Gulf, from Russia, from China, from everywhere that great wealth accumulates and seeks a permanent, stable home in a jurisdiction with reliable property rights and strong rule of law. One of its most valuable residents — property purchased in 2014 for thirty-three-point-seven million pounds — belongs, according to Bloomberg\'s March eighth investigation, to a corporate structure connected to Mojtaba Khamenei. The son of the assassinated Supreme Leader. The new Supreme Leader of the Islamic Republic of Iran. A man whose country was being struck by Tomahawk cruise missiles at three-point-five million dollars each while his London property empire — exceeding one hundred million pounds total — sat untouched, un-sanctioned as property, and legally protected by the same British legal system whose government was diplomatically supporting the military campaign against his country.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_15_seg00_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_15_seg00_v1.wav')
"
