#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''To be precise about why it didn\'t happen: stock buybacks return capital to shareholders at current valuations — immediately measurable, immediately rewarded by the market, immediately visible in quarterly earnings presentations. Production capacity expansion generates returns over five to ten year timelines, at utilization rates dependent on government contracts that may or may not materialize, in a sector where the government is the only customer and the government can change its procurement priorities between election cycles. The capital allocation decision between buyback and production investment was, from a shareholder-return perspective, entirely rational. The market rewarded the financial engineering. The executives were compensated at record levels. The investors were satisfied. The production capacity was not built.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_21_seg01_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_21_seg01_v2.wav')
"
