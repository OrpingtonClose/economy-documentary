#!/bin/bash
set -e

echo '--- Generating scene_26_clip10_sub00.mp4 ---'

python3 -c "
import torch
from diffusers import LTXPipeline

pipe = LTXPipeline.from_pretrained(
    '/workspace/models/ltx-video-2.3',
    torch_dtype=torch.bfloat16
).to('cuda')

prompt = '''Long shot down ordinary city block at dusk; people walking home. dramatic cinematic lighting, high contrast shadows. High aerial on Strait; rapid associative cuts (each 5–8 seconds); return to aerial; gentle cuts to street; final pull back. cinematic documentary, photorealistic, shot on Arri Alexa, 16:9 widescreen, shallow depth of field, anamorphic lens flare, dramatic lighting, high contrast, subtle film grain, color palette: #8090A0, #D08030, #1A2A4A'''

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
export_to_video(video, '/home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub00.mp4', fps=24)
print(f'Generated: /home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub00.mp4')
"


echo '--- Generating scene_26_clip10_sub01.mp4 ---'

# Extract last frame from previous clip
ffmpeg -y -sseof -0.1 -i /home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub00.mp4 -frames:v 1 -q:v 2 /home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub00_lastframe.jpg

python3 -c "
import torch
from diffusers import LTXImageToVideoPipeline
from PIL import Image

pipe = LTXImageToVideoPipeline.from_pretrained(
    '/workspace/models/ltx-video-2.3',
    torch_dtype=torch.bfloat16
).to('cuda')

image = Image.open('/home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub00_lastframe.jpg')
# Continue the scene - describe motion continuation, not the static scene
continuation_prompt = 'Camera continues moving, scene continues naturally. Long shot down ordinary city block at dusk; people walking home. dramatic cinematic lighting, high contrast shadows. High aerial on Strait; rapid associative cuts (each 5–8 seconds); return to aerial; gentle cuts to street; final pull back. cinematic documentary, photorealistic, shot on Arri Alexa, 16:9 widescreen, shallow depth of field, anamorphic lens flare, dramatic lighting, high contrast, subtle film grain, color palette: #8090A0, #D08030, #1A2A4A'

video = pipe(
    prompt=continuation_prompt,
    image=image,
    negative_prompt='blurry, low quality, text, watermark, letters, words, subtitles, logo, static, frozen, looping',
    width=1280,
    height=720,
    num_frames=121,
    guidance_scale=3.5,
    num_inference_steps=50,
).frames[0]

from diffusers.utils import export_to_video
export_to_video(video, '/home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub01.mp4', fps=24)
print(f'Generated: /home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub01.mp4')
"


echo '--- Generating scene_26_clip10_sub02.mp4 ---'

# Extract last frame from previous clip
ffmpeg -y -sseof -0.1 -i /home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub01.mp4 -frames:v 1 -q:v 2 /home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub01_lastframe.jpg

python3 -c "
import torch
from diffusers import LTXImageToVideoPipeline
from PIL import Image

pipe = LTXImageToVideoPipeline.from_pretrained(
    '/workspace/models/ltx-video-2.3',
    torch_dtype=torch.bfloat16
).to('cuda')

image = Image.open('/home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub01_lastframe.jpg')
# Continue the scene - describe motion continuation, not the static scene
continuation_prompt = 'Camera continues moving, scene continues naturally. Long shot down ordinary city block at dusk; people walking home. dramatic cinematic lighting, high contrast shadows. High aerial on Strait; rapid associative cuts (each 5–8 seconds); return to aerial; gentle cuts to street; final pull back. cinematic documentary, photorealistic, shot on Arri Alexa, 16:9 widescreen, shallow depth of field, anamorphic lens flare, dramatic lighting, high contrast, subtle film grain, color palette: #8090A0, #D08030, #1A2A4A'

video = pipe(
    prompt=continuation_prompt,
    image=image,
    negative_prompt='blurry, low quality, text, watermark, letters, words, subtitles, logo, static, frozen, looping',
    width=1280,
    height=720,
    num_frames=121,
    guidance_scale=3.5,
    num_inference_steps=50,
).frames[0]

