#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Specific commitments. THAAD production: from ninety-six interceptors per year to four hundred per year — more than a four-fold increase, requiring new production lines, new workforce, new supply chain capacity. Tomahawk cruise missile production: to exceed one thousand per year, from a base that had already been running below demand. These are not incremental adjustments. These are multi-year industrial transformation orders. They require capital investment. They require facility expansion. They require long-term supply chain contracts with hundreds of sub-tier suppliers. They require, above all, long-term contract certainty from the government — the kind that generates predictable revenue streams and predictable margins for five, ten, fifteen years.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_18_seg01_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_18_seg01_v2.wav')
"
