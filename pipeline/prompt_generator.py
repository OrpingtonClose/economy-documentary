#!/usr/bin/env python3
"""
LTX-2.3 Prompt Generator — Six-Layer Cinematic Prompts
========================================================
Generates cinema-quality prompts for LTX-2.3 following the official guide.

Reads audio timing from the OTIO timeline, then generates a video prompt
for each narration segment (or group of segments forming a visual beat).

Six-Layer Prompt Structure:
  1. Shot establishment — cinematic framing (wide, close-up, tracking, etc.)
  2. Scene/environment — lighting, color, textures, atmosphere
  3. Action — what happens from start to end of the clip
  4. Character(s) — physical description, clothing, physical emotion cues
  5. Camera movement — how camera moves, what it reveals
  6. Audio description — ambient sound, voice quality, environmental acoustics

Key LTX-2.3 rules:
  - Single flowing paragraph, present tense
  - 4-8 sentences for 5-8s clips, more for longer clips
  - Prompt length MUST match video duration (short prompt + long video = rushed)
  - NO text/logos on screen
  - NO emotional labels — use PHYSICAL CUES instead
  - NO numerical specifications — use natural language
  - For documentary: visual metaphors > literal depictions
"""

import json
import logging
import os
import random

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Shot type vocabulary — varied across clips for visual rhythm
# ------------------------------------------------------------------
SHOT_TYPES = [
    "A wide establishing shot",
    "A medium shot",
    "An intimate close-up",
    "An extreme close-up",
    "A slow tracking shot",
    "A sweeping aerial view",
    "A low-angle shot looking upward",
    "A high-angle shot looking down",
    "A Dutch angle shot",
    "A shallow depth-of-field close-up",
    "A rack-focus medium shot",
    "A static wide shot",
    "A handheld documentary-style shot",
    "A steady overhead shot looking directly down",
    "A silhouette shot against backlight",
]

# ------------------------------------------------------------------
# Camera movements — natural language, no numerical specs
# ------------------------------------------------------------------
CAMERA_MOVEMENTS = [
    "The camera slowly pushes forward, drawing closer to the subject as details sharpen into focus",
    "The camera glides gently to the right in a smooth tracking movement, parallax separating foreground from background",
    "The camera pulls back gradually, widening the frame to reveal the full scope of the scene",
    "The camera holds steady with only the faintest handheld micro-drift, letting the action unfold naturally within the frame",
    "The camera descends slowly from above, transitioning from an overhead perspective to eye level",
    "The camera drifts left in a gentle arc, circling slightly around the subject to reveal new angles",
    "The camera eases in with a slow dolly push, the depth of field narrowing as focus tightens on the central subject",
    "The camera tilts upward gradually, following a vertical line from the ground to the horizon",
    "The camera performs a subtle crane movement, rising gently while maintaining its forward gaze",
    "The camera remains locked in a composed static frame, the stillness emphasizing the gravity of the moment",
    "The camera racks focus from the foreground element to the background, shifting attention across the depth of the scene",
    "The camera follows the subject at a matched pace, maintaining consistent framing throughout the movement",
]

