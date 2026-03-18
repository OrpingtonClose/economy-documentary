#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''This is not unusual. It is normal. The Khamenei family is estimated to hold between ninety-five and two hundred billion dollars in assets hidden outside Iran. The range is wide because the secrecy is effective. The secrecy is effective because five legal jurisdictions each provided one link in a chain that none of them sees in full. This is not a failure of individual laws. It is a success of fragmentation. The sanction is a national instrument. The wealth is a global one.'''
speaker = 'male_narrator_03'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_16_seg03_v3.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_16_seg03_v3.wav')
"
