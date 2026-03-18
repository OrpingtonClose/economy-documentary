#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''There is a word for the condition in which a military burns through consumable inventory faster than its industrial base can replace it. The word is \'depletion.\' And depletion, in a market economy, is an opportunity. Every Tomahawk fired was a Tomahawk that needed to be replaced at emergency-production pricing. Every THAAD interceptor launched was a contract waiting to be written. The war was simultaneously destroying assets and generating the demand for their replacement. The beneficiaries of that demand had been building positions for eight weeks before the first shot.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_02_seg02_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_02_seg02_v2.wav')
"
