#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''According to data in the Fortune investigation, one-point-seven billion dollars flowed through Binance to what researchers call \'Entity A\' — a cluster of crypto wallet addresses connected to Iran\'s Islamic Revolutionary Guard Corps and, separately, to the Houthis in Yemen who were firing on international shipping in the Red Sea. The mechanism: Tether stablecoins — digital tokens that trade one-to-one with the US dollar, fully convertible, globally liquid — moved along the Tron blockchain. Tron is fast. Tron is cheap. Tron\'s transaction fees are measured in fractions of a cent. And Tron, at the time of these flows, had limited real-time sanctions screening capability compared to the more regulated Ethereum network. One-point-seven billion dollars. Moving in transactions as small as a few thousand dollars, aggregating through hundreds of wallets into a single operational war chest.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_08_seg01_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_08_seg01_v2.wav')
"