# ------------------------------------------------------------------
# Environment / atmosphere palettes for documentary subjects
# ------------------------------------------------------------------
ENVIRONMENTS = {
    "trading_floor": {
        "lighting": "Cool fluorescent overhead light mixed with the blue-white glow of multiple monitors",
        "textures": "polished desk surfaces reflect green and red numbers, scattered papers, coffee-stained reports",
        "atmosphere": "the tense hum of urgent voices and electronic beeps fills the space",
        "palette": "deep navy, electric blue, amber warning indicators, clinical white",
    },
    "war_zone": {
        "lighting": "Harsh, directional sunlight cutting through dust and smoke",
        "textures": "pitted concrete, twisted rebar, the chalky surface of rubble",
        "atmosphere": "a heavy stillness broken only by distant rumbles and the crackle of settling debris",
        "palette": "desaturated earth tones, khaki, smoke gray, occasional flash of orange flame",
    },
    "government_office": {
        "lighting": "Warm tungsten desk lamps creating pools of light against wood-paneled darkness",
        "textures": "leather-bound folders, polished mahogany, brass fixtures catching the light",
        "atmosphere": "the quiet of thick carpeting and heavy doors, the scratch of a pen on paper",
        "palette": "rich burgundy, dark walnut, gold leaf accents, cream paper",
    },
    "street_market": {
        "lighting": "Dappled morning light filtering through canvas awnings and rising steam",
        "textures": "rough burlap sacks, stacked crates of produce, hand-painted price signs",
        "atmosphere": "a layered soundscape of bargaining voices, clinking coins, and distant traffic",
        "palette": "warm earth tones, vibrant produce colors muted by morning haze",
    },
    "oil_refinery": {
        "lighting": "Industrial sodium-vapor lighting casting everything in amber-orange against a darkening sky",
        "textures": "massive steel pipes, riveted tanks, the oily sheen on metal walkways",
        "atmosphere": "the deep industrial drone of machinery, the hiss of steam from pressure valves",
        "palette": "industrial gray, rust brown, sodium amber, petroleum black",
    },
    "kitchen_domestic": {
        "lighting": "Warm morning light slanting through a window casting long shadows across worn surfaces",
        "textures": "scratched countertops, ceramic mugs, the grain of oak visible under soft directional light",
        "atmosphere": "the quiet intimacy of a family space, steam rising from coffee, a clock ticking",
        "palette": "warm honey tones, cream, soft olive, weathered wood brown",
    },
    "data_center": {
        "lighting": "Rows of blinking LEDs creating rhythmic patterns of green and blue light in a dark space",
        "textures": "brushed aluminum server racks, fiber optic cables catching light, ventilation grilles",
        "atmosphere": "the constant white noise of cooling fans, a clinical sterile stillness",
        "palette": "midnight blue, LED green, cool silver, cable-management black",
    },
    "shipping_port": {
        "lighting": "Overcast gray daylight reflecting off water and metal container surfaces",
        "textures": "weathered steel containers stacked high, heavy-gauge chain, rust streaks on painted metal",
        "atmosphere": "the deep horn of a cargo vessel, the clatter of crane mechanisms, seagull calls",
        "palette": "container blue, safety orange, maritime gray, oxidized rust",
    },
    "financial_district": {
        "lighting": "Cold morning light reflecting off glass towers, sharp geometric shadows on concrete",
        "textures": "polished granite lobbies, steel and glass curtain walls, digital tickers",
        "atmosphere": "the murmur of suited pedestrians, the whoosh of revolving doors, distant sirens",
        "palette": "steel blue, concrete gray, glass reflection white, power-suit black",
    },
    "abstract_metaphor": {
        "lighting": "Dramatic chiaroscuro lighting with deep blacks and sharp highlights",
        "textures": "abstract surfaces, flowing liquids, crystalline structures, particles suspended in air",
        "atmosphere": "a resonant low-frequency tone underscoring the visual weight of the image",
        "palette": "high contrast black and white with a single accent color bleeding through",
    },
    "military_hardware": {
        "lighting": "Flat overcast light with harsh clarity, every detail visible without shadow comfort",
        "textures": "matte olive drab paint, riveted armor plate, rubber treads, stenciled markings",
        "atmosphere": "the diesel rumble of engines, the metallic clank of machinery, radio static",
        "palette": "olive green, desert tan, matte black, warning red markings",
    },
    "newsroom": {
        "lighting": "Bright broadcast lighting from above, sharp and shadowless on the anchor desk",
        "textures": "glossy desk surfaces, teleprompter glass, studio monitors showing live feeds",
        "atmosphere": "the hum of studio equipment, producer chatter in earpieces, the urgency of breaking news",
        "palette": "broadcast blue, studio white, accent red, ticker yellow",
    },
}

