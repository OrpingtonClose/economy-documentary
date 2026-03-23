#!/usr/bin/env python3
"""
LTX-2.3 Prompt Generator v9 — Context-Aware Cinematic Composition Engine
==========================================================================
Generates cinema-quality prompts for LTX-2.3 using a sophisticated rule-based
composition engine that understands narration context, scene continuity, and
LTX-2.3's specific requirements.

NO external LLM API required — runs entirely on VMs without internet access.
The quality improvement comes from better template composition, scene continuity
tracking, narration-context awareness, and duration-calibrated complexity.

Composition Layers:
  1. Shot establishment — cinematic framing varied across clips for visual rhythm
  2. Scene/environment — lighting, color, textures, atmosphere
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
    # --- v9 new environments ---
    "refugee_camp": {
        "lighting": "Flat midday sun casting sharp shadows under canvas and corrugated roofing",
        "textures": "frayed tarpaulin, stacked plastic crates, dusty gravel paths worn smooth by foot traffic",
        "atmosphere": "a low murmur of distant voices, children calling, the flap of loose tent fabric in dry wind",
        "palette": "UN blue, dust beige, faded orange tarpaulin, bleached white canvas",
    },
    "naval_vessel": {
        "lighting": "Overcast maritime light filtered through salt spray, steel surfaces gleaming dull silver",
        "textures": "riveted hull plating, non-skid deck coating, heavy watertight hatches with spinning wheel locks",
        "atmosphere": "the deep throb of engines below deck, the hiss of bow wake, the snap of signal flags in wind",
        "palette": "battleship gray, deep ocean blue, safety yellow, rust-streaked white",
    },
    "hospital": {
        "lighting": "Harsh fluorescent ceiling panels casting flat, shadowless clinical light",
        "textures": "polished linoleum floors, stainless steel instrument trays, starched cotton curtain dividers",
        "atmosphere": "the rhythmic beep of monitors, the squeak of rubber soles on tile, a distant intercom page",
        "palette": "clinical white, scrub green, monitor-glow blue, latex-glove purple",
    },
    "parliament": {
        "lighting": "Grand chandeliers casting warm amber light across tiered seating and carved stone",
        "textures": "green leather benches polished by decades of use, heavy oak dispatch boxes, gilt trim on dark wood",
        "atmosphere": "the echo of voices in a high-ceilinged chamber, papers rustling, the thud of a gavel",
        "palette": "parliamentary green, dark oak brown, gilt gold, parchment cream",
    },
    "protest_march": {
        "lighting": "Harsh afternoon sun glinting off glass storefronts and the raised surfaces of hand-painted signs",
        "textures": "rough cardboard placards, wrinkled fabric banners, scuffed asphalt underfoot",
        "atmosphere": "a rising and falling chant carried on the wind, the shuffle of thousands of feet, distant megaphone distortion",
        "palette": "crowd-cloth mixed colors, asphalt gray, banner red, spray-paint black",
    },
    "underground_bunker": {
        "lighting": "Low-wattage incandescent bulbs in wire cages casting warm pools of light against raw concrete",
        "textures": "rough poured-concrete walls, exposed conduit pipes, metal shelving heavy with supply crates",
        "atmosphere": "a deep subterranean hum of ventilation, the drip of condensation, muffled silence",
        "palette": "concrete gray, military olive, incandescent amber, shadow black",
    },
    "satellite_view": {
        "lighting": "The cold blue of reflected earthlight against the darkness of orbital space",
        "textures": "cloud formations swirling in visible atmospheric layers, the curvature of coastlines, glowing city grids at night",
        "atmosphere": "a profound silence broken only by the faintest hum of station systems, the void pressing against every surface",
        "palette": "deep space black, atmospheric blue, cloud white, city-light amber",
    },
    "grain_field": {
        "lighting": "Golden hour light raking across endless rows of wheat, long shadows stretching east",
        "textures": "dry wheat stalks rustling against each other, cracked soil visible between rows, the dust of harvest machinery",
        "atmosphere": "the whisper of wind through standing grain, the distant rumble of a combine harvester, insect hum",
        "palette": "wheat gold, harvest amber, soil brown, sky-wash blue",
    },
    "gas_station": {
        "lighting": "Harsh overhead canopy fluorescents casting a flat white pool against dark surroundings",
        "textures": "cracked concrete islands, weathered pump housings, the rainbow sheen of spilled fuel on wet pavement",
        "atmosphere": "the mechanical clunk of a pump nozzle, the hiss of fuel flowing, distant highway drone",
        "palette": "canopy white, pump-display green, petroleum rainbow, nighttime black",
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
    # --- v9 new concept visuals ---
    "blockade": ["a heavy chain stretched taut across a harbor entrance, links rusted and slick with sea spray",
                 "a barrier of concrete blocks arranged across a road, razor wire coiled along the top",
                 "a cargo ship sitting motionless at anchor, its deck stacked with containers going nowhere"],
    "refugee": ["a single pair of sandals left at the edge of a dusty road, tread worn paper-thin",
                "a crowded tent settlement stretching to the horizon under a flat white sky",
                "hands gripping the mesh of a chain-link fence, knuckles white with pressure"],
    "missile": ["a contrail drawing a white line across a clear blue sky, accelerating toward the vanishing point",
                "a radar screen with a single blip moving steadily across concentric green circles",
                "a silo hatch grinding open to reveal darkness and the curved nose of a warhead"],
    "sanction": ["a rubber stamp pressing down hard on a document marked with red ink",
                 "a shipping manifest with entire sections struck through in heavy black marker",
                 "bank vault doors swinging shut, the locking mechanism turning with mechanical finality"],
    "alliance": ["two hands reaching across a polished conference table to clasp in a firm grip",
                 "overlapping flag shadows cast side by side on a marble floor",
                 "a row of chairs filling one by one as delegates take their seats at a long table"],
    "ceasefire": ["a radio handset placed down gently on a desk, its cord still swaying",
                  "a soldier lowering a weapon slowly until the barrel points at the ground",
                  "a clock on a bunker wall, its second hand sweeping through silence"],
    "escalation": ["a staircase spiraling upward into shadow, each step narrower than the last",
                   "a pressure gauge needle creeping steadily toward the red zone",
                   "flames spreading from a single point across a paper map, edges curling black"],
    "humanitarian": ["a convoy of white trucks moving single-file along a dirt road toward rising dust",
                     "medical supplies being unloaded by many hands from the back of a cargo plane",
                     "a Red Cross flag snapping in the wind above a field hospital tent"],
    "propaganda": ["a printing press rolling sheets of identical posters onto a growing stack",
                   "a television screen showing a broadcast that repeats on every monitor in a shop window",
                   "a loudspeaker mounted on a pole, its cone aimed down at an empty square"],
    "intelligence": ["a wall covered in pinned photographs connected by red string forming a web",
                     "a pair of headphones resting beside a reel-to-reel tape recorder, spools turning slowly",
                     "a satellite dish pivoting silently against a night sky full of stars"],
}

# ------------------------------------------------------------------
# Environment keyword scoring — maps narration keywords to environments
# ------------------------------------------------------------------
ENVIRONMENT_SCORING = {
    "trading_floor": ["trade", "stock", "market", "dow", "nasdaq", "exchange", "ticker", "portfolio", "index"],
    "war_zone": ["bomb", "missile", "attack", "destruction", "rubble", "casualt", "soldier", "combat", "strike", "shelling"],
    "government_office": ["policy", "regulation", "senator", "congress", "legislation", "bureaucr", "official"],
    "street_market": ["price", "grocery", "consumer", "inflation", "cost of living", "food", "bread"],
    "oil_refinery": ["oil", "petroleum", "barrel", "crude", "refinery", "pipeline", "opec", "fuel"],
    "kitchen_domestic": ["family", "kitchen", "home", "breakfast", "morning", "coffee", "everyday"],
    "data_center": ["data", "algorithm", "crypto", "bitcoin", "blockchain", "digital", "server", "cyber"],
    "shipping_port": ["shipping", "container", "cargo", "supply chain", "port", "export", "import"],
    "financial_district": ["bank", "wall street", "finance", "investment", "billion", "hedge fund", "capital"],
    "abstract_metaphor": ["concept", "abstract", "metaphor", "idea", "fundamental", "system"],
    "military_hardware": ["weapon", "defense", "military", "arms", "tank", "aircraft", "ammunition", "contract"],
    "newsroom": ["report", "breaking", "headline", "anchor", "broadcast", "coverage", "media"],
    "refugee_camp": ["refugee", "displaced", "camp", "tent", "flee", "asylum", "migration", "humanitarian crisis"],
    "naval_vessel": ["navy", "warship", "destroyer", "carrier", "fleet", "strait", "blockade", "maritime"],
    "hospital": ["hospital", "wounded", "medical", "doctor", "triage", "casualty", "clinic", "patients"],
    "parliament": ["parliament", "debate", "session", "vote", "resolution", "assembly", "chamber", "legislative"],
    "protest_march": ["protest", "march", "demonstration", "rally", "crowd", "uprising", "dissent", "unrest"],
    "underground_bunker": ["bunker", "shelter", "underground", "command center", "fortified", "air raid", "sirens"],
    "satellite_view": ["satellite", "orbit", "aerial", "geopolitical", "global", "overview", "borders", "territory"],
    "grain_field": ["grain", "wheat", "harvest", "agriculture", "crop", "famine", "food supply", "farming"],
    "gas_station": ["gas", "gasoline", "fuel price", "pump", "rationing", "petrol", "filling station"],
}

# ------------------------------------------------------------------
# Voice-specific visual treatment
# ------------------------------------------------------------------
VOICE_VISUAL_TREATMENT = {
    "V1": {
        "style": "data-driven",
        "preference": ["trading_floor", "data_center", "financial_district"],
        "detail_focus": "screens, charts, data displays, numerical readouts, technical instruments",
    },
    "V2": {
        "style": "analytical",
        "preference": ["government_office", "parliament", "newsroom"],
        "detail_focus": "maps, documents, policy papers, strategic diagrams, diplomatic settings",
    },
    "V3": {
        "style": "historical",
        "preference": ["war_zone", "street_market", "kitchen_domestic"],
        "detail_focus": "archival textures, worn surfaces, weathered objects, historical artifacts",
    },
}

# ------------------------------------------------------------------
# Scene opener phrases keyed by environment
# ------------------------------------------------------------------
SCENE_OPENERS = {
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
    "refugee_camp": "a sprawling encampment of canvas tents and corrugated shelters stretching across dry earth",
    "naval_vessel": "the steel deck of a warship cutting through gray swells, spray misting across the bow",
    "hospital": "a crowded hospital corridor where gurneys line the walls under buzzing fluorescent tubes",
    "parliament": "a grand legislative chamber where tiered benches face an ornate speaker's podium",
    "protest_march": "a surging crowd filling a wide avenue, hand-painted signs bobbing above a sea of heads",
    "underground_bunker": "a low-ceilinged concrete shelter where bare bulbs cast harsh circles of light",
    "satellite_view": "the curved horizon of the earth seen from orbit, cloud systems swirling over darkened landmass",
    "grain_field": "an endless expanse of golden wheat rippling in waves under a wide-open sky",
    "gas_station": "a solitary filling station under harsh canopy lights, pumps standing like sentinels in the dark",
}

# ------------------------------------------------------------------
# Character templates — multiple per environment for variety
# ------------------------------------------------------------------
CHARACTER_TEMPLATES = {
    "trading_floor": [
        "A trader in a rumpled white dress shirt, sleeves rolled to the elbows, presses his palm against his forehead and exhales slowly through pursed lips, tie hanging loose, collar unbuttoned, dark circles visible under the monitor glow.",
        "A woman at a desk leans forward with both hands flat on the surface, her blazer pushed back at the shoulders, eyes scanning rapidly between three screens, jaw clenched tight.",
        "A floor manager stands with a phone pressed to each ear, feet planted wide apart, the cords stretched taut, sweat visible on his brow in the cool fluorescent light.",
    ],
    "war_zone": [
        "A figure in a dust-covered jacket stands with shoulders hunched, one hand gripping a crumbling doorframe for balance, grime lining the creases of their face, gaze fixed on a point in the middle distance.",
        "A medic crouches beside a stretcher, hands moving with swift precision, the cuffs of their uniform dark with stains, a headlamp throwing a beam across rubble.",
        "An elderly person sits motionless on a concrete block, hands folded in their lap, dust settled in the folds of their clothing, watching the horizon with an unblinking stillness.",
    ],
    "government_office": [
        "A silver-haired official in a dark suit adjusts a stack of folders with deliberate, measured movements, reading glasses perched at the tip of their nose, a fountain pen held motionless between two fingers.",
        "An aide stands at the edge of the frame, arms full of dossiers, one foot angled toward the door, waiting for a signal from the figure behind the desk.",
        "A diplomat leans back in a leather chair, fingertips pressed together in a steeple, the reflection of the window casting a grid of light across their face.",
    ],
    "street_market": [
        "A vendor with weathered hands and a faded apron leans forward to rearrange a display, fingers moving with practiced efficiency, deep lines framing their eyes as they squint against the morning sun.",
        "A shopper pauses mid-stride with a cloth bag over one shoulder, reaching toward a price card and pulling their hand back slowly, the weight of the bag shifting on their frame.",
        "An old man sits on an upturned crate, wrapping produce in newspaper with gnarled fingers, a scale balanced on the ledge beside him.",
    ],
    "oil_refinery": [
        "A worker in a hard hat and high-visibility vest walks along a catwalk, one gloved hand trailing along the railing, boots leaving prints on the metal grating.",
        "An engineer holds a clipboard against their chest, visor raised, staring upward at a maze of pipes, the orange glow of sodium lamps reflecting off their safety goggles.",
    ],
    "kitchen_domestic": [
        "A middle-aged person in a worn cardigan pauses mid-motion, coffee mug suspended halfway to their lips, eyes fixed on a newspaper headline, their other hand resting flat on the table, fingers spread wide.",
        "A teenager stands at the counter pouring cereal, one hand braced against the cabinet, their gaze drawn to a phone screen propped against the toaster, spoon forgotten mid-air.",
    ],
    "data_center": [
        "A technician in a dark polo shirt peers at a rack display, the screen's light casting sharp shadows across their focused expression, fingers hovering over a keyboard without pressing a key.",
        "A security guard walks the aisle between server racks, flashlight sweeping in slow arcs, the beam catching on dust motes drifting in recycled air.",
    ],
    "shipping_port": [
        "A dockworker in a heavy coat and safety vest signals with broad arm gestures, the wind catching the loose fabric of their clothing, salt-weathered skin and squinting eyes against the harbor glare.",
        "A crane operator sits in a glass cab high above the dock, hands steady on twin controls, their silhouette framed against an overcast sky.",
    ],
    "financial_district": [
        "Suited pedestrians move through the frame, their pace quickening, briefcases gripped tighter, phone screens illuminating downcast faces, one figure standing still against the flow, staring upward at a ticker display.",
        "A woman in a long coat pushes through a revolving door, one hand pressing a phone to her ear, the other clutching a portfolio, the glass panels spinning behind her.",
    ],
    "military_hardware": [
        "A uniformed figure stands at attention beside a vehicle, arms clasped behind their back, chin slightly raised, the fabric of their uniform pressed and rigid, catching hard shadows.",
        "A mechanic lies on a creeper beneath an armored vehicle, only their boots visible, a wrench clanging against metal in a rhythmic pulse.",
    ],
    "newsroom": [
        "An anchor sits straight-backed behind the desk, papers squared precisely, maintaining composed stillness as studio lights cast a bright wash, eyes tracking a teleprompter with controlled precision.",
        "A producer leans over a mixing board, one hand pressing a headphone to their ear, the other jabbing at a row of illuminated switches, monitors reflected in their glasses.",
    ],
    "refugee_camp": [
        "A woman wrapped in a heavy shawl sits on the ground outside a tent, one hand resting on a sleeping child's back, her other hand wrapped around a tin cup, eyes staring past the camera.",
        "A young man carries two plastic water jugs by their handles, arms extended, shoulders hunched under the weight, dust rising from each footstep on the packed earth.",
    ],
    "naval_vessel": [
        "A sailor in a blue duty uniform grips a railing with both hands, salt spray misting across their face, eyes fixed on a point beyond the bow, feet braced wide on the rolling deck.",
        "An officer stands at the bridge window, binoculars raised to their eyes, the ship's wheel visible behind them, jacket buttoned tight against the maritime cold.",
    ],
    "hospital": [
        "A surgeon in scrubs pulls a mask down below their chin, sweat visible on their forehead, hands held up and apart at chest height, still gloved, eyes closed for a single long breath.",
        "A nurse moves between beds with quick, efficient steps, hands adjusting an IV line, the squeak of their shoes on linoleum marking each stride.",
    ],
    "parliament": [
        "A legislator rises from a green leather bench, one hand gripping a sheaf of papers, the other resting on the bench back, chin lifted as they prepare to address the chamber.",
        "A clerk sits at a desk below the speaker's podium, pen moving steadily across a ledger, oblivious to the raised voices echoing off stone walls above.",
    ],
    "protest_march": [
        "A young woman at the front of the march holds a placard above her head with both hands, arms fully extended, her face tilted upward against the sun, mouth open mid-chant.",
        "A man in a heavy jacket links arms with those beside him, feet planted on asphalt, the muscles in his neck taut as the crowd surges forward around him.",
    ],
    "underground_bunker": [
        "A communications officer hunches over a desk cluttered with radio equipment, one hand pressing a headset to their ear, the other scribbling on a notepad, a single bare bulb illuminating their work.",
        "A civilian sits on a narrow cot against the concrete wall, arms wrapped around their knees, the orange glow of a space heater casting long shadows across the floor.",
    ],
    "satellite_view": [
        "No human figure is visible at this altitude, only the patterns of civilization etched into the landscape, roads threading between settlements, harbors filled with the specks of waiting ships.",
    ],
    "grain_field": [
        "A farmer stands knee-deep in wheat, one hand shading their eyes against the low sun, the other holding a handful of grain stalks, their silhouette sharp against the golden horizon.",
        "A child runs along a narrow path between rows, arms outstretched, fingertips brushing the tops of the wheat, dust rising in a thin trail behind them.",
    ],
    "gas_station": [
        "A driver leans against the side of a car with arms folded, watching the pump numbers climb, the fluorescent canopy light flattening every shadow, jaw tight, foot tapping the concrete.",
        "An attendant in a stained uniform scrubs the windshield of a vehicle, one hand braced on the hood, the squeegee leaving clean arcs across dusty glass.",
    ],
}

# ------------------------------------------------------------------
# Extra environment detail for longer clips
# ------------------------------------------------------------------
ENVIRONMENT_DETAILS = {
    "trading_floor": "Papers shift in the air-conditioning draft, a phone rings unanswered on a far desk, the timestamp on a corner monitor ticks forward, reflections of chart patterns ripple across a glass partition.",
    "war_zone": "A curtain flutters through a broken window, dust motes drift slowly through a shaft of light, a cracked mirror on a wall reflects fragmented sky, water drips steadily from exposed pipes into a growing puddle.",
    "government_office": "A grandfather clock in the corner marks time with a low pendulum swing, light catches the edge of a crystal inkwell, a leather blotter bears the impression of a hundred signatures.",
    "street_market": "Flies circle a basket of overripe fruit, a torn awning flaps in a gust, a child's hand reaches up to a counter edge, coins slide across a worn wooden surface.",
    "oil_refinery": "A plume of steam vents from a pressure relief valve, puddles of oily water shimmer with iridescent color, a warning light rotates slowly on a distant catwalk.",
    "kitchen_domestic": "Steam curls upward from the cup catching golden light, a clock on the wall ticks audibly, crumbs scatter across a breadboard, the refrigerator hum provides a bass note to the silence.",
    "data_center": "A status LED shifts from green to amber on a single server blade, a maintenance cart sits abandoned in the aisle, cable bundles sway gently from overhead trays in the airflow.",
    "shipping_port": "A rope as thick as an arm creaks against a bollard, container numbers blur as a crane swings its load, oil-sheened water laps against barnacle-crusted pilings.",
    "financial_district": "Pigeons scatter from a ledge as a taxi horn sounds below, a newspaper page tumbles along the gutter, the digital clock on a bank facade flickers between time and temperature.",
    "abstract_metaphor": "Particles drift through intersecting beams of light, a surface ripples as if struck by an unseen force, shadows lengthen and contract in a rhythmic, breathing pattern.",
    "military_hardware": "A tarpaulin snaps in the wind over a supply pallet, boot prints track through a mud patch between vehicles, a radio aerial sways against a pale sky.",
    "newsroom": "A coffee mug sits forgotten beside a keyboard, its surface reflecting the studio lights, a stack of printouts curls at the edges under the heat of the broadcast lamps.",
    "refugee_camp": "A child's drawing is pinned to the inside of a tent wall, a queue of people stretches from a water point into the distance, laundry strung between poles flutters in dry wind.",
    "naval_vessel": "Binoculars hang from a hook on the bridge wall, swaying with the ship's motion, a signal lamp blinks in staccato bursts from a distant escort vessel.",
    "hospital": "An IV bag drips with metronomic regularity, a clipboard hangs from the foot of a bed, the rubber wheels of a gurney leave faint tracks on freshly mopped tile.",
    "parliament": "Microphones stand at attention on every desk, their cables trailing to the floor, a gallery of portraits lines the upper walls, gilt frames catching the chandelier light.",
    "protest_march": "Discarded flyers litter the asphalt in the wake of the crowd, a megaphone crackles with feedback, the shadow of the march stretches long down a cross street.",
    "underground_bunker": "A map pinned to the wall is covered in colored pins and string, condensation beads on a steel pipe, a stack of ration boxes fills one corner floor to ceiling.",
    "satellite_view": "Cloud formations cast vast shadows on the landscape below, city lights define the coastline in amber points, shipping lanes appear as faint wakes on the dark ocean surface.",
    "grain_field": "A harvester trail cuts a geometric line through the standing crop, dust hangs in the air behind machinery like a golden curtain, birds rise in a flock from a disturbed section.",
    "gas_station": "A price board with removable digits shows numbers recently rearranged, a crushed soda can rolls slowly across the forecourt in a breeze, moths orbit the canopy light in tight spirals.",
}


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
        self.current_environment = None
        self.current_palette = None
        self.character_descriptions_used = []
        self.clip_index = 0

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

    def advance(self, shot_type, camera_movement, environment, character_desc):
        """Record what was used for this clip and advance."""
        self.previous_shot_type = shot_type
        self.previous_camera_movement = camera_movement
        self.current_environment = environment
        if character_desc:
            self.character_descriptions_used.append(character_desc)
        self.clip_index += 1


# ===================================================================
# Core composition engine functions
# ===================================================================

def _pick_environment(narration_text, scene_title, voice=None):
    """
    Select the most fitting environment based on narration content.
    Voice preference is used as a tiebreaker.
    """
    text = (narration_text + " " + scene_title).lower()

    best_env = "abstract_metaphor"
    best_score = 0

    for env_key, keywords in ENVIRONMENT_SCORING.items():
        score = sum(1 for kw in keywords if kw in text)
        # Boost environments preferred by this voice
        if voice and voice in VOICE_VISUAL_TREATMENT:
            if env_key in VOICE_VISUAL_TREATMENT[voice]["preference"]:
                score += 0.5
        if score > best_score:
            best_score = score
            best_env = env_key

    return best_env


def _pick_visual_metaphor(narration_text):
    """Select a visual metaphor based on narration content, preferring specificity."""
    text = narration_text.lower()

    matches = []
    for concept, visuals in CONCEPT_VISUALS.items():
        if concept in text:
            matches.append((concept, visuals))

    if not matches:
        return None

    # Prefer longer concept matches (more specific)
    matches.sort(key=lambda x: len(x[0]), reverse=True)
    concept, visuals = matches[0]
    return random.choice(visuals)


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


def _extract_key_concept(text):
    """Extract the core concept from narration text."""
    text_lower = text.lower()
    for concept in CONCEPT_VISUALS:
        if concept in text_lower:
            return concept
    words = [w for w in text.split()[:8] if len(w) > 3]
    return " ".join(words[:4]) if words else "the unfolding situation"


def _narration_to_visual_action(narration_text, scene_context, env_key, voice=None):
    """
    Convert narration text into a visual action description.
    Produces cinematic B-roll descriptions rather than literal illustrations.
    Uses environment context and voice treatment for more specific output.
    """
    text = narration_text.strip().lower()

    # Concept-specific visual actions (richer than v8 static replacements)
    concept_actions = {
        "billion": "Rows of figures scroll across a backlit display, each line replacing the one before, the scale of the numbers suggested by the endless cascade of digits filling every screen in the frame",
        "percent": "A graph line arcs sharply across a display, the curve bending like a drawn bow, and the light from the screen plays across nearby surfaces as the direction shifts",
        "inflation": "Price cards on a market stall flip one by one from lower to higher values, each new card placed by a hand that moves with tired familiarity, the old cards scattered on the ground below",
        "interest rate": "A dial on an institutional wall rotates clockwise with a mechanical click at each increment, the needle passing through marked zones from green to amber to red",
        "sanctions": "A red stamp descends onto a shipping manifest, the imprint spreading ink across the paper surface, as a stack of identical documents waits in a tray beside it",
        "supply chain": "Containers move along a vast conveyor system that stretches from foreground to vanishing point, each box stamped with routing codes, the machinery humming with continuous motion",
        "deficit": "A balance scale in an empty room tips sharply to one side, the lower pan descending slowly, its weight pulling the frame composition off-center",
        "gdp": "A bar chart fills the wall of a briefing room, each bar growing or shrinking as a hand adjusts figures on a chalkboard beside it, erasure marks ghosting behind the current numbers",
        "unemployment": "A row of empty workstations stretches across a factory floor, each chair pushed back from its desk, overhead lights still on, casting bright pools onto vacant surfaces",
        "debt": "Stacked folders rise in a tall column on a wooden desk, each one thicker than the last, the topmost teetering at the edge, casting a long shadow across scattered receipts",
        "export": "Crates branded with destination markings slide down a loading ramp into the hold of a cargo aircraft, dock workers guiding each one with practiced hand signals",
        "import": "Packages accumulate on a customs inspection table, each one opened and resealed with tape, a pair of gloved hands methodically cataloging contents onto a clipboard",
        "treaty": "A fountain pen moves across a thick document on a polished table, the nib leaving a trail of wet ink, two pairs of hands visible at the edges of the frame",
        "nuclear": "A control room panel displays rows of indicator lights, each one a steady green except for one that shifts to amber, a technician's hand hovering over the switch below it",
        "election": "Paper ballots cascade into a transparent box, each one tumbling slowly, the pile rising unevenly, hands reaching in from outside the frame to deposit more",
    }

    for phrase, action_desc in concept_actions.items():
        if phrase in text:
            return action_desc

    # Voice-specific fallback actions
    if voice == "V1":
        return (
            "Data streams across multiple displays in the frame, each screen showing a different facet "
            "of the same underlying pattern, the glow of the readouts casting shifting light across "
            "the surrounding surfaces as values update in real time"
        )
    elif voice == "V2":
        return (
            "A document lies open on a polished surface, its margins filled with annotations in different "
            "inks, a magnifying glass resting beside it catches the overhead light, and the visible text "
            "suggests the weight and complexity of what is being decided"
        )
    elif voice == "V3":
        return (
            "The scene unfolds with the measured patience of a historical record, worn surfaces bearing "
            "the marks of time and use, each object in the frame positioned as if it has been there for "
            "decades, the light falling across textures that tell their own story of age and endurance"
        )

    # Generic fallback
    return (
        "The scene unfolds with deliberate pacing, each element in the frame reinforcing the gravity "
        "of the narration, visual details accumulate gradually, building a layered documentary "
        "composition where light, texture, and spatial depth carry the weight of meaning"
    )


def _select_character(env_key, used_descriptions):
    """
    Select a character description for the environment, avoiding recently used ones.
    Returns a character description string.
    """
    templates = CHARACTER_TEMPLATES.get(env_key)
    if not templates:
        return (
            "A solitary figure occupies the frame, their posture conveying the weight "
            "of the moment through subtle physical tension visible in their hands and shoulders."
        )

    # Filter out recently used
    available = [t for t in templates if t not in used_descriptions]
    if not available:
        available = templates  # Reset if all used

    return random.choice(available)


def _select_shot_type(dramatic_position, previous_shot_type):
    """Select shot type based on dramatic position, avoiding repetition."""
    preferred = SHOT_PROGRESSION.get(dramatic_position, SHOT_TYPES)
    # Exclude previous
    available = [s for s in preferred if s != previous_shot_type]
    if not available:
        available = [s for s in SHOT_TYPES if s != previous_shot_type]
    return random.choice(available)


def _select_camera_movement(previous_movement):
    """Select camera movement, avoiding repetition."""
    available = [m for m in CAMERA_MOVEMENTS if m != previous_movement]
    return random.choice(available)


def _compose_style_signature(env_key):
    """Generate the photorealistic style signature for the prompt."""
    env = ENVIRONMENTS.get(env_key, ENVIRONMENTS["abstract_metaphor"])
    return (
        "Photorealistic cinematic documentary footage, shot on Arri Alexa with Cooke anamorphic lenses, "
        f"natural film grain, shallow depth of field, {env['palette']} color palette, "
        "documentary-style composition with deliberate negative space."
    )


# ===================================================================
# Main prompt generation function — single clip
# ===================================================================

def generate_prompt(narration_text, scene_title, scene_context,
                    target_duration_sec, clip_index, total_clips_in_scene,
                    previous_shot_type=None, voice=None, scene_state=None):
    """
    Generate a single LTX-2.3 video prompt using the context-aware composition engine.

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
        voice: narration voice (V1/V2/V3) for visual treatment
        scene_state: SceneVisualState for continuity tracking

    Returns:
        dict with 'prompt', 'shot_type', 'environment', 'camera_movement',
        'word_count', 'generation_params'
    """
    min_sent, max_sent, min_words, max_words = _scale_prompt_length(target_duration_sec)

    # Determine dramatic position
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

    # Environment selection — consistent within scene if state available
    if scene_state and scene_state.current_environment:
        env_key = scene_state.current_environment
    else:
        env_key = _pick_environment(narration_text, scene_title, voice)

    env = ENVIRONMENTS[env_key]

    # Layer 1: Shot type — driven by dramatic arc
    shot = _select_shot_type(dramatic_pos, prev_shot)

    # Layer 2: Scene/environment with textures
    opener = SCENE_OPENERS.get(env_key, "a carefully composed documentary scene")
    env_sentence = f"{env['lighting']}, {env['textures']}."

    # Layer 3: Action — narration-derived visual metaphor or B-roll
    metaphor = _pick_visual_metaphor(narration_text)
    if metaphor:
        action_sentence = (
            f"{metaphor.rstrip('.')}. "
            f"The scene conveys the weight of {_extract_key_concept(narration_text)}"
        )
    else:
        action_sentence = _narration_to_visual_action(narration_text, scene_context, env_key, voice)

    # Layer 4: Character — physical cues, no emotions, varied across scene
    character_sentence = _select_character(env_key, used_chars)

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
    parts.append(_compose_style_signature(env_key))

    # Build prompt
    prompt = " ".join(p.strip() for p in parts if p.strip())

    # Clean up double periods, extra spaces
    prompt = prompt.replace("..", ".").replace(". .", ".").replace("  ", " ")

    # Pad for longer clips if under word target
    word_count = len(prompt.split())
    if word_count < min_words and target_duration_sec > 5:
        extra = ENVIRONMENT_DETAILS.get(env_key, (
            "Small details emerge in the periphery, textures reveal themselves under scrutiny, "
            "ambient elements shift subtly, and the frame breathes with quiet, documentary patience."
        ))
        prompt = prompt.rstrip(".") + ". " + extra
        word_count = len(prompt.split())

    # For very long clips, add even more detail
    if word_count < min_words and target_duration_sec > 10:
        # Add a second metaphor or voice-specific detail
        voice_treatment = VOICE_VISUAL_TREATMENT.get(voice, VOICE_VISUAL_TREATMENT["V1"])
        prompt = prompt.rstrip(".") + f". Additional detail draws the eye to {voice_treatment['detail_focus']}, each element contributing to the layered documentary texture."
        word_count = len(prompt.split())

    # Update scene state for continuity
    if scene_state:
        scene_state.advance(shot, cam_move, env_key, character_sentence)

    return {
        "prompt": prompt,
        "shot_type": shot,
        "environment": env_key,
        "camera_movement": cam_move,
        "duration_sec": target_duration_sec,
        "word_count": len(prompt.split()),
        "generation_params": {
            "dramatic_position": dramatic_pos,
            "voice": voice or "V1",
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
        voices: list of voice assignments (V1/V2/V3) per segment

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
                 else seg.get("voice", "V1"))

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

    parser = argparse.ArgumentParser(description="LTX-2.3 context-aware prompt generator v9")
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
