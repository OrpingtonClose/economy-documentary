#!/usr/bin/env python3
"""
LTX-2.3 Prompt Generator — Project-Agnostic Cinematic Composition Engine
==========================================================================
Generates cinema-quality prompts for LTX-2.3 using NLP-based narration analysis
that derives ALL visual vocabulary from the narration content at runtime.

NO hardcoded topic-specific dictionaries. Works for any documentary subject
(medicine, space, cooking, history, war, economy — anything).

NO external LLM API required — runs entirely on VMs without internet access.
Quality comes from NLP-based narration analysis, cinematic building blocks,
scene continuity tracking, and duration-calibrated complexity.

Composition Layers:
  1. Shot establishment — cinematic framing varied across clips for visual rhythm
  2. Scene/environment — lighting, color, textures, atmosphere (derived from narration)
  3. Action — visual metaphors and narration-derived B-roll descriptions
  4. Character(s) — physical description, clothing, physical emotion cues
  5. Camera movement — explicit, natural-language camera directions
  6. Audio description — ambient sound, environmental acoustics

Key LTX-2.3 rules:
  - Single flowing paragraph, present tense
  - 150-250 words per prompt (standard clips)
  - Prompt length MUST match video duration (short prompt + long video = rushed)
  - NO text/logos on screen ever
  - NO emotional labels — use PHYSICAL CUES only
  - NO numerical specifications — use natural language
  - For documentary: visual metaphors > literal depictions
  - Explicit camera movement in every prompt
  - Physical/material textures in every prompt
  - Ambient audio description in every prompt
  - Shot type varies across sequential clips

OTIO Integration:
  - Reads audio segments from OTIO timeline as primary input
  - Stores generated prompts directly in OTIO metadata on video track gaps
  - OTIO is the single source of truth — JSON export is derivative only
"""

import json
import logging
import os
import random
import re

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

# Dramatic arc shot progression — used to select shot types based on position
SHOT_PROGRESSION = {
    "opening": ["A wide establishing shot", "A sweeping aerial view",
                 "A static wide shot", "A slow tracking shot"],
    "rising": ["A medium shot", "A rack-focus medium shot",
               "A handheld documentary-style shot", "A low-angle shot looking upward"],
    "climax": ["An intimate close-up", "An extreme close-up",
               "A shallow depth-of-field close-up", "A Dutch angle shot"],
    "falling": ["A high-angle shot looking down", "A steady overhead shot looking directly down",
                "A medium shot", "A slow tracking shot"],
    "closing": ["A wide establishing shot", "A silhouette shot against backlight",
                "A static wide shot", "A sweeping aerial view"],
}

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
# Cinematic building blocks — filmmaking vocabulary, NOT topic-specific
# ------------------------------------------------------------------

LIGHTING_MOODS = {
    "tense": [
        "harsh directional sidelight casting deep shadows",
        "cold fluorescent overhead light creating stark contrast",
    ],
    "calm": [
        "soft diffused natural light filtering through the space",
        "warm golden hour light raking across surfaces at a low angle",
    ],
    "urgent": [
        "rapidly shifting light sources creating unstable illumination",
        "bright overhead light leaving no shadows to hide in",
    ],
    "somber": [
        "muted overcast light draining color from every surface",
        "a single dim light source leaving the edges in darkness",
    ],
    "neutral": [
        "even natural daylight filling the space without dramatic shadows",
        "balanced practical lighting from visible sources in the scene",
    ],
}

TEXTURE_FAMILIES = {
    "organic": "weathered wood grain, rough natural fiber, worn leather, aged paper",
    "industrial": "brushed metal, riveted steel, poured concrete, rubber gaskets",
    "digital": "glass screens, fiber optic cables, LED indicators, polished surfaces",
    "architectural": "polished stone, carved wood, painted plaster, window glass",
    "natural": "soil, water, growing plants, raw stone, unprocessed materials",
}

ATMOSPHERE_TYPES = {
    "tense": "a weighted silence punctuated by small sharp sounds",
    "busy": "overlapping voices and activity creating a layered soundscape",
    "empty": "the hollow resonance of an unoccupied space",
    "intimate": "close quiet sounds — breathing, fabric movement, the smallest gestures amplified",
    "vast": "sound swallowed by distance, echoes fading into open space",
}