# ------------------------------------------------------------------
# Visual metaphor mappings for documentary narration concepts
# ------------------------------------------------------------------
CONCEPT_VISUALS = {
    "money": ["stacks of currency being counted by weathered hands", "a scale tipping under the weight of gold coins",
              "numbers cascading across a trading display", "a vault door slowly swinging open"],
    "war": ["a chess board with pieces cast in the shape of military equipment",
            "a map with lines being drawn and redrawn by unseen hands",
            "smoke rising from a distant horizon viewed through binoculars"],
    "oil": ["crude oil pouring in slow motion, its viscous surface catching the light",
            "a gas pump nozzle dripping the last drops", "an oil derrick silhouetted against an amber sunset"],
    "power": ["a gavel striking wood in extreme close-up", "a hand signing a document with a heavy fountain pen",
              "a spotlight illuminating an empty podium in a vast dark room"],
    "collapse": ["dominoes falling in a long chain viewed from a low angle",
                 "a skyscraper reflected in a puddle, the image fragmenting as a foot steps through",
                 "a stock ticker freezing mid-scroll, the numbers glitching"],
    "surveillance": ["a bank of security monitors showing empty corridors",
                     "a satellite dish slowly rotating against a star-filled sky",
                     "a cursor blinking on a dark terminal screen"],
    "profit": ["a champagne glass being filled in slow motion at a rooftop party",
               "a luxury watch mechanism ticking in extreme macro close-up",
               "a fountain pen signing a figure on a contract"],
    "suffering": ["an empty playground swing moving gently in the wind",
                  "a family photograph lying face-down in dust",
                  "a pair of worn shoes left at a doorstep"],
    "trade": ["cargo containers stacked like building blocks on a massive vessel",
              "hands exchanging a briefcase in a dimly lit corridor",
              "a scale balancing two different commodities"],
    "corruption": ["a shadow falling across an official document",
                   "coins slowly sinking into dark water",
                   "a crack spreading through a marble facade"],
    "technology": ["fiber optic cables pulsing with light in a dark server room",
                   "a drone's eye view descending over a sprawling tech campus",
                   "lines of code reflecting in the lens of a pair of glasses"],
    "diplomacy": ["two flags hanging limply in still air inside a grand hall",
                  "a long polished table with empty chairs and name placards",
                  "a document being slid across a table between two pairs of hands"],
}


def _pick_environment(narration_text, scene_title):
    """Select the most fitting environment based on narration content."""
    text = (narration_text + " " + scene_title).lower()

    scoring = {
        "trading_floor": ["trade", "stock", "market", "dow", "nasdaq", "exchange", "ticker", "portfolio", "index"],
        "war_zone": ["bomb", "missile", "attack", "destruction", "rubble", "casualt", "soldier", "combat", "strike"],
        "government_office": ["policy", "regulation", "senator", "congress", "legislation", "bureaucr", "official"],
        "street_market": ["price", "grocery", "consumer", "inflation", "cost of living", "food", "bread"],
        "oil_refinery": ["oil", "petroleum", "barrel", "crude", "refinery", "pipeline", "opec", "gas", "fuel"],
        "kitchen_domestic": ["family", "kitchen", "home", "breakfast", "morning", "coffee", "everyday"],
        "data_center": ["data", "algorithm", "crypto", "bitcoin", "blockchain", "digital", "server", "cyber"],
        "shipping_port": ["shipping", "container", "cargo", "supply chain", "port", "export", "import", "vessel"],
        "financial_district": ["bank", "wall street", "finance", "investment", "billion", "hedge fund", "capital"],
        "abstract_metaphor": ["concept", "abstract", "metaphor", "idea", "fundamental", "system"],
        "military_hardware": ["weapon", "defense", "military", "arms", "tank", "aircraft", "ammunition", "contract"],
        "newsroom": ["report", "breaking", "headline", "anchor", "broadcast", "coverage", "media"],
    }

    best_env = "abstract_metaphor"
    best_score = 0

    for env_key, keywords in scoring.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > best_score:
            best_score = score
            best_env = env_key

    return best_env


def _pick_visual_metaphor(narration_text):
    """Select a visual metaphor based on narration content."""
    text = narration_text.lower()

    for concept, visuals in CONCEPT_VISUALS.items():
        if concept in text:
            return random.choice(visuals)

    return None


