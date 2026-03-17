#!/usr/bin/env python3
"""
Convert 42 scene visual descriptions into LTX-2.3 optimized prompts.
Each scene may need multiple clips (~5s each at 24fps/121 frames).

LTX-2.3 prompting rules:
- Single flowing paragraph, present tense
- 150-250 words per clip
- Explicit camera movement, material textures, lighting, ambient audio
- Start directly with action
- No text/letters on screen
- One action + one camera move per clip
"""

import json
import os
import re

# Global style string for visual consistency
STYLE_STRING = (
    "Photorealistic cinematic documentary footage, "
    "shot on Arri Alexa Mini with Cooke anamorphic lenses, natural film grain, "
    "shallow depth of field with subtle bokeh, "
    "desaturated cool color palette with occasional warm accents, "
    "documentary-style handheld micro-drift, "
    "ambient room tone and environmental sound"
)

def scene_to_clips(scene):
    """Convert a scene's visual description into multiple clip prompts."""
    scene_num = scene["scene_num"]
    title = scene.get("title", "")
    visual = scene.get("visual_description", "")
    duration = scene.get("duration_sec", 60)
    
    # Each clip is ~5s. We need enough clips to cover the scene.
    # But actual clip count will be determined at generation time based on narration duration.
    # Here we create prompts for multiple visual moments within the scene.
    
    # Parse causal beats if present
    beats = parse_beats(visual)
    
    # Extract the main visual narrative (before the table)
    main_desc = visual.split("#### CAUSAL BEATS")[0].strip() if "#### CAUSAL BEATS" in visual else visual
    main_desc = main_desc.split("####")[0].strip()  # Remove any other markdown headers
    
    # Create clip prompts based on the visual description
    clips = create_clip_prompts(scene_num, title, main_desc, beats, duration)
    
    return {
        "scene_num": scene_num,
        "title": title,
        "duration_sec": duration,
        "clips": clips,
        "prompt": clips[0]["prompt"] if clips else "",  # Fallback single prompt
    }

def parse_beats(visual):
    """Extract causal beats timing table from visual description."""
    beats = []
    if "#### CAUSAL BEATS" not in visual:
        return beats
    
    table_section = visual.split("#### CAUSAL BEATS")[1]
    lines = table_section.strip().split("\n")
    
    for line in lines:
        if "|" in line and not line.strip().startswith("|---") and "Time" not in line and "Narration" not in line:
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if len(parts) >= 3:
                time_str = parts[0]
                trigger = parts[1]
                action = parts[2]
                beats.append({
                    "time": time_str,
                    "trigger": trigger,
                    "visual_action": action,
                })
    
    return beats