PALETTE_MODES = {
    "warm": "warm amber, honey gold, burnt sienna, soft cream",
    "cool": "steel blue, slate gray, pale silver, muted teal",
    "desaturated": "washed-out earth tones, faded gray-green, muted ochre, dusty beige",
    "high_contrast": "deep blacks against bright highlights, sharp tonal separation, minimal mid-tones",
    "monochrome": "near-monochromatic tones with a single subtle accent color bleeding through",
}

# ------------------------------------------------------------------
# NLP cue word sets — used to classify narration content at runtime
# ------------------------------------------------------------------

_INDOOR_CUES = {
    "office", "room", "lab", "laboratory", "hospital", "factory", "studio",
    "kitchen", "house", "home", "school", "church", "library", "museum",
    "warehouse", "hall", "building", "station", "theater", "court", "prison",
    "clinic", "workshop", "store", "shop", "restaurant", "bar", "cafe",
    "bunker", "shelter", "chamber", "cockpit", "cabin", "cellar",
}
_OUTDOOR_CUES = {
    "field", "mountain", "ocean", "sea", "river", "forest", "desert", "sky",
    "road", "street", "city", "village", "farm", "garden", "beach", "coast",
    "valley", "hill", "lake", "island", "jungle", "plain", "tundra", "port",
    "harbor", "bridge", "border", "camp", "ruins", "canyon", "crater",
    "orbit", "summit", "plateau", "marsh", "reef", "glacier",
}

_TENSE_CUES = {
    "crisis", "threat", "danger", "fear", "risk", "conflict", "struggle",
    "tension", "alarm", "emergency", "collapse", "destruction", "attack",
    "chaos", "panic", "disaster", "catastrophe", "confrontation", "battle",
    "critical", "desperate", "volatile", "unstable", "deadly", "violent",
}
_CALM_CUES = {
    "peace", "quiet", "gentle", "slow", "calm", "rest", "steady", "still",
    "harmony", "balance", "serene", "tranquil", "relaxed", "gradual",
    "patient", "soft", "ease", "stable", "settled", "comfort",
}
_URGENT_CUES = {
    "rapid", "sudden", "immediate", "race", "rush", "surge", "accelerat",
    "fast", "quick", "speed", "hurry", "sprint", "scramble", "overnight",
    "explode", "spike", "soar", "plummet", "crash", "erupt",
}
_SOMBER_CUES = {
    "loss", "grief", "death", "mourn", "sorrow", "tragic", "suffer",
    "decline", "fade", "wither", "abandon", "empty", "silent", "dark",
    "bleak", "grim", "desolate", "forgotten", "ruin", "end",
}

_TEXTURE_CUES = {
    "organic": {"wood", "leather", "cloth", "paper", "fiber", "fabric", "grain", "cotton", "wool", "bone", "shell"},
    "industrial": {"metal", "steel", "concrete", "machine", "factory", "pipe", "engine", "iron", "wire", "bolt", "gear"},
    "digital": {"screen", "computer", "data", "digital", "code", "monitor", "server", "network", "satellite", "radar"},
    "architectural": {"building", "wall", "column", "floor", "ceiling", "door", "window", "stone", "brick", "tile", "arch"},
    "natural": {"water", "earth", "soil", "plant", "tree", "river", "rock", "grass", "leaf", "sand", "ice", "snow"},
}

# Cinematic action verbs — generic framing language
_CINEMATIC_VERBS = [
    "unfolds", "emerges", "shifts", "accumulates", "transforms",
    "materializes", "dissolves", "intensifies", "recedes", "converges",
    "fragments", "coalesces", "ripples outward", "settles into place",
    "builds momentum", "comes into sharp focus",
]

# Physical detail fragments for character building
_POSTURE_DETAILS = [
    "shoulders drawn slightly forward under an invisible weight",
    "standing with feet planted wide, arms folded across the chest",
    "leaning forward with both hands flat on a surface",
    "sitting upright with a rigid spine, chin level",
    "turned half away, one hand resting on a nearby object",
    "paused mid-stride, weight shifted to one foot",
    "bent forward at the waist, peering closely at something just out of frame",
    "perched on the edge of a seat, body angled toward the center of the action",
]
_HAND_DETAILS = [
    "fingers interlaced tightly, knuckles whitening",
    "one hand gripping a worn object, the other hanging loose",
    "hands moving with practiced efficiency across a workspace",
    "fingers drumming silently on a surface",
    "palms open and upturned in a gesture of explanation",
    "hands clasped behind the back, fingers occasionally flexing",
    "one hand tracing a line across a document or surface",
    "both hands wrapped around a vessel, thumbs aligned along the rim",
]
_GAZE_DETAILS = [
    "eyes fixed on a point in the middle distance",
    "gaze tracking something just outside the frame",
    "eyes narrowed, scanning details closely",
    "looking downward at an object held in both hands",
    "staring directly into the lens with unblinking composure",
    "eyes moving rapidly, processing visible information",
    "gaze shifting between two focal points, weighing what each reveals",
    "eyes lifted toward the horizon, chin slightly raised",
]

