#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Here is what we know from the Center for Strategic and International Studies war cost analysis, published March thirteenth. Day one and two: five-point-six billion dollars. Three hundred and nineteen Tomahawk cruise missiles. Each one costs three-point-five million dollars. That is three-point-five million dollars of machined steel and guidance systems and solid-fuel propellant, designed to be used exactly once. More than one hundred fifty THAAD interceptors, each one costing between twelve and fifteen million dollars. And Patriot missile systems, depleted by fourteen-point-six percent of total US stockpile. In two days. The United States used fourteen-point-six percent of its Patriot inventory in forty-eight hours. The Patriot production line cannot replace that inventory until April 2027 at minimum.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_02_seg00_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_02_seg00_v2.wav')
"
