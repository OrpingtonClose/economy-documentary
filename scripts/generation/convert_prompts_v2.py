#!/usr/bin/env python3
"""
Convert 42 scene visual descriptions into LTX-2.3 optimized prompts (v2).
Creates rich 150-250 word prompts by combining beat actions with the scene's
full visual narrative context.

LTX-2.3 prompting rules:
- Single flowing paragraph, present tense, 150-250 words
- Explicit camera movement, material textures, lighting, ambient audio
- Start directly with action
- One primary action + one camera move per clip
"""

import json
import re

STYLE_STRING = (
    "Photorealistic cinematic documentary footage shot on Arri Alexa Mini "
    "with Cooke anamorphic lenses, natural film grain, "
    "shallow depth of field with subtle bokeh, "
    "desaturated cool color palette with warm amber accents, "
    "documentary-style gentle handheld micro-drift"
)

def parse_beats(visual):
    """Extract causal beats table."""
    beats = []
    if "#### CAUSAL BEATS" not in visual:
        return beats
    table_section = visual.split("#### CAUSAL BEATS")[1]
    for line in table_section.strip().split("\n"):
        if "|" in line and not line.strip().startswith("|---") and "Time" not in line and "Narration Trigger" not in line:
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if len(parts) >= 3:
                beats.append({
                    "time": parts[0],
                    "trigger": parts[1],
                    "visual_action": parts[2],
                })
    return beats

def get_narrative_context(visual):
    """Extract the rich narrative description before the beats table."""
    main = visual.split("#### CAUSAL BEATS")[0] if "#### CAUSAL BEATS" in visual else visual
    main = main.split("####")[0].strip()
    # Clean markdown
    main = re.sub(r'\*\*([^*]+)\*\*', r'\1', main)
    main = re.sub(r'\*([^*]+)\*', r'\1', main)
    main = re.sub(r'\\n', ' ', main)
    main = re.sub(r'\s+', ' ', main).strip()
    return main

def enrich_prompt(beat_action, narrative_context, scene_title, clip_idx, total_clips):
    """Create a rich 150-250 word LTX-2.3 prompt from a beat action + context."""
    
    # Extract relevant sentences from narrative that relate to this beat's content
    context_sentences = narrative_context.split('. ')
    
    # Keywords from the beat action to find relevant context
    beat_words = set(beat_action.lower().split())
    relevant_context = []
    for sent in context_sentences:
        sent_words = set(sent.lower().split())
        overlap = len(beat_words & sent_words)
        if overlap >= 2 and len(sent) > 30:
            relevant_context.append(sent.strip())
    
    # Build the prompt
    parts = []
    
    # Start with the specific beat action, enriched
    action = beat_action.strip().rstrip('.')
    # Remove timestamp references
    action = re.sub(r'\d+:\d+', '', action).strip()
    # Clean markdown
    action = re.sub(r'\*\*([^*]+)\*\*', r'\1', action)
    action = re.sub(r'\*([^*]+)\*', r'\1', action)
    
    parts.append(action)
    
    # Add relevant narrative context (1-2 sentences max)
    for ctx in relevant_context[:2]:
        if len(ctx) > 30 and ctx not in action:
            parts.append(ctx.strip().rstrip('.'))
    
    # Add environmental/atmospheric details based on common scene elements
    if any(kw in action.lower() for kw in ['table', 'kitchen', 'coffee', 'mug', 'jar', 'newspaper']):
        parts.append("Warm morning light slants through a window casting long shadows across worn wooden surfaces, "
                     "dust particles visible in the light beam, steam rising gently from a coffee mug, "
                     "the rich texture of oak grain visible under soft directional light")
    elif any(kw in action.lower() for kw in ['pump', 'gas', 'nozzle', 'station', 'fuel']):
        parts.append("Cold gray morning light reflects off chrome and stainless steel surfaces, "
                     "the amber glow of the price display casting warm highlights on the metal nozzle, "
                     "condensation visible on the pump housing, concrete forecourt wet from early dew")
    elif any(kw in action.lower() for kw in ['trading', 'screen', 'floor', 'monitor', 'ticker']):
        parts.append("Cool fluorescent overhead light mixed with the blue-white glow of multiple monitors, "
                     "green and red numbers reflecting on polished desk surfaces, "
                     "the ambient hum of cooling fans and distant keyboard clicks")
    elif any(kw in action.lower() for kw in ['map', 'globe', 'world', 'country', 'territory']):
        parts.append("Warm directional light from above illuminates the surface with gentle shadows, "
                     "the rich detail of geographic contours and boundaries visible, "
                     "muted earth tones with selective warm highlights on key regions")
    elif any(kw in action.lower() for kw in ['street', 'city', 'urban', 'building', 'sidewalk']):
        parts.append("Overcast urban daylight with soft diffused shadows, "
                     "concrete and glass surfaces reflecting the gray sky, "
                     "the texture of weathered brick and damp asphalt visible in sharp detail")
    elif any(kw in action.lower() for kw in ['paper', 'document', 'chart', 'graph', 'book']):
        parts.append("Warm desk lamp light creating a pool of illumination on paper surfaces, "
                     "the texture of high-quality bond paper visible, "
                     "shadows from the lamp edge creating depth across the desk surface")
    elif any(kw in action.lower() for kw in ['gold', 'metal', 'coin', 'bar', 'precious']):
        parts.append("Rich warm side-lighting catches metallic surfaces creating brilliant specular highlights, "
                     "the weight and density of precious metal visible in how light wraps around its contours, "
                     "deep shadows accentuating the three-dimensional form")
    elif any(kw in action.lower() for kw in ['hand', 'finger', 'grip', 'touch', 'hold']):
        parts.append("Close-up detail showing skin texture, subtle vein patterns, "
                     "the micro-movements of tendons and muscles beneath the skin, "
                     "shallow depth of field isolating the hand against a softly blurred background")
    else:
        parts.append("Cinematic documentary lighting with natural color temperature, "
                     "rich material textures and surface details visible in sharp focus, "
                     "environmental ambient sound of the space filling the scene")
    
    # Add camera movement
    if clip_idx == 1:
        parts.append("The camera holds in a medium establishing shot with gentle documentary handheld drift, "
                     "slowly revealing the scene's key elements through subtle compositional shifts")
    elif clip_idx == total_clips:
        parts.append("The camera slowly pulls back, widening the frame to encompass the full scene, "
                     "a sense of contemplative distance settling over the image")
    elif clip_idx % 3 == 0:
        parts.append("The camera pushes in slowly, drawing the viewer deeper into the detail, "
                     "the depth of field narrowing as focus tightens on the subject")
    elif clip_idx % 3 == 1:
        parts.append("The camera drifts gently to the right in a slow tracking movement, "
                     "parallax revealing depth between foreground and background elements")
    else:
        parts.append("The camera eases forward with a gentle dolly movement, "
                     "the frame composition shifting subtly to emphasize the central subject")
    
    # Add style
    parts.append(STYLE_STRING)
    
    # Join into single paragraph
    prompt = ". ".join(p.rstrip('.') for p in parts if p) + "."
    
    # Ensure we hit the word target (150-250)
    words = prompt.split()
    if len(words) > 260:
        prompt = " ".join(words[:250]) + "."
    
    return prompt