# Generic action verbs extracted from narration
_ACTION_VERB_PATTERN = re.compile(
    r'\b(discover|transform|build|destroy|create|reveal|change|grow|'
    r'spread|collapse|rise|fall|move|shift|turn|break|open|close|'
    r'begin|end|start|stop|fight|defend|protect|save|lose|find|'
    r'search|explore|cross|reach|arrive|leave|return|carry|hold|'
    r'watch|observe|record|measure|test|launch|land|fly|sail|'
    r'march|gather|scatter|connect|divide|merge|split|produce|'
    r'harvest|cook|heal|teach|learn|write|read|sing|play|work|'
    r'climb|dig|pour|press|pull|push|lift|drop|cut|shape|'
    r'assemble|dismantle|navigate|orbit|descend|ascend|operate|'
    r'examine|prepare|collect|distribute|construct|demolish)\w*',
    re.IGNORECASE
)

# Generic person reference words
_PERSON_WORDS = {
    "person", "people", "man", "woman", "child", "children", "worker",
    "leader", "figure", "scientist", "doctor", "soldier", "farmer",
    "teacher", "student", "artist", "engineer", "pilot", "captain",
    "crew", "team", "group", "crowd", "citizen", "resident", "official",
    "expert", "researcher", "volunteer", "survivor", "witness", "patient",
    "nurse", "technician", "operator", "inspector", "guide", "traveler",
    "craftsman", "merchant", "apprentice", "elder", "youth", "infant",
    "villager", "settler", "explorer", "commander", "medic", "assistant",
}


# ------------------------------------------------------------------
# NLP-based narration analysis
# ------------------------------------------------------------------

def _analyze_narration(narration_text, scene_title=""):
    """
    Analyze narration text to extract visual composition cues.
    Returns a dict with: mood, indoor_outdoor, texture_family,
    time_of_day, palette_mode, content_words, persons, actions.
    """
    combined = (narration_text + " " + scene_title).lower()
    words = set(re.findall(r'[a-z]+', combined))

    # Detect mood from cue words
    mood = "neutral"
    mood_scores = {
        "tense": len(words & _TENSE_CUES),
        "calm": len(words & _CALM_CUES),
        "urgent": len(words & _URGENT_CUES),
        "somber": len(words & _SOMBER_CUES),
    }
    best_mood_score = max(mood_scores.values())
    if best_mood_score > 0:
        mood = max(mood_scores, key=mood_scores.get)

    # Detect indoor/outdoor
    indoor_score = len(words & _INDOOR_CUES)
    outdoor_score = len(words & _OUTDOOR_CUES)
    if indoor_score > outdoor_score:
        indoor_outdoor = "indoor"
    elif outdoor_score > indoor_score:
        indoor_outdoor = "outdoor"
    else:
        indoor_outdoor = "indoor"

    # Detect texture family
    texture_scores = {family: len(words & cues) for family, cues in _TEXTURE_CUES.items()}
    best_texture_score = max(texture_scores.values())
    if best_texture_score > 0:
        texture_family = max(texture_scores, key=texture_scores.get)
    else:
        texture_family = "architectural"

    # Time of day
    time_of_day = "day"
    if words & {"night", "midnight", "dark", "dusk", "evening", "moonlight", "starlight"}:
        time_of_day = "night"
    elif words & {"dawn", "sunrise", "morning", "early"}:
        time_of_day = "morning"
    elif words & {"sunset", "twilight", "golden"}:
        time_of_day = "evening"

    # Palette from mood
    palette_map = {
        "tense": "high_contrast",
        "calm": "warm",
        "urgent": "desaturated",
        "somber": "monochrome",
        "neutral": "cool",
    }
    palette_mode = palette_map.get(mood, "cool")

    # Extract action verbs first (needed to exclude from content words)
    action_matches = _ACTION_VERB_PATTERN.findall(combined)
    actions = list(dict.fromkeys(action_matches))[:6]
    action_stems = {a.lower() for a in actions}

    # Extract content words (nouns/descriptors) — excluding cue words and verbs
    all_cue_words = (_TENSE_CUES | _CALM_CUES | _URGENT_CUES | _SOMBER_CUES
                     | _INDOOR_CUES | _OUTDOOR_CUES | _PERSON_WORDS | action_stems)
    for cue_set in _TEXTURE_CUES.values():
        all_cue_words |= cue_set
    # Also exclude common function words
    _STOP_WORDS = {
        "this", "that", "with", "from", "have", "been", "were", "they",
        "their", "them", "than", "then", "into", "over", "also", "just",
        "more", "most", "such", "when", "what", "which", "where", "will",
        "would", "could", "should", "about", "after", "before", "between",
        "through", "during", "without", "another", "because", "every",
        "other", "some", "very", "only", "each", "much", "many", "well",
        "back", "even", "same", "made", "like", "slowly", "quickly",
        "carefully", "suddenly", "nearly", "almost", "already", "often",
        "never", "always", "still", "away", "across", "along", "around",
        "while", "until", "since", "being", "doing", "having", "going",
        "took", "went", "came", "gave", "told", "knew", "said", "become",
        "overnight", "aboard", "below", "above", "here", "there",
    }
    all_cue_words |= _STOP_WORDS
    content_words = [
        w for w in re.findall(r'[a-z]{4,}', combined)
        if w not in all_cue_words and not any(w.startswith(stem) for stem in action_stems)
    ]
    # Deduplicate while preserving order
    seen = set()
    unique_content = []
    for w in content_words:
        if w not in seen:
            seen.add(w)
            unique_content.append(w)
    content_words = unique_content[:12]

    # Extract person references
    found_persons = list(words & _PERSON_WORDS)

    return {
        "mood": mood,
        "indoor_outdoor": indoor_outdoor,
        "texture_family": texture_family,
        "time_of_day": time_of_day,
        "palette_mode": palette_mode,
        "content_words": content_words,
        "persons": found_persons,
        "actions": actions,
    }


