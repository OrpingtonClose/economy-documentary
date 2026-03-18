#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''March twelfth. Day eleven of the war. Gas is at three dollars and seventy-two cents per gallon — the number I asked you to hold. Oil has crossed one hundred dollars per barrel. The Strategic Petroleum Reserve release — one hundred seventy-two million barrels, the largest in history — has not been sufficient to bring prices back to pre-war levels. Spring break starts in four days. Tens of millions of American families are about to drive to beaches, to relatives, to vacation destinations they booked before the war. The administration faces a simple political reality: American families on vacation are paying seventy cents more per gallon than they were three weeks ago. That is visible. That is felt in the wallet. That votes in November.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_12_seg00_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_12_seg00_v1.wav')
"
