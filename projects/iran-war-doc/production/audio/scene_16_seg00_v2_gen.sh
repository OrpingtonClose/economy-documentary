#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''Bloomberg\'s investigation reconstructed the financial architecture with specificity. Step one: Iranian oil sales, routed through a UAE-based intermediary — the UAE, which maintains formal relations with Iran despite international pressure, functioning as a commercial valve between the Iranian economy and the global financial system. Step two: funds moved through the Abu Dhabi Islamic Bank. Step three: Ziba Leisure Ltd., incorporated in Saint Kitts and Nevis — a Caribbean tax haven with minimal beneficial ownership disclosure requirements and a visa program that offers citizenship in exchange for investment. Step four: from Ziba Leisure, funds capitalized a German company — Allsco Gravenbruch Hotelbetriebsgesellschaft mbH. Step five: that German company controls the management agreement for the Hilton Frankfurt Gravenbruch, one of Germany\'s finest five-star hotels, operating normally, welcoming guests, generating returns — today.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_16_seg00_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_16_seg00_v2.wav')
"
