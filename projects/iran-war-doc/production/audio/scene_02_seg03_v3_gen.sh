#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The Roman historian Vegetius wrote, in the fifth century, that \'whoever wants peace must prepare for war.\' What he did not calculate — what no war theorist ever quite calculates — is the opportunity cost. Sixteen-point-five billion dollars in twelve days. That is three years of the national school lunch program. That is the entire annual budget of the National Institutes of Health. That is one thousand four hundred elementary schools, built, equipped, staffed for a year. The missiles flew. The schools were not built. That is also a decision — and someone made it.'''
speaker = 'male_narrator_03'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_02_seg03_v3.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_02_seg03_v3.wav')
"
