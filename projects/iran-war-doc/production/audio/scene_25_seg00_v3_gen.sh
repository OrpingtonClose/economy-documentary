#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''While the United States drew down its Strategic Petroleum Reserve to twelve days of consumption, China maintained a different posture. China\'s strategic petroleum reserve covers one hundred and four days of consumption. One hundred and four days — the equivalent of more than three months of warfare, three months of Strait closure, three months of energy shock that China could absorb without fundamental economic disruption. China is not unaffected by the Hormuz closure. Between forty-five and fifty-seven percent of China\'s oil imports transit the Strait. But China\'s one-hundred-and-four-day buffer means it can wait. It can absorb the initial shock. It can negotiate from a position of time, which is the most valuable position in any negotiation.'''
speaker = 'male_narrator_03'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_25_seg00_v3.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_25_seg00_v3.wav')
"
