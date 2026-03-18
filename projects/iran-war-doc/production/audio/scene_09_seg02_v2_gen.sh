#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Add the shadow banking network: an estimated nine billion dollars per year in Iranian financial flows moving through informal value transfer systems. Hawala networks connecting Iranian diaspora to the homeland. UAE-registered front companies transacting in Gulf currencies. Real estate transactions in Dubai serving as value stores outside the formal financial system. The aggregate picture: total accessible war financing available to Iran at the conflict\'s start between twenty and thirty billion dollars. Enough for three to six months of sustained military operations at pre-war expenditure rates. The United States was spending one-point-five billion dollars per day bombing a country that had twenty to thirty billion dollars in reserve financing that the bombing could not reach.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_09_seg02_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_09_seg02_v2.wav')
"
