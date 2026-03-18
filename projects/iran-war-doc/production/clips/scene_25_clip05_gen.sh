#!/bin/bash
set -e

echo '--- Generating scene_25_clip05_sub00.mp4 ---'

python3 -c "
import torch
from diffusers import LTXPipeline

pipe = LTXPipeline.from_pretrained(
    '/workspace/models/ltx-video-2.3',
    torch_dtype=torch.bfloat16
).to('cuda')

prompt = '''Back to global map: China deep blue, US amber; both in frame; hold. dramatic cinematic lighting, high contrast shadows. Global map rotation; close on regional color changes; agricultural field wide handheld; back to map. cinematic documentary, photorealistic, shot on Arri Alexa, 16:9 widescreen, shallow depth of field, anamorphic lens flare, dramatic lighting, high contrast, subtle film grain, color palette: #0A1020, #E8D090, #1840A0, #D09030'''

video = pipe(
    prompt=prompt,
    negative_prompt='blurry, low quality, text, watermark, letters, words, subtitles, logo, static, frozen, looping',
    width=1280,
    height=720,
    num_frames=121,
    guidance_scale=3.5,
    num_inference_steps=50,
).frames[0]

from diffusers.utils import export_to_video
export_to_video(video, '/home/user/workspace/iran-war-doc/production/clips/scene_25_clip05_sub00.mp4', fps=24)
print(f'Generated: /home/user/workspace/iran-war-doc/production/clips/scene_25_clip05_sub00.mp4')
"




# Single clip — trim to target duration
ffmpeg -y -i /home/user/workspace/iran-war-doc/production/clips/scene_25_clip05_sub00.mp4 -t 5 -c copy /home/user/workspace/iran-war-doc/production/clips/scene_25_clip05.mp4

