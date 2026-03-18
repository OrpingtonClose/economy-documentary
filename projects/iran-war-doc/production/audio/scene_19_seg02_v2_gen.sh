#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''There is a word in economics for when the people who award contracts have a systematic financial interest in the outcome of those contracts. The word is not \'corruption.\' It is not \'fraud.\' The word is \'misaligned incentives.\' The distinction matters enormously in criminal law. It matters less in outcomes. The outcome of a system with misaligned incentives — even a perfectly legal one — consistently favors the party whose incentives are aligned with the decision. In this case, the contracts go to the defense sector. Steve Feinberg\'s professional network is in the defense sector. These two facts coexist within the law. Whether they coexist within the spirit of public service is a question the law does not adjudicate.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_19_seg02_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_19_seg02_v2.wav')
"