def _scale_prompt_length(duration_sec):
    """
    Determine target sentence count based on clip duration.
    LTX-2.3 rule: prompt length must match video duration.
    Short prompt + long video = model rushes through content.
    """
    if duration_sec <= 3:
        return 3, 4   # min, max sentences
    elif duration_sec <= 5:
        return 4, 6
    elif duration_sec <= 8:
        return 5, 8
    elif duration_sec <= 11:
        return 7, 10
    else:
        return 8, 12


def generate_prompt(narration_text, scene_title, scene_context,
                    target_duration_sec, clip_index, total_clips_in_scene,
                    previous_shot_type=None):
    """
    Generate a single LTX-2.3 video prompt following the six-layer structure.

    Args:
        narration_text: the narration being spoken during this clip
        scene_title: the scene title for context
        scene_context: broader scene narrative context
        target_duration_sec: how long the video clip should be
        clip_index: position of this clip within the scene (0-based)
        total_clips_in_scene: total clips in this scene
        previous_shot_type: shot type used in previous clip (to vary)

    Returns:
        dict with 'prompt', 'shot_type', 'environment', 'duration_sec'
    """
    min_sentences, max_sentences = _scale_prompt_length(target_duration_sec)
    env_key = _pick_environment(narration_text, scene_title)
    env = ENVIRONMENTS[env_key]
    metaphor = _pick_visual_metaphor(narration_text)

    # Layer 1: Shot establishment — vary across clips for visual rhythm
    available_shots = [s for s in SHOT_TYPES if s != previous_shot_type]
    # First clip: establishing; last: pull-back; middle: variety
    if clip_index == 0:
        shot = random.choice([s for s in available_shots if "wide" in s.lower() or "establishing" in s.lower()] or available_shots)
    elif clip_index == total_clips_in_scene - 1:
        shot = random.choice([s for s in available_shots if "wide" in s.lower() or "pull" in s.lower() or "static" in s.lower()] or available_shots)
    else:
        shot = random.choice(available_shots)

    # Layer 2: Scene/environment
    env_sentence = f"{env['lighting']}, {env['textures']}."

    # Layer 3: Action — derive from narration, use visual metaphor if available
    if metaphor:
        action_sentence = (
            f"{metaphor.rstrip('.')}. "
            f"The scene conveys the weight of {_extract_key_concept(narration_text)}"
        )
    else:
        action_sentence = _narration_to_visual_action(narration_text, scene_context)

    # Layer 4: Character/subject — physical description with physical cues (no emotions)
    character_sentence = _generate_character_layer(narration_text, env_key)

    # Layer 5: Camera movement
    cam_move = random.choice(CAMERA_MOVEMENTS)

    # Layer 6: Audio description
    audio_sentence = f"The ambient sound is {env['atmosphere']}."

    # Assemble into single flowing paragraph
    parts = [
        f"{shot} of {_scene_opener(narration_text, env_key)}.",
        env_sentence,
        action_sentence + ".",
        character_sentence,
        cam_move + ".",
        audio_sentence,
    ]

    # Style signature
    style = (
        "Photorealistic cinematic documentary footage, shot on Arri Alexa with Cooke anamorphic lenses, "
        f"natural film grain, shallow depth of field, {env['palette']} color palette, "
        "documentary-style composition with deliberate negative space."
    )
    parts.append(style)

    # Build prompt — ensure it fits duration requirements
    prompt = " ".join(p.strip() for p in parts if p.strip())

    # Clean up double periods, extra spaces
    prompt = prompt.replace("..", ".").replace(". .", ".").replace("  ", " ")

    # Verify word count matches duration
    word_count = len(prompt.split())
    target_min = min_sentences * 15  # rough: 15 words/sentence
    target_max = max_sentences * 25

    if word_count < target_min and target_duration_sec > 5:
        # Add more detail for longer clips
        extra = _add_detail_for_duration(narration_text, env_key, target_duration_sec)
        prompt = prompt.rstrip(".") + ". " + extra

    return {
        "prompt": prompt,
        "shot_type": shot,
        "environment": env_key,
        "duration_sec": target_duration_sec,
        "word_count": len(prompt.split()),
    }


