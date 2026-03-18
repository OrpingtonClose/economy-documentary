#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''There is a specific kind of institutional irony available only to large democracies — the irony of a state that simultaneously funds both sides of the same war. Not through conspiracy, not through any single identifiable bad actor, but through the accumulation of individually logical decisions: pardon the crypto founder because the legal case was complex and the political benefit was real; delay the investigation because the regulatory gap was known and not yet closed; fail to screen the stablecoin flows because the technology outpaced the compliance architecture; bomb the IRGC because the security justification was sound. Each decision, individually, rational. The aggregate: the United States government was financing the operation it was simultaneously bombing. At the margin. Through the financial system. Legally.'''
speaker = 'male_narrator_03'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_10_seg03_v3.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_10_seg03_v3.wav')
"