from diffusers.utils import export_to_video
export_to_video(video, '/home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub02.mp4', fps=24)
print(f'Generated: /home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub02.mp4')
"


echo '--- Generating scene_26_clip10_sub03.mp4 ---'

# Extract last frame from previous clip
ffmpeg -y -sseof -0.1 -i /home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub02.mp4 -frames:v 1 -q:v 2 /home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub02_lastframe.jpg

python3 -c "
import torch
from diffusers import LTXImageToVideoPipeline
from PIL import Image

pipe = LTXImageToVideoPipeline.from_pretrained(
    '/workspace/models/ltx-video-2.3',
    torch_dtype=torch.bfloat16
).to('cuda')

image = Image.open('/home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub02_lastframe.jpg')
# Continue the scene - describe motion continuation, not the static scene
continuation_prompt = 'Camera continues moving, scene continues naturally. Long shot down ordinary city block at dusk; people walking home. dramatic cinematic lighting, high contrast shadows. High aerial on Strait; rapid associative cuts (each 5–8 seconds); return to aerial; gentle cuts to street; final pull back. cinematic documentary, photorealistic, shot on Arri Alexa, 16:9 widescreen, shallow depth of field, anamorphic lens flare, dramatic lighting, high contrast, subtle film grain, color palette: #8090A0, #D08030, #1A2A4A'

video = pipe(
    prompt=continuation_prompt,
    image=image,
    negative_prompt='blurry, low quality, text, watermark, letters, words, subtitles, logo, static, frozen, looping',
    width=1280,
    height=720,
    num_frames=121,
    guidance_scale=3.5,
    num_inference_steps=50,
).frames[0]

from diffusers.utils import export_to_video
export_to_video(video, '/home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub03.mp4', fps=24)
print(f'Generated: /home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub03.mp4')
"


echo '--- Generating scene_26_clip10_sub04.mp4 ---'

# Extract last frame from previous clip
ffmpeg -y -sseof -0.1 -i /home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub03.mp4 -frames:v 1 -q:v 2 /home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub03_lastframe.jpg

python3 -c "
import torch
from diffusers import LTXImageToVideoPipeline
from PIL import Image

pipe = LTXImageToVideoPipeline.from_pretrained(
    '/workspace/models/ltx-video-2.3',
    torch_dtype=torch.bfloat16
).to('cuda')

image = Image.open('/home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub03_lastframe.jpg')
# Continue the scene - describe motion continuation, not the static scene
continuation_prompt = 'Camera continues moving, scene continues naturally. Long shot down ordinary city block at dusk; people walking home. dramatic cinematic lighting, high contrast shadows. High aerial on Strait; rapid associative cuts (each 5–8 seconds); return to aerial; gentle cuts to street; final pull back. cinematic documentary, photorealistic, shot on Arri Alexa, 16:9 widescreen, shallow depth of field, anamorphic lens flare, dramatic lighting, high contrast, subtle film grain, color palette: #8090A0, #D08030, #1A2A4A'

video = pipe(
    prompt=continuation_prompt,
    image=image,
    negative_prompt='blurry, low quality, text, watermark, letters, words, subtitles, logo, static, frozen, looping',
    width=1280,
    height=720,
    num_frames=121,
    guidance_scale=3.5,
    num_inference_steps=50,
).frames[0]

from diffusers.utils import export_to_video
export_to_video(video, '/home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub04.mp4', fps=24)
print(f'Generated: /home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_sub04.mp4')
"




cat > /home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_concat.txt << 'CONCATEOF'
file 'scene_26_clip10_sub00.mp4'
file 'scene_26_clip10_sub01.mp4'
file 'scene_26_clip10_sub02.mp4'
file 'scene_26_clip10_sub03.mp4'
file 'scene_26_clip10_sub04.mp4'
CONCATEOF
ffmpeg -y -f concat -safe 0 -i /home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_concat.txt -c copy /home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_raw.mp4

# Trim to exact target duration
ffmpeg -y -i /home/user/workspace/iran-war-doc/production/clips/scene_26_clip10_raw.mp4 -t 25 -c copy /home/user/workspace/iran-war-doc/production/clips/scene_26_clip10.mp4
echo "Final clip: scene_26_clip10.mp4 (trimmed to 25s)"

