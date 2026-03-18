#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''While the missiles were flying, something was happening on the ground in America. Gas prices, which had already been rising over the previous weeks, began their next climb. By the time the war entered its second week, the national average was up approximately seventy cents per gallon. That sounds small. That is not small. At the average American driving rate — one thousand miles per month, fuel economy of twenty-five miles per gallon — that is an extra twenty-eight dollars per month per household. Times one hundred and thirty million American households. That is three-point-six billion dollars per month in additional consumer spending on gasoline alone. Nine billion dollars per month. Extracted not by a tax, not by a vote, but by the physics of a conflict eight thousand miles away.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_03_seg00_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_03_seg00_v1.wav')
"