# ------------------------------------------------------------------
# Environment builder — derives from narration, no fixed dict
# ------------------------------------------------------------------

def _build_environment(analysis):
    """
    Build environment description (lighting, textures, atmosphere, palette)
    from narration analysis — NOT from a fixed environment dictionary.
    """
    mood = analysis["mood"]
    texture_family = analysis["texture_family"]
    palette_mode = analysis["palette_mode"]
    time_of_day = analysis["time_of_day"]
    indoor_outdoor = analysis["indoor_outdoor"]

    # Lighting: mood-based, modified by time of day
    lighting_options = LIGHTING_MOODS.get(mood, LIGHTING_MOODS["neutral"])
    lighting = random.choice(lighting_options)
    if time_of_day == "morning":
        lighting = lighting + ", touched by early morning light"
    elif time_of_day == "evening":
        lighting = lighting + ", tinged with amber twilight"
    elif time_of_day == "night":
        lighting = "darkness punctuated by " + lighting.lower()

    # Textures from family
    textures = TEXTURE_FAMILIES.get(texture_family, TEXTURE_FAMILIES["architectural"])

    # Atmosphere: map mood to atmosphere type, influenced by indoor/outdoor
    mood_to_atmo = {
        "tense": "tense",
        "calm": "intimate",
        "urgent": "busy",
        "somber": "empty",
        "neutral": "vast" if indoor_outdoor == "outdoor" else "intimate",
    }
    atmo_key = mood_to_atmo.get(mood, "intimate")
    atmosphere = ATMOSPHERE_TYPES[atmo_key]

    # Palette
    palette = PALETTE_MODES.get(palette_mode, PALETTE_MODES["cool"])

    return {
        "lighting": lighting[0].upper() + lighting[1:],
        "textures": textures,
        "atmosphere": atmosphere,
        "palette": palette,
    }


# ------------------------------------------------------------------
# Scene opener — derived from narration cues
# ------------------------------------------------------------------

