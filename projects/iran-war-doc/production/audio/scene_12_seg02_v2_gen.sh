#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The economics: approximately seven to ten million barrels of Russian crude at one hundred dollars or more per barrel. Between seven hundred million and one billion dollars gross revenue to Russian oil sellers, at minimum. The official American estimate — \'no significant financial benefit to Russia\' — relies on a narrow accounting of the specific transaction. Zelenskyy\'s estimate of the total financial benefit to Russia, across all associated flows and market signals: ten billion dollars for the war. One of these estimates was produced by people with a reason to minimize the number. The other was produced by someone watching his country being invaded by the state receiving the payment.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_12_seg02_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_12_seg02_v2.wav')
"
