#!/usr/bin/env python3
"""
LTX-2.3 Prompt Generator — Context-Aware Cinematic Composition Engine
========================================================================
Generates cinema-quality prompts for LTX-2.3 using a sophisticated rule-based
composition engine that understands narration context, scene continuity, dramatic
arc, and LTX-2.3's specific requirements.

NO external LLM API required — runs entirely on VMs without internet access.
The improvement is in prompt QUALITY through better template composition.

Reads audio timing from the OTIO timeline, then generates a video prompt
for each narration segment with:
  - Scene continuity tracking (consistent characters, environments, palettes)
  - Narration-context awareness (V1/V2/V3 voice treatment, concept parsing)
  - Duration-calibrated complexity (prompt length matches video duration)
  - Dramatic arc positioning (opening → rising → climax → falling → closing)
  - Shot type variation (no two adjacent clips with same shot type)

Six-Layer Prompt Structure:
  1. Shot establishment — cinematic framing (wide, close-up, tracking, etc.)
  2. Scene/environment — lighting, color, textures, atmosphere
  3. Action — visual metaphor or narration-derived visual action
  4. Character(s) — physical description, clothing, physical cues (NO emotions)
  5. Camera movement — how camera moves, what it reveals
  6. Audio description — ambient sound, environmental acoustics

Key LTX-2.3 rules:
  - Single flowing paragraph, present tense
  - 150-250 words per prompt for standard clips
  - Prompt length MUST match video duration (short prompt + long video = rushed)
  - NO text/logos on screen ever
  - NO emotional labels — use PHYSICAL CUES instead
  - NO numerical specifications — use natural language
  - For documentary: visual metaphors > literal depictions
  - Explicit camera movement in every prompt
  - Physical/material descriptions (textures, surfaces, light quality)
  - Ambient audio description in every prompt
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
    # --- New environments for expanded documentary coverage ---
    "refugee_camp": {
        "lighting": "Flat midday sun beating down on white canvas and corrugated metal, harsh shadows pooling beneath tarps",
        "textures": "mud-streaked canvas tents, frayed nylon rope, stacked water canisters with peeling UN markings, dusty gravel paths",
        "atmosphere": "the murmur of many voices layered beneath generator hum, children calling in the distance, canvas flapping in dry wind",
        "palette": "bleached canvas white, dust brown, faded blue tarp, humanitarian orange",
    },
    "naval_vessel": {
        "lighting": "Steel-gray overcast sky reflecting off a matte hull, salt spray catching scattered light",
        "textures": "riveted gray deck plates, heavy hatch mechanisms, anti-corrosion paint peeling along weld seams, thick mooring cable coiled on deck",
        "atmosphere": "the deep throb of turbine engines below deck, waves slapping against the hull, the crackle of radio communication on the bridge",
        "palette": "battleship gray, ocean dark blue, safety yellow railings, matte black weaponry",
    },
    "hospital": {
        "lighting": "Harsh overhead fluorescent strips casting flat white light with no shadows, punctuated by blinking monitor LEDs",
        "textures": "polished linoleum floors reflecting light, stainless steel equipment surfaces, stacked gauze and tape, curtain fabric dividers",
        "atmosphere": "the rhythmic beep of heart monitors, the squeak of rubber soles on linoleum, muffled voices behind curtains, ventilator hiss",
        "palette": "clinical white, surgical green, monitor blue, stainless steel silver",
    },
    "parliament": {
        "lighting": "Grand chandeliers casting warm amber light across rows of polished wooden benches, daylight filtered through tall arched windows",
        "textures": "carved wood paneling, leather bench seats worn smooth, marble columns, brass microphone stands, heavy bound legislation volumes",
        "atmosphere": "the echo of a single voice across a vast chamber, the rustle of papers, murmured consultation between seated delegates",
        "palette": "parliamentary green, polished oak brown, brass gold, marble white",
    },
    "protest_march": {
        "lighting": "Harsh afternoon sun cutting through a haze of dust and scattered smoke, long shadows stretching across asphalt",
        "textures": "hand-painted cardboard signs, worn shoe soles on cracked pavement, fabric banners rippling in wind, barricade mesh",
        "atmosphere": "rhythmic chanting rising and falling, car horns in the distance, helicopter blades chopping overhead, megaphone feedback",
        "palette": "asphalt gray, crowd-cloth earth tones, banner red, tear-gas haze yellow",
    },
    "underground_bunker": {
        "lighting": "A single overhead bulb swinging slightly, casting shifting shadows across reinforced concrete walls",
        "textures": "raw poured concrete with form-board imprints, metal ventilation ducts, stacked provisions in military crates, condensation beads on steel doors",
        "atmosphere": "a low constant hum of air filtration, the drip of condensation, muffled booms transmitted through thick walls, radio static",
        "palette": "bunker gray, bare concrete tan, emergency light red, institutional green",
    },
    "satellite_view": {
        "lighting": "The blue-white curvature of earth glowing against the absolute black of space, sunlight catching cloud formations in sharp relief",
        "textures": "swirling cloud patterns, the geometric precision of city grids at night, coastlines where green meets deep blue, desert sand ripples visible from altitude",
        "atmosphere": "a vast silence, the faintest electronic hum of satellite instrumentation, the sense of immense distance and scale",
        "palette": "earth blue, cloud white, ocean dark, landmass green-brown, city-light amber",
    },
    "grain_field": {
        "lighting": "Golden hour sunlight streaming low across an endless expanse of wheat, each stalk casting its own long shadow",
        "textures": "ripe grain heads heavy and bending, dry chaff in the air, cracked earth visible between rows, a distant combine harvester trailing dust",
        "atmosphere": "the whisper of wind through tall grain, the distant mechanical drone of harvest equipment, insects buzzing in warm air",
        "palette": "wheat gold, harvest amber, sun-bleached straw, rich earth brown, clear sky blue",
    },
    "gas_station": {
        "lighting": "Fluorescent canopy light pooling on wet concrete at dusk, neon price signs glowing against a dimming sky",
        "textures": "cracked concrete forecourt, weathered pump housings, scratched price displays with changing digits, oil-stained asphalt",
        "atmosphere": "the mechanical clunk of pump handles, fuel vapor in still air, a distant highway drone, the flicker of a failing overhead tube",
        "palette": "forecourt concrete gray, neon price-sign green, fuel-pump silver, dusk purple-orange",
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
    # --- New concept visuals for expanded coverage ---
    "blockade": ["a heavy chain stretched across a harbor entrance, links rusting in salt air",
                 "cargo ships anchored motionless in a long queue stretching to the horizon",
                 "a barrier gate lowering across a road as vehicles brake to a halt",
                 "empty store shelves stretching into the distance under fluorescent light"],
    "refugee": ["a worn suitcase sitting alone on a gravel path between rows of tents",
                "small hands gripping a chain-link fence, knuckles white against the wire",
                "a long queue of silhouetted figures moving slowly along a dusty road at dawn",
                "a child's drawing pinned to canvas, depicting a house with smoke rising from it"],
    "missile": ["a contrail scratching a white line across an otherwise clear blue sky",
                "a radar screen sweeping in a dark room, a single bright dot tracking across the display",
                "shattered window glass scattered across a floor, daylight pouring through the empty frame",
                "a silo door grinding open, mechanical arms lifting toward vertical"],
    "sanction": ["a red stamp pressing firmly onto a customs document, ink spreading into the paper fibers",
                 "a ship turning in open water, reversing course against the current",
                 "a bank vault door swinging shut in slow motion, its massive bolts engaging one by one",
                 "a handshake interrupted mid-grip, two hands pulling apart"],
    "alliance": ["two flags rising on adjacent poles, fabric unfurling against the same wind",
                 "a conference table viewed from above, papers and water glasses arranged in parallel rows",
                 "military vehicles from different nations parked in formation, each bearing distinct markings",
                 "a signed document with multiple pens arranged in a fan beneath the signatures"],
    "ceasefire": ["a weapon laid down on bare ground, hands withdrawing slowly from the frame",
                  "a clock face showing the exact moment, second hand frozen mid-tick",
                  "a white flag catching wind against a backdrop of smoke-stained buildings",
                  "two figures standing at opposite ends of a bridge, the space between them still and empty"],
    "escalation": ["a thermometer rising, mercury climbing past marked thresholds",
                   "successive doors slamming shut in a long corridor, each louder than the last",
                   "a map with expanding red zones, borders shifting outward frame by frame",
                   "hands gripping a railing tighter, knuckles whitening as the grip intensifies"],
    "humanitarian": ["boxes bearing red cross markings being unloaded from a cargo plane onto tarmac",
                     "a water tanker pouring into rows of waiting containers held by outstretched hands",
                     "a medical worker in a white coat pressing a stethoscope to a small chest",
                     "blankets being distributed from the back of a truck, arms reaching upward"],
    "propaganda": ["a printing press churning out identical sheets at high speed, ink still wet and gleaming",
                   "a loudspeaker mounted on a pole, its cone angled down toward an empty street",
                   "a television screen showing the same face from multiple angles simultaneously",
                   "posters layered on a wall, each partially covering the one beneath it"],
    "intelligence": ["a satellite dish rotating in the dark, its surface catching starlight",
                     "a hand placing a photograph face-down on a desk already covered in documents",
                     "a screen displaying intercepted communications, green text scrolling on black",
                     "a figure photographing documents through a partially open door, lens catching light"],
}


# ------------------------------------------------------------------
# Scene continuity state — tracks visual elements across clips in a scene
# ------------------------------------------------------------------
class SceneContinuityState:
    """Tracks visual continuity within a scene to ensure consistent characters,
    environments, and varied shot types across sequential clips."""

    def __init__(self, scene_num, total_clips):
        self.scene_num = scene_num
        self.total_clips = total_clips
        self.environment_key = None
        self.palette = None
        self.previous_shot_type = None
        self.character_variant_index = 0
        self.established_characters = []

    def get_dramatic_position(self, clip_index):
        """Determine where this clip sits in the dramatic arc of the scene.

        Returns one of: opening, rising, climax, falling, closing
        """
        if self.total_clips <= 1:
            return "opening"
        ratio = clip_index / max(1, self.total_clips - 1)
        if ratio < 0.15:
            return "opening"
        elif ratio < 0.40:
            return "rising"
        elif ratio < 0.65:
            return "climax"
        elif ratio < 0.85:
            return "falling"
        else:
            return "closing"

    def advance(self, shot_type, character_desc):
        """Advance the state after generating a clip."""
        self.previous_shot_type = shot_type
        self.character_variant_index += 1
        if character_desc and character_desc not in self.established_characters:
            self.established_characters.append(character_desc)


# ------------------------------------------------------------------
# Voice-based visual treatment
# ------------------------------------------------------------------
VOICE_TREATMENTS = {
    "V1": {
        "style": "data-driven",
        "preferred_envs": ["trading_floor", "financial_district", "data_center"],
        "visual_bias": "screens, charts, numerical displays, digital interfaces",
        "detail_focus": "The display surfaces glow with shifting data, reflections playing across glass and polished surfaces",
    },
    "V2": {
        "style": "analytical",
        "preferred_envs": ["government_office", "military_hardware", "newsroom", "parliament"],
        "visual_bias": "maps, documents, strategic displays, official spaces",
        "detail_focus": "Documents and maps spread across surfaces, the weight of analysis visible in the arrangement of evidence",
    },
    "V3": {
        "style": "historical",
        "preferred_envs": ["street_market", "kitchen_domestic", "war_zone", "refugee_camp"],
        "visual_bias": "archival textures, weathered surfaces, the passage of time",
        "detail_focus": "Worn surfaces carry the patina of time, each scratch and stain suggesting the accumulation of years",
    },
}


def _pick_environment(narration_text, scene_title, voice=None):
    """Select the most fitting environment based on narration content and voice.

    Uses weighted keyword scoring with voice-based preference boosting.
    """
    text = (narration_text + " " + scene_title).lower()

    scoring = {
        "trading_floor": ["trade", "stock", "market", "dow", "nasdaq", "exchange", "ticker", "portfolio", "index", "equity", "share", "derivative"],
        "war_zone": ["bomb", "missile", "attack", "destruction", "rubble", "casualt", "soldier", "combat", "strike", "airstrike", "shell", "crater"],
        "government_office": ["policy", "regulation", "senator", "congress", "legislation", "bureaucr", "official", "minister", "decree", "executive order"],
        "street_market": ["price", "grocery", "consumer", "inflation", "cost of living", "food", "bread", "market stall", "vendor", "shopkeeper"],
        "oil_refinery": ["oil", "petroleum", "barrel", "crude", "refinery", "pipeline", "opec", "fuel", "petrochemical", "drilling"],
        "kitchen_domestic": ["family", "kitchen", "home", "breakfast", "morning", "coffee", "everyday", "household", "child", "dinner table"],
        "data_center": ["data", "algorithm", "crypto", "bitcoin", "blockchain", "digital", "server", "cyber", "network", "bandwidth"],
        "shipping_port": ["shipping", "container", "cargo", "supply chain", "port", "export", "import", "vessel", "freight", "logistics"],
        "financial_district": ["bank", "wall street", "finance", "investment", "billion", "hedge fund", "capital", "treasury", "federal reserve", "central bank"],
        "abstract_metaphor": ["concept", "abstract", "metaphor", "idea", "fundamental", "system", "theory", "paradigm"],
        "military_hardware": ["weapon", "defense", "military", "arms", "tank", "aircraft", "ammunition", "contract", "arsenal", "warhead"],
        "newsroom": ["report", "breaking", "headline", "anchor", "broadcast", "coverage", "media", "press conference", "correspondent"],
        "refugee_camp": ["refugee", "displaced", "humanitarian", "tent", "camp", "flee", "asylum", "shelter", "migration", "crisis"],
        "naval_vessel": ["navy", "naval", "fleet", "carrier", "destroyer", "strait", "maritime", "blockade", "submarine", "frigate"],
        "hospital": ["hospital", "medical", "wound", "surgeon", "triage", "ambulance", "patient", "clinic", "casualty ward", "medic"],
        "parliament": ["parliament", "debate", "vote", "resolution", "assembly", "chamber", "speaker", "delegation", "ratif"],
        "protest_march": ["protest", "demonstrat", "march", "rally", "riot", "uprising", "dissent", "crowd", "tear gas", "barricade"],
        "underground_bunker": ["bunker", "underground", "shelter", "fallout", "command center", "fortif", "reinforced", "tunnel"],
        "satellite_view": ["satellite", "aerial", "orbit", "global", "overview", "surveil", "reconnaiss", "earth", "hemisphere"],
        "grain_field": ["grain", "wheat", "harvest", "crop", "famine", "agriculture", "farm", "food supply", "drought", "arable"],
        "gas_station": ["gas station", "petrol", "pump", "fuel price", "gallon", "diesel", "at the pump", "fill up", "forecourt"],
    }

    best_env = "abstract_metaphor"
    best_score = 0

    for env_key, keywords in scoring.items():
        score = sum(1 for kw in keywords if kw in text)
        # Boost score for voice-preferred environments
        if voice and voice in VOICE_TREATMENTS:
            if env_key in VOICE_TREATMENTS[voice]["preferred_envs"]:
                score += 0.5
        if score > best_score:
            best_score = score
            best_env = env_key

    return best_env


def _pick_visual_metaphor(narration_text):
    """Select a visual metaphor based on narration content.

    Scans for concept keywords and returns an appropriate visual description.
    """
    text = narration_text.lower()

    for concept, visuals in CONCEPT_VISUALS.items():
        if concept in text:
            return random.choice(visuals)

    return None


def _scale_prompt_complexity(duration_sec):
    """Determine target sentence count and word range based on clip duration.

    LTX-2.3 rule: prompt length must match video duration.
    Short prompt + long video = model rushes through content.

    Returns (min_sentences, max_sentences, min_words, max_words)
    """
    if duration_sec <= 3:
        return 3, 4, 50, 100
    elif duration_sec <= 5:
        return 4, 6, 80, 150
    elif duration_sec <= 8:
        return 6, 8, 130, 220
    elif duration_sec <= 11:
        return 8, 10, 180, 250
    else:
        return 10, 14, 220, 320


def _extract_key_concepts(text):
    """Extract multiple key concepts from narration text for richer visual composition."""
    text_lower = text.lower()
    found = []
    for concept in CONCEPT_VISUALS:
        if concept in text_lower:
            found.append(concept)
    if not found:
        words = [w for w in text.split()[:12] if len(w) > 3]
        return [" ".join(words[:4])] if words else ["the unfolding situation"]
    return found


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
        "refugee_camp": "a sprawling encampment of white tents and corrugated shelters stretching across a dusty plain",
        "naval_vessel": "the gray steel deck of a warship cutting through dark water under an overcast sky",
        "hospital": "a clinical corridor where fluorescent light reflects off polished floors and stainless steel",
        "parliament": "a grand legislative chamber where tiered wooden benches rise beneath a vaulted ceiling",
        "protest_march": "a wide boulevard filled with a moving mass of figures carrying signs and banners",
        "underground_bunker": "a reinforced concrete chamber where a single overhead light casts hard-edged shadows",
        "satellite_view": "the curved edge of the earth seen from above, city lights tracing coastlines against dark ocean",
        "grain_field": "an endless golden expanse of ripe wheat bending under a low afternoon sun",
        "gas_station": "a fuel station forecourt where fluorescent canopy light pools on wet concrete at dusk",
    }
    return openers.get(env_key, "a carefully composed documentary scene")


def _narration_to_visual_action(narration_text, scene_context, env_key, dramatic_position):
    """Convert narration text into a visual action description.

    Context-aware: uses dramatic position and environment to create
    more varied and appropriate visual descriptions.
    """
    text = narration_text.strip()
    if len(text) > 200:
        text = text[:200]

    # Dramatic-position-aware pacing
    pacing = {
        "opening": "The scene reveals itself gradually, elements emerging from stillness as",
        "rising": "Activity builds within the frame, each element adding urgency as",
        "climax": "The full weight of the moment presses into the frame as",
        "falling": "The pace eases as the scene settles, residual tension visible as",
        "closing": "The scene draws toward quiet resolution, final details lingering as",
    }

    pace_prefix = pacing.get(dramatic_position, "The scene unfolds with deliberate pacing as")

    # Concept-specific visual translations
    replacements = [
        ("billion dollars", "a display fills with accumulating figures, each digit clicking into place"),
        ("percent", "a graph line shifts sharply, its trajectory changing the shape of the composition"),
        ("inflation", "price tags flip in sequence along a market row, each showing a higher figure than the last"),
        ("interest rate", "a dial turns clockwise with visible mechanical resistance, its needle crossing a threshold"),
        ("sanctions", "a red stamp descends onto paper, the impact sending ripples through a stack of documents"),
        ("supply chain", "containers advance along a conveyor system, their spacing growing wider with each passing moment"),
        ("deficit", "a balance scale tips decisively to one side, the heavier pan settling with finality"),
        ("GDP", "a series of bar segments stack upward in the frame, their heights telling a story of expansion and contraction"),
        ("blockade", "a heavy gate lowers across a passage, the space beyond it visible but unreachable"),
        ("ceasefire", "hands withdraw from equipment and hang at sides, fingers uncurling slowly"),
        ("escalation", "successive layers of activity pile onto the frame, each more intense than the one beneath"),
        ("alliance", "parallel movements synchronize across the frame, separate actions falling into shared rhythm"),
    ]

    for phrase, replacement in replacements:
        if phrase.lower() in text.lower():
            return (
                f"{pace_prefix} {replacement}. "
                f"The broader composition reinforces the weight of what unfolds"
            )

    return (
        f"{pace_prefix} visual details accumulate gradually in the frame, "
        f"building a layered documentary composition where each element "
        f"reinforces the gravity of the narration"
    )


def _generate_character_layer(narration_text, env_key, variant_index=0, dramatic_position="rising"):
    """Generate character/subject descriptions using physical cues only.

    Varies character presentation based on variant index and dramatic position
    to avoid repetition across clips in the same scene.
    """
    # Multiple character variants per environment for variety
    char_variants = {
        "trading_floor": [
            (
                "A trader in a rumpled white dress shirt, sleeves rolled to the elbows, "
                "presses his palm against his forehead and exhales slowly through pursed lips. "
                "His tie hangs loose, collar unbuttoned, dark circles visible under the monitor glow."
            ),
            (
                "A woman in a charcoal blazer stands behind a desk, one hand braced flat on the surface, "
                "the other holding a phone receiver away from her ear. Her jaw is set, chin slightly raised, "
                "eyes tracking a descending line on the nearest screen."
            ),
            (
                "Two figures lean toward the same monitor, shoulders almost touching, their hands "
                "hovering over keyboards without typing. The blue light of the display casts sharp shadows "
                "under their brows, both pairs of eyes fixed on the same point."
            ),
        ],
        "war_zone": [
            (
                "A figure in a dust-covered jacket stands with shoulders hunched, "
                "one hand gripping a crumbling doorframe for balance. "
                "Grime lines the creases of their face, their gaze fixed on a point in the middle distance."
            ),
            (
                "A person crouches beside a shattered wall, arms wrapped around drawn-up knees, "
                "a thin blanket draped over one shoulder. Dust has settled in the folds of their clothing, "
                "and their boots are caked with pale debris."
            ),
            (
                "A helmeted figure gestures with one arm toward a distant point, "
                "the other hand resting on a radio clipped to their vest. "
                "Their stance is wide, weight shifted forward, boots planted in loose rubble."
            ),
        ],
        "government_office": [
            (
                "A silver-haired official in a dark suit adjusts a stack of folders with deliberate, "
                "measured movements. Reading glasses perched at the tip of their nose, "
                "a fountain pen held motionless between two fingers."
            ),
            (
                "A young aide in a pressed white shirt stands near a bookshelf, a leather portfolio "
                "clutched to their chest, head slightly bowed as if waiting for instruction. "
                "Their shoes are polished to a high shine against the dark carpet."
            ),
        ],
        "street_market": [
            (
                "A vendor with weathered hands and a faded apron leans forward to rearrange a display, "
                "fingers moving with practiced efficiency. Deep lines frame their eyes, "
                "squinting against the morning sun."
            ),
            (
                "A shopper holds a small paper bag close to their body, one hand sorting through coins "
                "in an open palm. Their shoulders are drawn in, posture compact, "
                "navigating the narrow space between stalls."
            ),
        ],
        "oil_refinery": [
            (
                "A worker in a hard hat and high-visibility vest walks along a catwalk, "
                "one gloved hand trailing along the railing. "
                "Their boots leave prints on the metal grating."
            ),
            (
                "A figure in flame-retardant coveralls leans against a pipe junction, clipboard in hand, "
                "pen tapping against the metal surface. Their face is partially obscured by a lowered visor, "
                "the orange of the vest bright against the industrial gray."
            ),
        ],
        "kitchen_domestic": [
            (
                "A middle-aged person in a worn cardigan pauses mid-motion, coffee mug suspended halfway "
                "to their lips, eyes fixed on a newspaper headline. Their other hand rests flat on the table, "
                "fingers spread wide as if bracing against the surface."
            ),
            (
                "A child sits at the kitchen table, legs dangling from a chair too tall, "
                "a cereal bowl pushed aside. Their small hands rest on an open schoolbook, "
                "but their gaze is turned toward the window."
            ),
        ],
        "data_center": [
            (
                "A technician in a dark polo shirt peers at a rack display, "
                "the screen's light casting sharp shadows across their focused expression. "
                "Their fingers hover over a keyboard without pressing a key."
            ),
            (
                "A figure pushes a rolling cart between server rows, cables coiled neatly on the top shelf. "
                "They pause to check a blinking amber indicator, tilting their head to read a label "
                "in the dim aisle lighting."
            ),
        ],
        "shipping_port": [
            (
                "A dockworker in a heavy coat and safety vest signals with broad arm gestures, "
                "the wind catching the loose fabric of their clothing. "
                "Salt-weathered skin and squinting eyes against the harbor glare."
            ),
        ],
        "financial_district": [
            (
                "Suited pedestrians move through the frame, their pace quickening, "
                "briefcases gripped tighter, phone screens illuminating downcast faces. "
                "One figure stands still against the flow, staring upward at a ticker display."
            ),
            (
                "A woman in a long coat walks briskly along the sidewalk, scarf pulled tight, "
                "portfolio bag swinging with each stride. She passes a window where a stock chart "
                "fills a large display, her reflection superimposed over the graph."
            ),
        ],
        "military_hardware": [
            (
                "A uniformed figure stands at attention beside a vehicle, "
                "arms clasped behind their back, chin slightly raised. "
                "The fabric of their uniform is pressed and rigid, catching hard shadows."
            ),
        ],
        "newsroom": [
            (
                "An anchor sits straight-backed behind the desk, papers squared precisely, "
                "maintaining composed stillness as studio lights cast a bright wash over the set. "
                "Their eyes track a teleprompter with controlled precision."
            ),
        ],
        "refugee_camp": [
            (
                "A woman sits on an overturned crate outside a tent, a bundled child resting "
                "against her shoulder. Her free hand smooths the edge of a worn blanket, "
                "fingers working the fabric with slow, repetitive motion."
            ),
            (
                "An aid worker in a khaki vest kneels beside a water distribution point, "
                "one hand steadying a jerrycan while the other adjusts the tap. "
                "Sweat traces clean lines through the dust on their forearms."
            ),
        ],
        "naval_vessel": [
            (
                "An officer stands on the bridge, both hands resting on the console edge, "
                "binoculars hanging from a strap around their neck. Their stance is wide for balance, "
                "weight shifting subtly with the motion of the deck."
            ),
        ],
        "hospital": [
            (
                "A surgeon in a blue gown stands beneath an overhead light, gloved hands held up "
                "at chest height, fingers spread. A surgical mask obscures all but their eyes, "
                "which are focused downward with absolute concentration."
            ),
        ],
        "parliament": [
            (
                "A delegate rises from a bench, one hand gripping the microphone stand, "
                "the other holding a folded sheet of paper. Their posture straightens as they begin, "
                "shoulders squared against the vast space of the chamber."
            ),
        ],
        "protest_march": [
            (
                "A young person at the front of the crowd holds a cardboard sign above their head, "
                "arms locked straight, chin lifted. The wind catches their hair and the edge of the sign, "
                "their feet planted firmly on the cracked asphalt."
            ),
        ],
        "underground_bunker": [
            (
                "A figure hunches over a radio console, headphones clamped tight, "
                "one hand turning a dial with minute precision. The green glow of the frequency display "
                "is the only color on their face, everything else cast in bunker gray."
            ),
        ],
        "satellite_view": [
            (
                "No human figure is visible at this altitude, only the evidence of human presence — "
                "the geometric sprawl of cities, the scarred lines of roads, "
                "the dark spread of industrial zones against green hinterland."
            ),
        ],
        "grain_field": [
            (
                "A farmer stands at the edge of the field, one hand shading their eyes against the low sun, "
                "the other resting on a wooden fence post. Their boots are dusted with chaff, "
                "and their rolled shirtsleeves reveal tanned forearms."
            ),
        ],
        "gas_station": [
            (
                "A driver stands beside a pump, one hand on the nozzle, the other in a jacket pocket. "
                "They stare at the price display as digits tick upward, their jaw tightening, "
                "weight shifting from one foot to the other on the oil-stained concrete."
            ),
        ],
    }

    variants = char_variants.get(env_key, [
        "A solitary figure occupies the frame, their posture conveying the weight "
        "of the moment through subtle physical tension visible in their hands and shoulders."
    ])

    idx = variant_index % len(variants)
    return variants[idx]


def _add_detail_for_duration(narration_text, env_key, duration_sec, dramatic_position="rising"):
    """Add extra environmental detail for longer clips so prompt matches duration.

    Varies based on environment and dramatic position.
    """
    extras = {
        "trading_floor": [
            "Papers shift in the air-conditioning draft. A phone rings unanswered on a far desk. "
            "The timestamp on a corner monitor ticks forward. Reflections of chart patterns "
            "ripple across a glass partition.",
            "Coffee grows cold in a paper cup beside a keyboard. Post-it notes line the edge of "
            "a monitor bezel, their handwritten figures half-obscured by new ones stuck on top.",
        ],
        "war_zone": [
            "A curtain flutters through a broken window. Dust motes drift slowly through a shaft of light. "
            "A cracked mirror on a wall reflects fragmented sky. Water drips from exposed pipes.",
            "A child's bicycle lies on its side in the street, one wheel still turning slowly. "
            "Bullet pocks dot a concrete wall in a tight cluster near a doorframe.",
        ],
        "kitchen_domestic": [
            "Steam curls upward from the cup, catching golden light. A clock on the wall ticks audibly. "
            "Crumbs scatter across a breadboard. The refrigerator hum provides a bass note to the silence.",
        ],
        "refugee_camp": [
            "A line of water containers stretches between tents, each marked with a family number. "
            "Laundry hangs on improvised lines, faded colors bleached further by relentless sun.",
        ],
        "naval_vessel": [
            "Wake foam trails behind the hull in a widening V. A flag snaps taut on the mast, "
            "its fabric rigid in the sea wind. Radar arrays rotate in slow, continuous sweeps.",
        ],
        "hospital": [
            "IV bags sway slightly on their stands, tubes tracing paths to obscured patients. "
            "A clipboard hangs from the foot of a bed, pen clipped to its edge, ink smudged by many hands.",
        ],
        "parliament": [
            "Empty water glasses line the bench in front of each seat. A page turns in someone's notes, "
            "the sound carrying in the chamber's acoustics. Light from the high windows shifts slowly across the floor.",
        ],
        "gas_station": [
            "The price sign flickers once, its digits rearranging. A receipt curls from the pump slot, "
            "printing a figure that is longer than it was last week. Puddles of spilled fuel "
            "catch rainbow refractions under the canopy light.",
        ],
    }

    env_extras = extras.get(env_key, [
        "Small details emerge in the periphery — textures reveal themselves under scrutiny, "
        "ambient elements shift subtly, and the frame breathes with quiet, documentary patience."
    ])

    return random.choice(env_extras) if isinstance(env_extras, list) else env_extras


def _get_voice_detail(voice, env_key):
    """Generate voice-specific visual treatment detail.

    Different narration voices get different visual emphasis to reflect their
    perspective on the material.
    """
    treatment = VOICE_TREATMENTS.get(voice)
    if not treatment:
        return ""
    return treatment["detail_focus"]


def generate_prompt(narration_text, scene_title, scene_context,
                    target_duration_sec, clip_index, total_clips_in_scene,
                    previous_shot_type=None, voice=None,
                    continuity_state=None):
    """Generate a single LTX-2.3 video prompt using the context-aware composition engine.

    Produces a single flowing paragraph in present tense, 150-250 words, with
    explicit camera movement, physical/material textures, ambient audio descriptions,
    and NO emotional labels (only physical cues).

    Args:
        narration_text: the narration being spoken during this clip
        scene_title: the scene title for context
        scene_context: broader scene narrative context
        target_duration_sec: how long the video clip should be
        clip_index: position of this clip within the scene (0-based)
        total_clips_in_scene: total clips in this scene
        previous_shot_type: shot type used in previous clip (to vary)
        voice: narration voice (V1/V2/V3) for visual treatment
        continuity_state: SceneContinuityState for tracking across clips

    Returns:
        dict with 'prompt', 'shot_type', 'environment', 'camera_movement',
        'word_count', 'generation_params'
    """
    min_sentences, max_sentences, min_words, max_words = _scale_prompt_complexity(target_duration_sec)

    # Determine dramatic position
    if continuity_state:
        dramatic_position = continuity_state.get_dramatic_position(clip_index)
        prev_shot = continuity_state.previous_shot_type or previous_shot_type
        variant_idx = continuity_state.character_variant_index
    else:
        total = total_clips_in_scene
        ratio = clip_index / max(1, total - 1) if total > 1 else 0
        if ratio < 0.15:
            dramatic_position = "opening"
        elif ratio < 0.40:
            dramatic_position = "rising"
        elif ratio < 0.65:
            dramatic_position = "climax"
        elif ratio < 0.85:
            dramatic_position = "falling"
        else:
            dramatic_position = "closing"
        prev_shot = previous_shot_type
        variant_idx = clip_index

    # Environment selection — use continuity state if established, else pick fresh
    if continuity_state and continuity_state.environment_key:
        env_key = continuity_state.environment_key
    else:
        env_key = _pick_environment(narration_text, scene_title, voice)
        if continuity_state:
            continuity_state.environment_key = env_key
            continuity_state.palette = ENVIRONMENTS[env_key]["palette"]

    env = ENVIRONMENTS[env_key]
    metaphor = _pick_visual_metaphor(narration_text)

    # Layer 1: Shot establishment — vary across clips for visual rhythm
    # Dramatic-position-aware shot selection
    available_shots = [s for s in SHOT_TYPES if s != prev_shot]

    if dramatic_position == "opening":
        shot = random.choice([s for s in available_shots if "wide" in s.lower() or "establishing" in s.lower() or "aerial" in s.lower()] or available_shots)
    elif dramatic_position == "climax":
        shot = random.choice([s for s in available_shots if "close" in s.lower() or "handheld" in s.lower() or "dutch" in s.lower()] or available_shots)
    elif dramatic_position == "closing":
        shot = random.choice([s for s in available_shots if "wide" in s.lower() or "pull" in s.lower() or "static" in s.lower() or "silhouette" in s.lower()] or available_shots)
    else:
        shot = random.choice(available_shots)

    # Layer 2: Scene/environment — with palette consistency
    env_sentence = f"{env['lighting']}, {env['textures']}."

    # Layer 3: Action — derive from narration, use visual metaphor if available
    if metaphor:
        concepts = _extract_key_concepts(narration_text)
        concept_str = " and ".join(concepts[:2]) if len(concepts) > 1 else concepts[0]
        action_sentence = (
            f"{metaphor.rstrip('.')}. "
            f"The scene conveys the weight of {concept_str}"
        )
    else:
        action_sentence = _narration_to_visual_action(narration_text, scene_context, env_key, dramatic_position)

    # Layer 4: Character/subject — physical description with physical cues (no emotions)
    character_sentence = _generate_character_layer(narration_text, env_key, variant_idx, dramatic_position)

    # Layer 5: Camera movement — dramatic-position-aware selection
    if dramatic_position == "opening":
        cam_candidates = [c for c in CAMERA_MOVEMENTS if "pull" in c.lower() or "descend" in c.lower() or "push" in c.lower()]
    elif dramatic_position == "climax":
        cam_candidates = [c for c in CAMERA_MOVEMENTS if "ease" in c.lower() or "rack" in c.lower() or "follow" in c.lower()]
    elif dramatic_position == "closing":
        cam_candidates = [c for c in CAMERA_MOVEMENTS if "pull back" in c.lower() or "crane" in c.lower() or "static" in c.lower() or "tilt" in c.lower()]
    else:
        cam_candidates = CAMERA_MOVEMENTS

    cam_move = random.choice(cam_candidates or CAMERA_MOVEMENTS)

    # Layer 6: Audio description
    audio_sentence = f"The ambient sound is {env['atmosphere']}."

    # Voice-specific detail layer
    voice_detail = _get_voice_detail(voice, env_key) if voice else ""

    # Assemble into single flowing paragraph
    parts = [
        f"{shot} of {_scene_opener(narration_text, env_key)}.",
        env_sentence,
        action_sentence + ".",
        character_sentence,
        cam_move + ".",
        audio_sentence,
    ]

    # Add voice-specific detail for mid-length and longer clips
    if voice_detail and target_duration_sec > 4:
        parts.append(voice_detail + ".")

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

    # Verify word count matches duration — add detail for longer clips
    word_count = len(prompt.split())
    if word_count < min_words and target_duration_sec > 5:
        extra = _add_detail_for_duration(narration_text, env_key, target_duration_sec, dramatic_position)
        prompt = prompt.rstrip(".") + ". " + extra

    # Trim if significantly over max for short clips
    word_count = len(prompt.split())
    if word_count > max_words and target_duration_sec <= 3:
        words = prompt.split()
        # Find a sentence boundary near the target
        target_end = max_words
        while target_end < len(words) and not words[target_end - 1].endswith("."):
            target_end += 1
        if target_end <= len(words):
            prompt = " ".join(words[:target_end])

    word_count = len(prompt.split())

    # Update continuity state
    if continuity_state:
        continuity_state.advance(shot, character_sentence)

    return {
        "prompt": prompt,
        "shot_type": shot,
        "environment": env_key,
        "camera_movement": cam_move,
        "duration_sec": target_duration_sec,
        "word_count": word_count,
        "dramatic_position": dramatic_position,
        "generation_params": {
            "min_sentences": min_sentences,
            "max_sentences": max_sentences,
            "target_word_range": [min_words, max_words],
        },
    }


def generate_prompts_for_scene(scene_num, scene_title, audio_segments,
                               narration_texts, scene_context=""):
    """Generate video prompts for all clips in a scene based on audio timing.

    Uses scene continuity tracking to ensure consistent characters, environments,
    and varied shot types across sequential clips.

    Args:
        scene_num: int
        scene_title: str
        audio_segments: list of dicts from otio_timeline.get_scene_audio_segments()
        narration_texts: list of (voice, text) tuples
        scene_context: broader context about the scene

    Returns:
        list of prompt dicts ready for video generation
    """
    prompts = []
    total_clips = len(audio_segments)
    continuity = SceneContinuityState(scene_num, total_clips)

    for i, seg in enumerate(audio_segments):
        dur = seg["duration_sec"]

        # Get narration text and voice for this segment
        if i < len(narration_texts):
            if isinstance(narration_texts[i], tuple):
                voice, narr_text = narration_texts[i]
            else:
                voice = seg.get("voice", "V1")
                narr_text = narration_texts[i]
        else:
            voice = seg.get("voice", "V1")
            narr_text = seg.get("text_preview", "")

        # Calculate generation parameters
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
            continuity_state=continuity,
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
            "dramatic_position": result["dramatic_position"],
            "generation_params": result["generation_params"],
            "narration_text": narr_text[:200],
            "voice": voice,
            "audio_start_sec": seg.get("start_sec", 0),
            "audio_end_sec": seg.get("end_sec", 0),
        })

    return prompts


def generate_all_prompts(otio_timeline, narration_data):
    """Generate video prompts for all scenes using OTIO audio timing.

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

        narr_texts = narr_lookup.get(scene_num, [])

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

    parser = argparse.ArgumentParser(description="LTX-2.3 prompt generator — context-aware composition engine")
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