def _build_scene_opener(analysis):
    """
    Derive a scene-setting opener from the narration analysis.
    Uses cinematic framing language to establish the scene.
    """
    content = analysis["content_words"]
    indoor_outdoor = analysis["indoor_outdoor"]
    texture = analysis["texture_family"]

    texture_detail = TEXTURE_FAMILIES.get(texture, TEXTURE_FAMILIES["architectural"])
    first_texture = texture_detail.split(",")[0].strip()

    if content:
        subject_phrase = " ".join(content[:3])
    else:
        subject_phrase = "the unfolding scene"

    if indoor_outdoor == "outdoor":
        openers = [
            f"an open expanse where {first_texture} meets the horizon, the setting of {subject_phrase} stretching into the distance",
            f"a wide landscape where the elements of {subject_phrase} are laid bare under an open sky",
            f"a sprawling exterior where natural light reveals every surface connected to {subject_phrase}",
        ]
    else:
        openers = [
            f"an enclosed space where {first_texture} catches the available light, the domain of {subject_phrase}",
            f"an interior where the details of {subject_phrase} fill the frame from edge to edge",
            f"a contained environment defined by surfaces of {first_texture}, the setting for {subject_phrase}",
        ]

    return random.choice(openers)


# ------------------------------------------------------------------
# Action description — derived from narration content
# ------------------------------------------------------------------

def _narration_to_visual_action(narration_text, scene_context, analysis):
    """
    Convert narration text into a visual action description.
    Extracts key subject and action from the narration and constructs
    a cinematic visual description — no hardcoded topic mappings.
    """
    actions = analysis["actions"]
    content_words = analysis["content_words"]

    cinematic_verb = random.choice(_CINEMATIC_VERBS)

    if actions and content_words:
        subject_words = " ".join(content_words[:3])
        return (
            f"The visual narrative {cinematic_verb} as the scene depicts {subject_words}, "
            f"each element in the frame reinforcing the subject, building a layered "
            f"documentary composition with deliberate pacing"
        )
    elif content_words:
        subject_words = " ".join(content_words[:4])
        return (
            f"The scene {cinematic_verb}, revealing {subject_words} "
            f"through carefully composed documentary imagery, "
            f"visual details accumulating gradually, each adding weight to the narrative"
        )
    else:
        return (
            f"The scene unfolds with deliberate pacing, each element in the frame "
            f"reinforcing the gravity of the narration, "
            f"visual details accumulate gradually, building a layered documentary composition"
        )


# ------------------------------------------------------------------
# Character builder — derived from narration context
# ------------------------------------------------------------------

def _build_character(analysis, used_descriptions):
    """
    Build a character/subject description from narration context.
    Uses physical cues only (LTX-2.3 rule). Avoids recently used descriptions.
    """
    persons = analysis["persons"]
    content_words = analysis["content_words"]

    posture = random.choice(_POSTURE_DETAILS)
    hands = random.choice(_HAND_DETAILS)
    gaze = random.choice(_GAZE_DETAILS)

    if persons:
        person_label = random.choice(persons)
        desc = (
            f"A {person_label} occupies the frame, {posture}. "
            f"Their {hands.lower()}, {gaze.lower()}."
        )
    elif content_words:
        context_hint = " ".join(content_words[:2])
        desc = (
            f"A solitary figure connected to {context_hint} stands in frame, "
            f"{posture}. {hands[0].upper()}{hands[1:]}, {gaze.lower()}."
        )
    else:
        desc = (
            f"A solitary figure occupies the frame, {posture}. "
            f"{hands[0].upper()}{hands[1:]}, {gaze.lower()}."
        )

    # If this exact description was recently used, regenerate with different details
    if desc in used_descriptions:
        posture = random.choice([p for p in _POSTURE_DETAILS if p != posture] or _POSTURE_DETAILS)
        gaze = random.choice([g for g in _GAZE_DETAILS if g != gaze] or _GAZE_DETAILS)
        if persons:
            desc = (
                f"A {random.choice(persons)} occupies the frame, {posture}. "
                f"Their {hands.lower()}, {gaze.lower()}."
            )
        else:
            desc = (
                f"A solitary figure occupies the frame, {posture}. "
                f"{hands[0].upper()}{hands[1:]}, {gaze.lower()}."
            )

    return desc


# ------------------------------------------------------------------
# Style signature — always Arri Alexa base, palette from analysis
# ------------------------------------------------------------------

def _compose_style_signature(analysis):
    """Generate the photorealistic style signature for the prompt."""
    palette = PALETTE_MODES.get(analysis["palette_mode"], PALETTE_MODES["cool"])
    return (
        "Photorealistic cinematic documentary footage, shot on Arri Alexa with Cooke anamorphic lenses, "
        f"natural film grain, shallow depth of field, {palette} color palette, "
        "documentary-style composition with deliberate negative space."
    )


