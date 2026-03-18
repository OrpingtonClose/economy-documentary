#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''March twelfth, 2026. Eleven days into the war. Fortune magazine publishes an investigation. The headline: \'Inside the Binance accounts that sent over one billion dollars to Iran-linked entities.\' We are going to spend the next nine minutes on this story. Because this story is the story of the other side of the war\'s ledger — the side that never appears in the Pentagon press conference, never appears in the CENTCOM strike count, but appears in the blockchain data with perfect clarity.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_08_seg00_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_08_seg00_v1.wav')
"
