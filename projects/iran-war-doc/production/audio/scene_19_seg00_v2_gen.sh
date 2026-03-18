#!/bin/bash

python3 -c "
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer
import soundfile as sf

model_path = '/workspace/models/Qwen3-TTS'
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(model_path, torch_dtype=torch.bfloat16, device_map='auto', trust_remote_code=True)

text = '''The contracts that result from that meeting will be awarded through the Department of Defense\'s acquisition process. The official who co-chairs the Defense Acquisition Board — the body that approves major defense contracts — is Steve Feinberg, Deputy Secretary of Defense. Before his government appointment, Feinberg was the founder and CEO of Cerberus Capital Management, one of the largest private equity firms in the United States, managing over sixty billion dollars in assets. Cerberus has historically held substantial positions in defense-related companies and industries adjacent to defense contracting. Feinberg\'s transition to government included a partial divestiture process — he recused himself from specific identified conflicts of interest. He remained in his role for the rest.'''
speaker = 'male_narrator_02'

# Generate speech
inputs = tokenizer(f'<|speaker|>{speaker}<|text|>{text}<|endoftext|>', return_tensors='pt').to(model.device)
with torch.no_grad():
    outputs = model.generate(**inputs, max_new_tokens=4096)
audio = outputs  # Decode to audio tensor

sf.write('/home/user/workspace/iran-war-doc/production/audio/scene_19_seg00_v2.wav', audio.cpu().numpy(), 24000)
print(f'Generated: /home/user/workspace/iran-war-doc/production/audio/scene_19_seg00_v2.wav')
"