# ------------------------------------------------------------------
# Duration padding — extra detail for longer clips
# ------------------------------------------------------------------

def _add_detail_for_duration(analysis):
    """Add extra environmental detail for longer clips so prompt matches duration."""
    texture = TEXTURE_FAMILIES.get(analysis["texture_family"], TEXTURE_FAMILIES["architectural"])
    texture_items = [t.strip() for t in texture.split(",")]
    picked = random.sample(texture_items, min(2, len(texture_items)))

    mood = analysis["mood"]
    mood_ambient = {
        "tense": "A faint vibration travels through the surfaces, the space holding its breath.",
        "calm": "Light shifts almost imperceptibly, marking the passage of a quiet moment.",
        "urgent": "Movement in the periphery keeps the eye restless, the frame alive with secondary motion.",
        "somber": "Stillness settles over every surface, the frame holding its breath in muted silence.",
    }
    ambient = mood_ambient.get(mood, "Small ambient details emerge in the periphery of the frame.")

    content = analysis["content_words"]
    if content:
        subject_ref = " ".join(content[:2])
        return (
            f"In the margins of the frame, {picked[0]} catches a glint of available light, "
            f"a quiet reminder of {subject_ref}. {ambient} "
            f"The documentary gaze lingers, letting texture and atmosphere speak."
        )
    return (
        f"In the margins of the frame, {picked[0]} catches a glint of available light. "
        f"{ambient} The documentary gaze lingers, letting texture and atmosphere speak."
    )


# ------------------------------------------------------------------
# Shot and camera selection helpers
# ------------------------------------------------------------------

def _select_shot_type(dramatic_position, previous_shot_type):
    """Select shot type based on dramatic position, avoiding repetition."""
    preferred = SHOT_PROGRESSION.get(dramatic_position, SHOT_TYPES)
    available = [s for s in preferred if s != previous_shot_type]
    if not available:
        available = [s for s in SHOT_TYPES if s != previous_shot_type]
    return random.choice(available)


def _select_camera_movement(previous_movement):
    """Select camera movement, avoiding repetition."""
    available = [m for m in CAMERA_MOVEMENTS if m != previous_movement]
    return random.choice(available)


# ===================================================================
# Scene Visual State — tracks continuity within a scene
# ===================================================================

class SceneVisualState:
    """Tracks visual continuity state across clips within a single scene."""

    def __init__(self, scene_num, total_clips):
        self.scene_num = scene_num
        self.total_clips = total_clips
        self.previous_shot_type = None
        self.previous_camera_movement = None
        self.current_palette = None
        self.character_descriptions_used = []
        self.clip_index = 0
        self._analysis_cache = None

    def get_dramatic_position(self, clip_index):
        """Determine dramatic arc position based on clip placement in scene."""
        if self.total_clips <= 1:
            return "opening"
        ratio = clip_index / max(1, self.total_clips - 1)
        if ratio < 0.15:
            return "opening"
        elif ratio < 0.4:
            return "rising"
        elif ratio < 0.65:
            return "climax"
        elif ratio < 0.85:
            return "falling"
        else:
            return "closing"

    def advance(self, shot_type, camera_movement, character_desc):
        """Record what was used for this clip and advance."""
        self.previous_shot_type = shot_type
        self.previous_camera_movement = camera_movement
        if character_desc:
            self.character_descriptions_used.append(character_desc)
        self.clip_index += 1


# ===================================================================
# Core prompt length calibration
# ===================================================================

def _scale_prompt_length(duration_sec):
    """
    Determine target sentence count and word count based on clip duration.
    LTX-2.3 rule: prompt length must match video duration.
    """
    if duration_sec <= 3:
        return 3, 4, 80, 120       # min_sent, max_sent, min_words, max_words
    elif duration_sec <= 5:
        return 4, 6, 120, 170
    elif duration_sec <= 8:
        return 6, 8, 150, 220
    elif duration_sec <= 11:
        return 8, 10, 200, 260
    else:
        return 10, 14, 230, 300


# ===================================================================
# Main prompt generation function — single clip
# ===================================================================

