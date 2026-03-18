#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Goldman Sachs research, published March twelfth, 2026: the bank cuts its US economic outlook. Recession probability over twelve months: twenty-five percent. Up from an already elevated pre-war baseline. The direct mechanism is simple: every ten percent rise in oil prices adds approximately two-tenths of a percentage point to the Consumer Price Index. Oil had risen more than ten percent from pre-war levels. The inflationary addition was already in the pipeline — in the literal sense that the oil was flowing at the new price, and the price was already embedded in supply contracts and futures hedges that would not be renegotiated until they expired.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_24_seg00_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_24_seg00_v1.wav')
"
