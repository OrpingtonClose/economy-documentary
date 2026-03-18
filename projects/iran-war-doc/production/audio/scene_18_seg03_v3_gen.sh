#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''There is a phrase from the First World War era: \'merchants of death.\' It was used by critics of the munitions industry to describe companies that profited from warfare. The phrase fell out of use because it was too simplistic — defense manufacturing is complicated, dual-use technology is real, and the people who work in these industries are not villains. What it captured, imprecisely but not inaccurately, was the structural reality that for some participants, war is not a cost. War is a revenue event. The White House meeting on March sixth was a revenue event, conducted at the highest levels of American government, three days into a war.'''
speaker = 'male_narrator_03'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_18_seg03_v3.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_18_seg03_v3.wav')
"