def _extract_key_concept(text):
    """Extract the core concept from narration text."""
    text_lower = text.lower()
    for concept in CONCEPT_VISUALS:
        if concept in text_lower:
            return concept
    # Fallback: use first few meaningful words
    words = [w for w in text.split()[:8] if len(w) > 3]
    return " ".join(words[:4]) if words else "the unfolding situation"


def _scene_opener(narration_text, env_key):
    """Generate a scene-setting opener based on environment."""
    openers = {
        "trading_floor": "a dimly lit trading floor where rows of monitors cast flickering light across tense faces",
        "war_zone": "a devastated urban landscape where dust and smoke drift through shattered window frames",
        "government_office": "a wood-paneled office where heavy curtains filter pale light onto stacked dossiers",
        "street_market": "a bustling morning market where vendors arrange goods under weathered canvas awnings",
        "oil_refinery": "a sprawling industrial complex where steel towers rise against a chemical-hued sky",
        "kitchen_domestic": "a quiet kitchen where morning light falls across a scratched wooden table",
        "data_center": "a vast server hall where endless racks pulse with blinking indicator lights in the darkness",
        "shipping_port": "a massive container port where cranes swing their loads against an overcast horizon",
        "financial_district": "a glass-and-steel canyon of towers where reflections distort the morning sky",
        "abstract_metaphor": "a stark abstract space where light and shadow create dramatic geometric patterns",
        "military_hardware": "a military staging area where rows of equipment stand under flat gray daylight",
        "newsroom": "a bright broadcast studio where multiple screens display competing urgent feeds",
    }
    return openers.get(env_key, "a carefully composed documentary scene")


def _narration_to_visual_action(narration_text, scene_context):
    """Convert narration text into a visual action description."""
    # Extract key nouns and verbs for visual representation
    text = narration_text.strip()
    if len(text) > 200:
        text = text[:200]

    # Convert abstract concepts to concrete visuals
    replacements = [
        ("billion dollars", "a figure with many zeroes appears on a screen"),
        ("percent", "a graph line shifts direction"),
        ("inflation", "price tags flip to higher numbers"),
        ("interest rate", "a dial turns clockwise"),
        ("sanctions", "a red stamp presses down on a document"),
        ("supply chain", "containers move along a conveyor system"),
        ("deficit", "a balance scale tips sharply to one side"),
        ("GDP", "a bar chart fills the frame"),
    ]

    visual = text
    for phrase, replacement in replacements:
        if phrase.lower() in visual.lower():
            return (
                f"In the foreground, {replacement}. "
                f"The broader scene reflects the weight of what is being described"
            )

    return (
        f"The scene unfolds with deliberate pacing, each element in the frame "
        f"reinforcing the gravity of the narration. "
        f"Visual details accumulate gradually, building a layered documentary composition"
    )


