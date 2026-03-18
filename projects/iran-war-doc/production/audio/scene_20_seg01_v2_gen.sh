#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The STOCK Act, passed in 2012, requires members of Congress to disclose trades within forty-five days. It does not require divestment from industries they oversee. It does not bar a member of the Armed Services Committee from voting for a defense budget — or a supplemental war appropriations bill — that benefits a company in whose stock they hold a position. The rationale for this permissiveness is separation of powers: Congress argues that requiring divestment would deter qualified people from public service. The consequence is a legislative body in which the people deciding the size of the defense budget simultaneously hold financial stakes in the companies receiving that budget.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_20_seg01_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_20_seg01_v2.wav')
"