def generate_prompt(narration_text, scene_title, scene_context,
                    target_duration_sec, clip_index, total_clips_in_scene,
                    previous_shot_type=None, voice=None, scene_state=None):
    """
    Generate a single LTX-2.3 video prompt using NLP-based narration analysis.

    Produces a single flowing paragraph in present tense, 150-250 words,
    with explicit camera movement, physical textures, ambient audio, and
    no emotional labels.

    Args:
        narration_text: the narration being spoken during this clip
        scene_title: the scene title for context
        scene_context: broader scene narrative context
        target_duration_sec: how long the video clip should be
        clip_index: position of this clip within the scene (0-based)
        total_clips_in_scene: total clips in this scene
        previous_shot_type: shot type used in previous clip (to vary)
        voice: narration voice label (used as label only, no topic-specific mapping)
        scene_state: SceneVisualState for continuity tracking

    Returns:
        dict with 'prompt', 'shot_type', 'environment', 'camera_movement',
        'word_count', 'generation_params'
    """
    min_sent, max_sent, min_words, max_words = _scale_prompt_length(target_duration_sec)

    # NLP analysis of narration content
    analysis = _analyze_narration(narration_text, scene_title)

    # Build environment from analysis
    env = _build_environment(analysis)

    # Determine dramatic position and continuity state
    if scene_state:
        dramatic_pos = scene_state.get_dramatic_position(clip_index)
        prev_shot = scene_state.previous_shot_type or previous_shot_type
        prev_cam = scene_state.previous_camera_movement
        used_chars = scene_state.character_descriptions_used
    else:
        dramatic_pos = "opening" if clip_index == 0 else ("closing" if clip_index == total_clips_in_scene - 1 else "rising")
        prev_shot = previous_shot_type
        prev_cam = None
        used_chars = []

    # Layer 1: Shot type — driven by dramatic arc
    shot = _select_shot_type(dramatic_pos, prev_shot)

    # Layer 2: Scene/environment with textures
    opener = _build_scene_opener(analysis)
    env_sentence = f"{env['lighting']}, {env['textures']}."

    # Layer 3: Action — narration-derived visual description
    action_sentence = _narration_to_visual_action(narration_text, scene_context, analysis)

    # Layer 4: Character — physical cues, no emotions, varied across scene
    character_sentence = _build_character(analysis, used_chars)

    # Layer 5: Camera movement — varied, no repetition
    cam_move = _select_camera_movement(prev_cam)

    # Layer 6: Audio description
    audio_sentence = f"The ambient sound is {env['atmosphere']}."

    # Assemble into single flowing paragraph
    parts = [
        f"{shot} of {opener}.",
        env_sentence,
        action_sentence + ".",
        character_sentence,
        cam_move + ".",
        audio_sentence,
    ]

    # Style signature
    parts.append(_compose_style_signature(analysis))

    # Build prompt
    prompt = " ".join(p.strip() for p in parts if p.strip())

    # Clean up double periods, extra spaces
    prompt = prompt.replace("..", ".").replace(". .", ".").replace("  ", " ")

    # Pad for longer clips if under word target
    word_count = len(prompt.split())
    if word_count < min_words and target_duration_sec > 5:
        extra = _add_detail_for_duration(analysis)
        prompt = prompt.rstrip(".") + ". " + extra
        word_count = len(prompt.split())

    # For very long clips, add even more narration-derived detail
    if word_count < min_words and target_duration_sec > 10:
        content = analysis["content_words"]
        if content:
            detail_subject = " ".join(content[:3])
            prompt = prompt.rstrip(".") + (
                f". Additional detail draws the eye to elements of {detail_subject}, "
                f"each contributing to the layered documentary texture."
            )
        else:
            prompt = prompt.rstrip(".") + (
                ". Additional detail draws the eye to surfaces and objects in the periphery, "
                "each contributing to the layered documentary texture."
            )
        word_count = len(prompt.split())

    # Derive environment label from analysis (for metadata)
    env_label = f"{analysis['indoor_outdoor']}_{analysis['texture_family']}_{analysis['mood']}"

    # Update scene state for continuity
    if scene_state:
        scene_state.advance(shot, cam_move, character_sentence)

    return {
        "prompt": prompt,
        "shot_type": shot,
        "environment": env_label,
        "camera_movement": cam_move,
        "duration_sec": target_duration_sec,
        "word_count": len(prompt.split()),
        "generation_params": {
            "dramatic_position": dramatic_pos,
            "voice": voice or "narrator",
            "palette": env["palette"],
        },
    }


# ===================================================================
# Scene-level and full-pipeline prompt generation
# ===================================================================