def _generate_character_layer(narration_text, env_key):
    """Generate character/subject descriptions using physical cues only."""
    char_templates = {
        "trading_floor": (
            "A trader in a rumpled white dress shirt, sleeves rolled to the elbows, "
            "presses his palm against his forehead and exhales slowly through pursed lips. "
            "His tie hangs loose, collar unbuttoned, dark circles visible under the monitor glow."
        ),
        "war_zone": (
            "A figure in a dust-covered jacket stands with shoulders hunched, "
            "one hand gripping a crumbling doorframe for balance. "
            "Grime lines the creases of their face, their gaze fixed on a point in the middle distance."
        ),
        "government_office": (
            "A silver-haired official in a dark suit adjusts a stack of folders with deliberate, "
            "measured movements. Reading glasses perched at the tip of their nose, "
            "a fountain pen held motionless between two fingers."
        ),
        "street_market": (
            "A vendor with weathered hands and a faded apron leans forward to rearrange a display, "
            "fingers moving with practiced efficiency. Deep lines frame their eyes, "
            "squinting against the morning sun."
        ),
        "oil_refinery": (
            "A worker in a hard hat and high-visibility vest walks along a catwalk, "
            "one gloved hand trailing along the railing. "
            "Their boots leave prints on the metal grating."
        ),
        "kitchen_domestic": (
            "A middle-aged person in a worn cardigan pauses mid-motion, coffee mug suspended halfway "
            "to their lips, eyes fixed on a newspaper headline. Their other hand rests flat on the table, "
            "fingers spread wide as if bracing against the surface."
        ),
        "data_center": (
            "A technician in a dark polo shirt peers at a rack display, "
            "the screen's light casting sharp shadows across their focused expression. "
            "Their fingers hover over a keyboard without pressing a key."
        ),
        "shipping_port": (
            "A dockworker in a heavy coat and safety vest signals with broad arm gestures, "
            "the wind catching the loose fabric of their clothing. "
            "Salt-weathered skin and squinting eyes against the harbor glare."
        ),
        "financial_district": (
            "Suited pedestrians move through the frame, their pace quickening, "
            "briefcases gripped tighter, phone screens illuminating downcast faces. "
            "One figure stands still against the flow, staring upward at a ticker display."
        ),
        "military_hardware": (
            "A uniformed figure stands at attention beside a vehicle, "
            "arms clasped behind their back, chin slightly raised. "
            "The fabric of their uniform is pressed and rigid, catching hard shadows."
        ),
        "newsroom": (
            "An anchor sits straight-backed behind the desk, papers squared precisely, "
            "maintaining composed stillness as studio lights cast a bright wash over the set. "
            "Their eyes track a teleprompter with controlled precision."
        ),
    }
    return char_templates.get(env_key, (
        "A solitary figure occupies the frame, their posture conveying the weight "
        "of the moment through subtle physical tension visible in their hands and shoulders."
    ))


def _add_detail_for_duration(narration_text, env_key, duration_sec):
    """Add extra environmental detail for longer clips so prompt matches duration."""
    extras = {
        "trading_floor": (
            "Papers shift in the air-conditioning draft. A phone rings unanswered on a far desk. "
            "The timestamp on a corner monitor ticks forward. Reflections of chart patterns "
            "ripple across a glass partition."
        ),
        "war_zone": (
            "A curtain flutters through a broken window. Dust motes drift slowly through a shaft of light. "
            "A cracked mirror on a wall reflects fragmented sky. Water drips from exposed pipes."
        ),
        "kitchen_domestic": (
            "Steam curls upward from the cup, catching golden light. A clock on the wall ticks audibly. "
            "Crumbs scatter across a breadboard. The refrigerator hum provides a bass note to the silence."
        ),
    }
    extra = extras.get(env_key, (
        "Small details emerge in the periphery — textures reveal themselves under scrutiny, "
        "ambient elements shift subtly, and the frame breathes with quiet, documentary patience."
    ))
    return extra


def generate_prompts_for_scene(scene_num, scene_title, audio_segments,
                               narration_texts, scene_context=""):
    """
    Generate video prompts for all clips in a scene based on audio timing.

    Args:
        scene_num: int
        scene_title: str
        audio_segments: list of dicts from otio_timeline.get_scene_audio_segments()
        narration_texts: list of str, the full narration text for each segment
        scene_context: broader context about the scene

    Returns:
        list of prompt dicts ready for video generation
    """
    prompts = []
    previous_shot = None

    # Calculate clip boundaries from audio segments
    # Each audio segment becomes one video clip
    total_clips = len(audio_segments)

    for i, seg in enumerate(audio_segments):
        dur = seg["duration_sec"]
        narr_text = narration_texts[i] if i < len(narration_texts) else seg.get("text_preview", "")

        # Generate slightly longer than audio (will be trimmed)
        # LTX-2.3 generates ~5.04s clips; for longer, we need frame-chaining
        generation_duration = dur + 0.5  # 0.5s buffer for trimming
        ltx_clips_needed = max(1, int(generation_duration / 5.04) + (1 if generation_duration % 5.04 > 0.5 else 0))

        result = generate_prompt(
            narration_text=narr_text,
            scene_title=scene_title,
            scene_context=scene_context,
            target_duration_sec=dur,
            clip_index=i,
            total_clips_in_scene=total_clips,
            previous_shot_type=previous_shot,
        )

        prompts.append({
            "clip_id": f"scene_{scene_num:02d}_clip{i:02d}",
            "scene_number": scene_num,
            "scene_title": scene_title,
            "clip_index": i,
            "target_duration_sec": round(dur, 3),
            "generation_duration_sec": round(generation_duration, 3),
            "ltx_clips_needed": ltx_clips_needed,
            "prompt": result["prompt"],
            "shot_type": result["shot_type"],
            "environment": result["environment"],
            "word_count": result["word_count"],
            "narration_text": narr_text[:200],
            "audio_start_sec": seg.get("start_sec", 0),
            "audio_end_sec": seg.get("end_sec", 0),
        })

        previous_shot = result["shot_type"]

    return prompts


