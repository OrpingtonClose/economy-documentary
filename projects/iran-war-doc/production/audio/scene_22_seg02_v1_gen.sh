#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The refill cost at one hundred dollars or more per barrel: approximately twenty billion dollars. Twenty billion dollars in deferred obligations, sitting in the national accounts, waiting to be paid by future taxpayers who had no vote in the decision to draw the reserve down. The IEA release moderated prices. That was its stated purpose, and it worked: gas came down a few cents. Spring break was manageable. The twenty-billion-dollar refill bill was not mentioned in the press conference announcing the release. It was not the point. The point was three-seventy-two at the pump. The twenty billion is somebody else\'s problem, scheduled for a future somebody.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_22_seg02_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_22_seg02_v1.wav')
"