def create_clip_prompts(scene_num, title, main_desc, beats, duration_sec):
    """Create optimized LTX-2.3 prompts for each clip in the scene."""
    
    # Estimate number of clips needed (5s each)
    n_clips = max(1, (duration_sec + 4) // 5)
    
    # If we have beats, group them into clip-sized chunks
    if beats and len(beats) >= 2:
        clips_from_beats = beats_to_clips(beats, n_clips, main_desc, scene_num)
        if clips_from_beats:
            return clips_from_beats
    
    # Otherwise, create prompts from the main description
    return description_to_clips(main_desc, n_clips, scene_num)

def beats_to_clips(beats, target_clips, main_desc, scene_num):
    """Convert causal beats into clip prompts."""
    clips = []
    
    # Group beats into roughly equal groups for target clip count
    # But cap at reasonable number of unique prompts (max 6 distinct, rest will repeat last)
    max_unique = min(target_clips, max(3, len(beats)))
    beats_per_clip = max(1, len(beats) // max_unique)
    
    for i in range(0, len(beats), beats_per_clip):
        group = beats[i:i + beats_per_clip]
        if not group:
            continue
        
        # Combine visual actions into a flowing prompt
        actions = [b["visual_action"] for b in group]
        combined_action = ". ".join(actions)
        
        # Build LTX-2.3 style prompt
        prompt = build_ltx_prompt(combined_action, main_desc, len(clips))
        
        clips.append({
            "clip_idx": len(clips) + 1,
            "prompt": prompt,
            "beats": group,
        })
    
    return clips

def description_to_clips(main_desc, n_clips, scene_num):
    """Split a visual description into multiple clip prompts."""
    # Split description into sentences
    sentences = re.split(r'(?<=[.!?])\s+', main_desc)
    sentences = [s.strip() for s in sentences if s.strip() and len(s.strip()) > 20]
    
    if not sentences:
        return [{"clip_idx": 1, "prompt": build_ltx_prompt(main_desc, main_desc, 0)}]
    
    # Group sentences into clips
    clips = []
    sents_per_clip = max(1, len(sentences) // min(n_clips, 6))
    
    for i in range(0, len(sentences), sents_per_clip):
        group = sentences[i:i + sents_per_clip]
        combined = " ".join(group)
        prompt = build_ltx_prompt(combined, main_desc, len(clips))
        clips.append({
            "clip_idx": len(clips) + 1,
            "prompt": prompt,
        })
        if len(clips) >= 6:  # Cap unique prompts
            break
    
    return clips

def build_ltx_prompt(action_desc, full_context, clip_index):
    """Build an LTX-2.3 optimized prompt from a visual action description."""
    
    # Clean up the action description
    action = action_desc.strip()
    # Remove markdown formatting
    action = re.sub(r'\*\*([^*]+)\*\*', r'\1', action)
    action = re.sub(r'\*([^*]+)\*', r'\1', action)
    # Remove any table formatting remnants
    action = re.sub(r'\|', '', action)
    # Remove references to specific timestamps
    action = re.sub(r'\d+:\d+', '', action)
    action = action.strip()
    
    # Extract camera movement hints from the action
    camera_hints = extract_camera_movement(action, full_context)
    
    # Extract lighting hints
    lighting = extract_lighting(action, full_context)
    
    # Build the final prompt
    # LTX-2.3 format: flowing paragraph, present tense, 150-250 words
    prompt_parts = []
    
    # Start with the visual action (converted to present tense)
    action_present = to_present_tense(action)
    prompt_parts.append(action_present)
    
    # Add camera movement if not already in the action
    if camera_hints and not any(kw in action.lower() for kw in ["camera", "dolly", "pan", "zoom", "tracking", "drift"]):
        prompt_parts.append(camera_hints)
    
    # Add lighting if not already described
    if lighting and not any(kw in action.lower() for kw in ["light", "shadow", "glow", "illuminate", "bright", "dark"]):
        prompt_parts.append(lighting)
    
    # Add style string
    prompt_parts.append(STYLE_STRING)
    
    prompt = ". ".join(p.rstrip(".") for p in prompt_parts if p) + "."
    
    # Ensure reasonable length (trim if over 300 words)
    words = prompt.split()
    if len(words) > 280:
        prompt = " ".join(words[:280]) + "."
    
    return prompt

def extract_camera_movement(action, context):
    """Extract or infer camera movement from the description."""
    movements = {
        "slow pull back": "The camera slowly pulls back, revealing more of the environment",
        "close-up": "Extreme close-up with shallow depth of field, subtle micro-drift",
        "zoom": "The camera smoothly zooms",
        "dolly": "The camera dollies smoothly",
        "pan": "The camera pans gently across the scene",
        "drift": "The camera drifts with documentary-style handheld movement",
        "tracking": "The camera tracks the subject with steady movement",
        "orbit": "The camera slowly orbits the subject",
        "crane": "The camera cranes upward revealing the broader scene",
    }
    
    for keyword, description in movements.items():
        if keyword in action.lower() or keyword in context.lower()[:500]:
            return ""  # Already has camera movement
    
    # Default camera movements based on clip position
    defaults = [
        "The camera holds steady with subtle documentary micro-drift, shallow depth of field",
        "The camera slowly drifts to the right, gentle handheld movement",
        "The camera pushes in gradually, tightening the frame on the subject",
        "The camera eases back slowly, widening the view of the scene",
        "The camera pans gently left to right, following the natural eye line",
        "The camera holds in a medium shot with gentle breathing movement",
    ]
    return ""  # Don't add default - let the visual description guide it

def extract_lighting(action, context):
    """Extract or infer lighting from descriptions."""
    if any(kw in action.lower() for kw in ["light", "shadow", "glow", "sun", "lamp", "bright", "dark", "morning", "evening"]):
        return ""
    return ""  # Don't add default lighting

def to_present_tense(text):
    """Rough conversion to present tense for LTX-2.3 prompts."""
    # Simple replacements - not perfect but good enough for prompts
    text = re.sub(r'\bbegan\b', 'begins', text)
    text = re.sub(r'\bstarted\b', 'starts', text)
    text = re.sub(r'\bmoved\b', 'moves', text)
    text = re.sub(r'\bpulled\b', 'pulls', text)
    text = re.sub(r'\bturned\b', 'turns', text)
    text = re.sub(r'\bopened\b', 'opens', text)
    text = re.sub(r'\bclosed\b', 'closes', text)
    text = re.sub(r'\bfell\b', 'falls', text)
    text = re.sub(r'\brose\b', 'rises', text)
    text = re.sub(r'\bshowed\b', 'shows', text)
    text = re.sub(r'\bappeared\b', 'appears', text)
    text = re.sub(r'\bfaded\b', 'fades', text)
    text = re.sub(r'\bbroadened\b', 'broadens', text)
    text = re.sub(r'\bwidened\b', 'widens', text)
    text = re.sub(r'\bnarrowed\b', 'narrows', text)
    return text

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenes", default="/home/user/workspace/scenes_parsed.json")
    parser.add_argument("--output", default="/home/user/workspace/scene_prompts.json")
    args = parser.parse_args()
    
    with open(args.scenes) as f:
        scenes = json.load(f)
    
    print(f"Converting {len(scenes)} scenes to LTX-2.3 prompts...")
    
    all_prompts = []
    total_clips = 0
    
    for scene in scenes:
        result = scene_to_clips(scene)
        all_prompts.append(result)
        n = len(result.get("clips", []))
        total_clips += n
        print(f"  Scene {result['scene_num']:2d} ({result['title'][:30]:30s}): {n} clips, ~{result['duration_sec']}s")
    
    with open(args.output, "w") as f:
        json.dump(all_prompts, f, indent=2)
    
    print(f"\nTotal: {total_clips} clips across {len(all_prompts)} scenes")
    print(f"Saved to {args.output}")

if __name__ == "__main__":
    main()
