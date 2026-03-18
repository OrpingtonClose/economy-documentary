#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''So the administration makes a decision that would have been unthinkable forty-eight hours into a war against Iran — a country that is Russia\'s strategic ally, a country that Russia is supplying with intelligence and support throughout the conflict. On March twelfth, Treasury Secretary Scott Bessent authorizes the sale of Russian oil already loaded on tankers and quote \'stranded at sea.\' The specific framing — stranded at sea, no significant financial benefit to Russia — is precisely chosen regulatory language, designed to navigate OFAC restrictions by arguing that the oil was effectively already sold, already in transit, and that refusing the sale would benefit nobody. The OFAC exemption is technical. The political reality is not.'''
speaker = 'male_narrator_01'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_12_seg01_v1.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_12_seg01_v1.wav')
"