def scene_to_prompts(scene):
    """Convert one scene into clip prompts."""
    scene_num = scene["scene_num"]
    title = scene.get("title", "")
    visual = scene.get("visual_description", "")
    duration = scene.get("duration_sec", 60)
    
    narrative = get_narrative_context(visual)
    beats = parse_beats(visual)
    
    # Calculate clip count
    n_clips = max(1, (duration + 4) // 5)
    
    clips = []
    
    if beats:
        # Group beats into clips (aim for 1-2 beats per clip)
        beats_per_clip = max(1, len(beats) // min(n_clips, len(beats)))
        
        for i in range(0, len(beats), beats_per_clip):
            group = beats[i:i + beats_per_clip]
            combined_action = ". ".join(b["visual_action"] for b in group)
            
            prompt = enrich_prompt(
                combined_action, narrative, title,
                clip_idx=len(clips) + 1,
                total_clips=min(n_clips, (len(beats) + beats_per_clip - 1) // beats_per_clip)
            )
            
            clips.append({
                "clip_idx": len(clips) + 1,
                "prompt": prompt,
            })
            
            if len(clips) >= n_clips:
                break
    
    if not clips:
        # No beats - create from narrative
        sentences = [s.strip() for s in narrative.split('. ') if len(s.strip()) > 30]
        sents_per_clip = max(1, len(sentences) // min(n_clips, 6))
        
        for i in range(0, len(sentences), sents_per_clip):
            group = sentences[i:i + sents_per_clip]
            combined = ". ".join(group)
            prompt = enrich_prompt(
                combined, narrative, title,
                clip_idx=len(clips) + 1,
                total_clips=min(n_clips, 6)
            )
            clips.append({
                "clip_idx": len(clips) + 1,
                "prompt": prompt,
            })
            if len(clips) >= 6:
                break
    
    return {
        "scene_num": scene_num,
        "title": title,
        "duration_sec": duration,
        "clips": clips,
        "prompt": clips[0]["prompt"] if clips else "",
    }

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenes", default="/home/user/workspace/scenes_parsed.json")
    parser.add_argument("--output", default="/home/user/workspace/scene_prompts.json")
    args = parser.parse_args()
    
    with open(args.scenes) as f:
        scenes = json.load(f)
    
    print(f"Converting {len(scenes)} scenes to LTX-2.3 prompts (v2 - enriched)...")
    
    all_prompts = []
    total_clips = 0
    word_counts = []
    
    for scene in scenes:
        result = scene_to_prompts(scene)
        all_prompts.append(result)
        n = len(result["clips"])
        total_clips += n
        
        wcs = [len(c["prompt"].split()) for c in result["clips"]]
        word_counts.extend(wcs)
        avg_wc = sum(wcs) / len(wcs) if wcs else 0
        
        print(f"  Scene {result['scene_num']:2d} ({result['title'][:30]:30s}): {n} clips, avg {avg_wc:.0f} words/prompt")
    
    with open(args.output, "w") as f:
        json.dump(all_prompts, f, indent=2)
    
    avg_total = sum(word_counts) / len(word_counts) if word_counts else 0
    print(f"\nTotal: {total_clips} clips across {len(all_prompts)} scenes")
    print(f"Average prompt length: {avg_total:.0f} words (target: 150-250)")
    print(f"Min: {min(word_counts)} words, Max: {max(word_counts)} words")
    print(f"Saved to {args.output}")

if __name__ == "__main__":
    main()
