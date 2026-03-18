#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The European Union\'s response to the Bessent decision was immediate and sharp. Multiple EU member governments — including Germany, France, and the Baltic states — issued formal objections within forty-eight hours. NATO allies who were simultaneously providing intelligence support and logistics assistance for the US operation against Iran found themselves being asked to publicly call on Washington to reverse a decision that Washington had already made. The argument was simple and unanswerable: you are fighting a war against Russia\'s strategic ally. You are simultaneously paying Russia. These two positions are in direct contradiction with each other.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_13_seg00_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_13_seg00_v2.wav')
"