def generate_all_prompts(otio_timeline, narration_data):
    """
    Generate video prompts for all scenes using OTIO audio timing.

    Args:
        otio_timeline: OTIOTimeline instance (loaded, with audio track populated)
        narration_data: list of scene dicts with narration text

    Returns:
        list of all prompt dicts across all scenes
    """
    all_prompts = []
    scenes_audio = otio_timeline.get_all_scenes_audio_timing()

    # Build narration text lookup: scene_num -> list of segment texts
    narr_lookup = {}
    for scene in narration_data:
        sn = scene["scene_number"]
        segments = parse_narration_segments_text(scene.get("narration_text", ""))
        narr_lookup[sn] = segments

    for scene in narration_data:
        scene_num = scene["scene_number"]
        scene_title = scene.get("scene_title", "")

        audio_segs = scenes_audio.get(scene_num, [])
        if not audio_segs:
            log.warning(f"Scene {scene_num}: no audio segments in OTIO timeline")
            continue

        narr_texts = [seg_text for _, seg_text in narr_lookup.get(scene_num, [])]

        prompts = generate_prompts_for_scene(
            scene_num=scene_num,
            scene_title=scene_title,
            audio_segments=audio_segs,
            narration_texts=narr_texts,
            scene_context=scene.get("context", ""),
        )

        all_prompts.extend(prompts)
        log.info(f"Scene {scene_num:2d} ({scene_title[:30]:30s}): {len(prompts)} prompts, "
                 f"avg {sum(p['word_count'] for p in prompts)/max(1,len(prompts)):.0f} words")

    log.info(f"\nTotal: {len(all_prompts)} prompts across {len(scenes_audio)} scenes")
    return all_prompts


def parse_narration_segments_text(narration_text):
    """Parse narration text into (voice, text) tuples."""
    import re
    segments = []
    pattern = r'(V[123])\s*(?:\([^)]*\))?\s*:\s*"?(.*?)(?:"|(?=\nV[123]\s*(?:\([^)]*\))?\s*:)|\Z)'
    matches = list(re.finditer(pattern, narration_text, re.DOTALL))

    if not matches:
        clean = narration_text.strip().strip('"')
        if clean:
            segments.append(("V1", clean))
        return segments

    for m in matches:
        voice = m.group(1)
        text = m.group(2).strip().strip('"').strip()
        text = re.sub(r'\s+', ' ', text)
        if text:
            segments.append((voice, text))

    return segments


def main():
    """CLI entry point for prompt generation."""
    import argparse
    from pipeline.otio_timeline import OTIOTimeline

    parser = argparse.ArgumentParser(description="LTX-2.3 prompt generator")
    parser.add_argument("--otio", required=True, help="Path to .otio timeline")
    parser.add_argument("--narration-script", required=True, help="Path to narration_script.json")
    parser.add_argument("--output", required=True, help="Output prompts JSON path")
    args = parser.parse_args()

    # Load OTIO timeline
    otio_tl = OTIOTimeline(args.otio)
    otio_tl.load()

    # Load narration script
    with open(args.narration_script) as f:
        narration_data = json.load(f)

    # Generate prompts
    all_prompts = generate_all_prompts(otio_tl, narration_data)

    # Save
    with open(args.output, "w") as f:
        json.dump(all_prompts, f, indent=2)

    log.info(f"Saved {len(all_prompts)} prompts to {args.output}")


if __name__ == "__main__":
    main()
