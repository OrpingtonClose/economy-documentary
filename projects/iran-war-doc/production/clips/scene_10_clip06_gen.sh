#!/bin/bash
set -e

echo '--- Generating scene_10_clip06_sub00.mp4 ---'

python3 -c "
import torch
from diffusers import LTXPipeline

pipe = LTXPipeline.from_pretrained(
    '/workspace/models/ltx-video-2.3',
    torch_dtype=torch.bfloat16
).to('cuda')

prompt = '''Newspaper page, shape visible, date in header region; eleven days. dramatic cinematic lighting, high contrast shadows. Slow corridor walk; locked at door with dark room; cut to server room (locked); cut to newspaper (held). cinematic documentary, photorealistic, shot on Arri Alexa, 16:9 widescreen, shallow depth of field, anamorphic lens flare, dramatic lighting, high contrast, subtle film grain, color palette: #E8ECF4, #C08040, #080808, #30C0D8'''

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
export_to_video(video, '/home/user/workspace/iran-war-doc/production/clips/scene_10_clip06_sub00.mp4', fps=24)
print(f'Generated: /home/user/workspace/iran-war-doc/production/clips/scene_10_clip06_sub00.mp4')
"




# Single clip — trim to target duration
ffmpeg -y -i /home/user/workspace/iran-war-doc/production/clips/scene_10_clip06_sub00.mp4 -t 5 -c copy /home/user/workspace/iran-war-doc/production/clips/scene_10_clip06.mp4