def generate_prompts_for_scene(scene_num, scene_title, audio_segments,
                               narration_texts, scene_context="",
                               voices=None):
    """
    Generate video prompts for all clips in a scene based on audio timing.

    Args:
        scene_num: int
        scene_title: str
        audio_segments: list of dicts from otio_timeline.get_scene_audio_segments()
        narration_texts: list of str, the full narration text for each segment
        scene_context: broader context about the scene
        voices: list of voice labels per segment (used as labels only)

    Returns:
        list of prompt dicts ready for video generation and OTIO storage
    """
    prompts = []
    total_clips = len(audio_segments)
    scene_state = SceneVisualState(scene_num, total_clips)

    for i, seg in enumerate(audio_segments):
        dur = seg["duration_sec"]
        narr_text = narration_texts[i] if i < len(narration_texts) else seg.get("text_preview", "")
        voice = (voices[i] if voices and i < len(voices)
                 else seg.get("voice", None))

        # LTX-2.3 generates ~5.04s clips; for longer, we need frame-chaining
        generation_duration = dur + 0.5
        ltx_clips_needed = max(1, int(generation_duration / 5.04) + (1 if generation_duration % 5.04 > 0.5 else 0))

        result = generate_prompt(
            narration_text=narr_text,
            scene_title=scene_title,
            scene_context=scene_context,
            target_duration_sec=dur,
            clip_index=i,
            total_clips_in_scene=total_clips,
            voice=voice,
            scene_state=scene_state,
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
            "camera_movement": result["camera_movement"],
            "word_count": result["word_count"],
            "generation_params": result["generation_params"],
            "narration_text": narr_text[:200],
            "audio_start_sec": seg.get("start_sec", 0),
            "audio_end_sec": seg.get("end_sec", 0),
        })

    return prompts


def generate_all_prompts(otio_timeline, narration_data):
    """
    Generate video prompts for all scenes using OTIO audio timing,
    then store them directly on the OTIO timeline.

    Args:
        otio_timeline: OTIOTimeline instance (loaded, with audio track populated)
        narration_data: list of scene dicts with narration text

    Returns:
        list of all prompt dicts across all scenes
    """
    all_prompts = []
    scenes_audio = otio_timeline.get_all_scenes_audio_timing()

    # Build narration text lookup: scene_num -> list of (voice, text) tuples
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

        parsed = narr_lookup.get(scene_num, [])
        narr_texts = [seg_text for _, seg_text in parsed]
        voices = [voice for voice, _ in parsed]

        prompts = generate_prompts_for_scene(
            scene_num=scene_num,
            scene_title=scene_title,
            audio_segments=audio_segs,
            narration_texts=narr_texts,
            scene_context=scene.get("context", ""),
            voices=voices,
        )

        # Store prompts on OTIO timeline
        for p in prompts:
            otio_timeline.store_prompt_on_gap(
                scene_num=p["scene_number"],
                clip_index=p["clip_index"],
                prompt_data={
                    "prompt": p["prompt"],
                    "shot_type": p["shot_type"],
                    "environment": p["environment"],
                    "camera_movement": p["camera_movement"],
                    "word_count": p["word_count"],
                    "generation_params": p["generation_params"],
                },
            )

        all_prompts.extend(prompts)
        log.info(f"Scene {scene_num:2d} ({scene_title[:30]:30s}): {len(prompts)} prompts, "
                 f"avg {sum(p['word_count'] for p in prompts)/max(1,len(prompts)):.0f} words")

    # Save OTIO with embedded prompts
    otio_timeline.save()

    log.info(f"\nTotal: {len(all_prompts)} prompts across {len(scenes_audio)} scenes")
    log.info("Prompts stored on OTIO timeline as metadata (single source of truth)")
    return all_prompts


def parse_narration_segments_text(narration_text):
    """Parse narration text into (voice, text) tuples."""
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

    parser = argparse.ArgumentParser(description="LTX-2.3 project-agnostic prompt generator")
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

    # Generate prompts (stores on OTIO + returns list)
    all_prompts = generate_all_prompts(otio_tl, narration_data)

    # Also export to JSON (derivative, not primary source)
    otio_tl.export_prompts_json(args.output)

    log.info(f"Saved {len(all_prompts)} prompts to {args.output}")
    log.info(f"Prompts also stored on OTIO timeline: {args.otio}")


if __name__ == "__main__":
    main()
