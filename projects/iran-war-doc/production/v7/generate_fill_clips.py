#!/usr/bin/env python3
"""
Generate narration-matched fill clip prompts for the Iran War Economy documentary.
Each fill clip is 5.04 seconds at 768x512, 24fps via LTX-2.3.
"""

import json
import re
import hashlib

# Load all input files
with open('/home/user/workspace/iran-war-doc/production/additional_clips_plan.json') as f:
    plan = json.load(f)

with open('/home/user/workspace/iran-war-doc/production/narration_script.json') as f:
    narration = json.load(f)

with open('/home/user/workspace/iran-war-doc/production/all_video_prompts.json') as f:
    existing_prompts = json.load(f)

# Build lookup: scene_number -> narration text
narration_by_scene = {}
for entry in narration:
    narration_by_scene[entry['scene_number']] = entry

# Build lookup: scene_number -> existing prompts
existing_by_scene = {}
for p in existing_prompts:
    sn = p['scene_number']
    if sn not in existing_by_scene:
        existing_by_scene[sn] = []
    existing_by_scene[sn].append(p)

# Extract the base style suffix from existing prompts
BASE_STYLE = "cinematic documentary, photorealistic, shot on Arri Alexa, 16:9 widescreen, shallow depth of field"

def get_color_palette(scene_number):
    """Extract color palette from existing clips for this scene."""
    if scene_number in existing_by_scene and existing_by_scene[scene_number]:
        return existing_by_scene[scene_number][0].get('color_palette', ['#C8D8EC', '#40C0D8', '#3A4A5A', '#E8C060'])
    return ['#C8D8EC', '#40C0D8', '#3A4A5A', '#E8C060']

def get_scene_lighting(scene_number, scene_title):
    """Determine lighting style based on scene context."""
    if scene_number in existing_by_scene and existing_by_scene[scene_number]:
        # Check last existing clip for lighting cues
        last_clip = existing_by_scene[scene_number][-1]
        prompt = last_clip.get('prompt', '')
        if 'cold blue-white institutional' in prompt:
            return 'cold blue-white institutional lighting, steel and glass surfaces'
        elif 'warm amber' in prompt:
            return 'warm amber light, lived-in textures, human scale'
        elif 'dramatic cinematic' in prompt:
            return 'dramatic cinematic lighting, high contrast shadows'
    return 'dramatic cinematic lighting, high contrast shadows'

def split_narration_into_sentences(text):
    """Split narration text into sentences for context matching."""
    # Clean up the text
    text = re.sub(r'\[pause\]', '', text)
    text = re.sub(r'V\d+\s*\([^)]*\)\s*:\s*', '', text)
    text = text.replace('\\n', ' ')
    # Split on sentence boundaries
    sentences = re.split(r'(?<=[.!?"])\s+', text)
    sentences = [s.strip().strip('"').strip() for s in sentences if s.strip() and len(s.strip()) > 10]
    return sentences

def get_later_narration_context(scene_number, existing_clip_count, new_clip_index, total_new_clips):
    """Get the narration context for the later part of the scene."""
    if scene_number not in narration_by_scene:
        return ""
    
    narr_text = narration_by_scene[scene_number].get('narration_text', '')
    sentences = split_narration_into_sentences(narr_text)
    
    if not sentences:
        return ""
    
    # The fill clips cover the LATER portion of narration
    # Calculate which portion of the narration corresponds to this fill clip
    total_sentences = len(sentences)
    
    # Existing clips cover roughly the first portion
    # We want to map fill clips to the later portion
    if existing_clip_count > 0:
        # Estimate where existing clips end (fraction of narration covered)
        plan_entry = None
        for p in plan:
            if p['scene_number'] == scene_number:
                plan_entry = p
                break
        
        if plan_entry:
            existing_duration = sum(c.get('target_duration_sec', 5) for c in existing_by_scene.get(scene_number, []))
            total_duration = existing_duration + plan_entry['gap_sec']
            existing_fraction = existing_duration / total_duration if total_duration > 0 else 0.5
        else:
            existing_fraction = 0.5
    else:
        existing_fraction = 0
    
    # Map this fill clip to a sentence range
    remaining_fraction = 1.0 - existing_fraction
    if total_new_clips > 0:
        clip_fraction_start = existing_fraction + (new_clip_index / total_new_clips) * remaining_fraction
        clip_fraction_end = existing_fraction + ((new_clip_index + 1) / total_new_clips) * remaining_fraction
    else:
        clip_fraction_start = existing_fraction
        clip_fraction_end = 1.0
    
    start_idx = int(clip_fraction_start * total_sentences)
    end_idx = min(int(clip_fraction_end * total_sentences) + 1, total_sentences)
    
    # Ensure at least one sentence
    if start_idx >= total_sentences:
        start_idx = total_sentences - 1
    if end_idx <= start_idx:
        end_idx = start_idx + 1
    
    context_sentences = sentences[start_idx:end_idx]
    return ' '.join(context_sentences[:2])  # Max 2 sentences for context


# Camera work variety pool
CAMERA_WORKS = [
    "Extreme close-up",
    "Close-up, shallow depth of field",
    "Medium close-up, rack focus",
    "Medium shot, slow dolly forward",
    "Medium wide, handheld slight drift",
    "Wide establishing shot, locked tripod",
    "Wide shot, slow push in",
    "Aerial drone shot, slow descent",
    "Aerial wide, slow lateral tracking",
    "Low angle, looking upward",
    "High angle, looking down",
    "Over-the-shoulder perspective",
    "Tracking shot, smooth lateral movement",
    "Slow crane up revealing landscape",
    "Static locked frame, no camera movement",
    "Pull-back reveal, steady dolly",
    "Macro close-up, extreme detail",
    "Dutch angle, slight tilt",
    "Bird's eye overhead view",
    "Dolly zoom, perspective shift",
]

# Scene-specific visual concepts based on narration analysis
SCENE_VISUAL_CONCEPTS = {
    1: {  # THE NUMBER
        "themes": ["financial trading", "market data", "oil tankers", "global scale"],
        "visuals": [
            "Empty trading floor at dawn, rows of dormant monitors reflecting blue light, vast corporate space",
            "Oil tanker wake cutting through dark ocean water, aerial perspective, golden hour light",
            "Financial district skyscraper reflections in glass facade, cold morning light, geometric patterns",
            "Cargo port at dusk, massive crane silhouettes against amber sky, industrial scale",
            "Control room with wall of monitoring screens, single operator silhouetted, cold blue glow",
            "Offshore oil platform in fog, industrial structure emerging from mist, dramatic isolation",
            "Empty boardroom table with leather chairs, morning light through blinds, institutional sterility",
            "Container ship bow cutting through waves, low angle from water level, power and momentum",
            "Financial district canyon of buildings, looking straight up, cold grey sky between towers",
            "Radar dish slowly rotating on naval vessel, twilight sky, mechanical precision",
            "Pipeline stretching to horizon across desert landscape, heat shimmer, golden light",
            "Empty airport departure gate at dawn, rows of seats, clinical fluorescent lighting",
            "Fuel gauge needle rising slowly, macro close-up, warm amber dashboard light",
            "Stock exchange floor after hours, empty and silent, overhead fluorescent hum",
            "Satellite dish array at dusk, geometric precision, cold blue-grey atmosphere",
            "Oil refinery at night, complex pipe network, industrial orange glow against dark sky",
            "Banker's hands resting on mahogany desk, morning light, stillness and weight",
        ]
    },
    2: {  # THE INVOICE
        "themes": ["military cost", "spending", "school budgets", "human cost"],
        "visuals": [
            "Calculator keys being pressed slowly, macro detail, each keystroke deliberate",
            "Paper receipt curling from printer, extreme close-up, warm light on paper edge",
            "Empty school cafeteria, rows of tables, harsh fluorescent light, institutional emptiness",
            "Military cargo plane on tarmac at dawn, massive scale, cold grey morning",
            "Government filing cabinet drawers, rows stretching deep, cold institutional corridor",
            "Pentagon exterior at twilight, geometric architecture, monumental scale, distant and imposing",
            "Child's empty desk in classroom, single shaft of light from window, poignant stillness",
            "Ammunition crates stacked in warehouse, industrial lighting, uniform repetition stretching to darkness",
            "Budget binder spine close-up, leather and paper texture, institutional detail",
            "School bus parked in empty lot, dawn light, yellow paint against grey asphalt",
            "Hands sorting through paper documents on desk, overhead angle, deliberate movement",
            "Military runway stretching to vanishing point, heat shimmer, vast emptiness",
            "Fluorescent light fixture buzzing in empty corridor, ceiling perspective, institutional decay",
            "Stack of folders on government desk, shallow focus, bureaucratic weight",
            "Empty playground at dusk, swings motionless, amber light fading",
            "Jet engine exhaust heat distortion, telephoto compression, raw power visualization",
            "Warehouse loading dock at night, single light illuminating concrete, industrial solitude",
        ]
    },
    3: {  # THE HUMAN SCALE
        "themes": ["gas prices", "families", "everyday cost", "oil reserves"],
        "visuals": [
            "Woman's hand gripping fuel nozzle, knuckles white, close-up at gas pump",
            "Gas station canopy at dusk, fluorescent glow against darkening sky, suburban isolation",
            "Car dashboard with fuel warning light, warm interior glow, anxiety in detail",
            "Suburban street with identical houses, aerial slowly descending, uniformity and scale",
            "Grocery store aisle, shopper checking prices, harsh overhead lighting, everyday struggle",
            "Oil storage tanks from above, geometric circles, industrial pattern, vast scale",
            "Family kitchen table with bills spread out, warm lamp light, domestic tension",
            "Server room corridor, endless racks of blinking lights, cold blue technological depth",
            "Gas station price display changing, slow motion, each increment weighted",
            "Suburban parking lot at dawn, empty except for one car, morning mist",
            "Woman driving, rearview mirror reflection, amber streetlight passing over her face",
            "Oil pipeline valve wheel, macro detail, industrial texture, cold steel",
            "Supermarket checkout, hands counting change, overhead fluorescent, economic pressure",
            "Petroleum refinery pipes against sunset, industrial silhouette, amber and steel grey",
            "Empty suburban cul-de-sac from above, morning shadows stretching long",
        ]
    },
    4: {  # THE DAILY BURN
        "themes": ["daily military cost", "budget comparison", "small town America", "infrastructure"],
        "visuals": [
            "Government office cubicle farm, overhead fluorescents, empty at dawn, institutional scale",
            "Small-town Main Street at blue hour, storefronts dark, single streetlight glowing",
            "Highway overpass from below, concrete geometry, pre-dawn blue sky, infrastructure weight",
            "Analyst's hands on keyboard, monitor glow on face, late-night institutional work",
            "Rural bridge over dry riverbed, aerial perspective, infrastructure decay",
            "Empty town square, park bench, fallen leaves, autumn amber light fading",
            "Federal building lobby, marble floor reflecting overhead lights, monumental emptiness",
            "Farm field at dawn, tractor silhouette on horizon, golden light, American landscape",
            "Strip mall with closed businesses, dusk light, economic stagnation visible",
            "Budget hearing room, empty chairs at curved desk, institutional gravitas",
            "Water tower silhouette against sunset, small-town landmark, isolation",
            "Interstate highway stretching to horizon, no traffic, dawn light, vast American distance",
        ]
    },
    5: {  # ACT TWO — THE WINNERS
        "themes": ["defense contractors", "profit", "corporate wealth", "stock surge"],
        "visuals": [
            "Corporate headquarters lobby, polished marble, morning light through floor-to-ceiling windows",
            "Defense contractor factory floor, robotic arms in motion, industrial production line",
            "Executive parking garage, luxury vehicles in row, cold concrete and LED lighting",
            "Stock ticker display, rapid upward movement, green glow reflected on glass surface",
            "Private jet on tarmac, sleek fuselage, golden hour light, exclusive wealth",
            "Corporate boardroom, empty leather chairs around long table, city skyline through windows",
            "Assembly line producing military equipment, precision machinery, cold industrial blue light",
            "Champagne glass being set down on mahogany table, extreme close-up, celebration detail",
            "Helicopter landing pad on corporate rooftop, city spread below, power perspective",
            "Defense industry trade show floor, empty before opening, exhibition hall vastness",
            "Corporate campus aerial, manicured lawns, geometric buildings, suburban wealth compound",
            "Lobbyist corridor in Washington, marble and brass, long perspective, power architecture",
        ]
    },
    6: {  # THE TRADERS
        "themes": ["oil trading", "futures market", "speculation", "algorithmic trading"],
        "visuals": [
            "Trading terminal screens cascading with data, blue-green glow, operator's hands hovering",
            "Commodity trading floor at peak hours, controlled chaos, institutional intensity",
            "Server rack blinking in data center, algorithmic trading infrastructure, cold precision",
            "Oil futures chart on large display, dramatic upward curve, blue institutional light",
            "Trader's eyes reflecting multiple screens, extreme close-up, concentration and calculation",
            "Financial district street at dawn, empty before the rush, wet pavement reflecting lights",
            "Cable bundles running through data center floor, infrastructure of digital finance",
            "Coffee cup beside multiple monitors, overnight trading session, fatigue and focus",
            "Fiber optic cables glowing, macro detail, data transmission visualization",
            "Exchange floor from overhead, geometric pattern of trading desks, institutional grid",
            "Bloomberg terminal close-up, cursor blinking, data fields, financial infrastructure",
            "Predawn city skyline, financial towers lit from within, isolated windows of activity",
        ]
    },
    7: {  # THE DEFENSE BOARD
        "themes": ["defense company boards", "CEO compensation", "corporate governance"],
        "visuals": [
            "Empty corporate boardroom, polished table reflecting ceiling lights, anticipation",
            "Executive elevator doors closing, brushed steel, cold corporate environment",
            "Corner office overlooking city, leather chair turned to window, power isolation",
            "Annual report pages fanning open, glossy paper, corporate imagery, macro detail",
            "Corporate atrium from below, glass and steel ascending, architectural power",
            "Mahogany conference table surface, wood grain detail, extreme close-up, corporate wealth",
            "Security desk in corporate lobby, monitors and barriers, controlled access",
            "Executive desk with pen and documents, morning light, deliberate composition",
            "Corporate hallway, long perspective, doors closed on both sides, institutional power",
            "Parking structure top level, single luxury car, city panorama behind, solitary wealth",
            "Glass-walled office interior, minimalist furniture, city lights beyond, controlled environment",
            "Corporate campus at dawn, pristine landscaping, monumental entrance, wealth architecture",
        ]
    },
    8: {  # THE REVOLVING DOOR
        "themes": ["government-industry revolving door", "lobbying", "Pentagon", "Washington"],
        "visuals": [
            "Revolving glass door spinning slowly, distorted reflections, institutional entrance",
            "Pentagon exterior at dusk, massive facade, geometric precision, institutional power",
            "K Street corridor in Washington, empty at dawn, lobbyist territory, power architecture",
            "Government building entrance stairs, wide establishing shot, marble and columns",
            "Security checkpoint in government building, metal detectors, institutional control",
            "Washington monument visible through office window, power symbolism, golden hour",
            "Congressional hallway, marble floors, distant figures, institutional scale",
            "Government ID badge on lanyard, macro close-up, institutional identity",
            "Capitol dome at twilight, architectural grandeur, political power center",
            "Office door name plate being changed, macro detail, transition of power",
            "Briefcase on government desk, leather texture, morning light through blinds",
            "Washington rooftops at sunset, institutional architecture stretching to horizon",
        ]
    },
    9: {  # THE SUPPLY CHAIN
        "themes": ["military supply chain", "subcontractors", "small businesses", "manufacturing"],
        "visuals": [
            "Factory floor with precision machinery, sparks flying, blue-collar manufacturing",
            "Warehouse shelves stacked with components, industrial scale, overhead fluorescent",
            "Welding torch illuminating worker's face shield, industrial close-up, intense light",
            "Loading dock with pallets of supplies, forklift in motion, logistics infrastructure",
            "Small manufacturing plant exterior, rural industrial park, morning fog",
            "Assembly line worker's hands installing components, precise movements, macro detail",
            "Shipping container yard from above, geometric grid, industrial logistics pattern",
            "Truck convoy on interstate at dawn, military supply chain in motion",
            "Metal stamping press in operation, industrial power, rhythmic mechanical force",
            "Quality control station, inspector examining component under magnification, precision",
            "Rural factory smokestacks against grey sky, blue-collar landscape, working class",
            "Conveyor belt carrying components, steady industrial rhythm, cold lighting",
        ]
    },
    10: {  # THE STOCK BUYBACK
        "themes": ["stock buybacks", "share price manipulation", "corporate finance", "shareholder value"],
        "visuals": [
            "Stock chart display showing vertical climb, blue-green institutional glow",
            "Corporate financial documents on executive desk, shallow focus, morning light",
            "Wall Street exterior at dawn, institutional architecture, financial power center",
            "Fountain pen signing documents, extreme close-up, decisive corporate action",
            "Trading floor monitors showing upward movement, green reflected on analyst's glasses",
            "Corporate safe vault door, heavy steel, security and wealth storage",
            "Financial analyst studying dual monitors, late-night office, screen glow on face",
            "Shareholder meeting room, empty chairs in rows, corporate governance stage",
            "Calculator and financial printout, overhead angle, accounting detail",
            "New York financial district at golden hour, monumental buildings, warm light on stone",
            "Executive hand reaching for phone on pristine desk, deliberate corporate action",
            "Bloomberg terminal scrolling financial data, green and blue glow, institutional technology",
        ]
    },
    11: {  # THE PENSION
        "themes": ["pension funds", "retirement", "worker savings", "institutional investment"],
        "visuals": [
            "Retired worker's hands resting on kitchen table, weathered skin, warm domestic light",
            "Pension fund office, filing cabinets, institutional grey, fluorescent overhead",
            "Factory gate at shift change, workers streaming out, industrial golden hour",
            "Retirement community common room, empty in morning light, institutional comfort",
            "Social Security office waiting room, institutional chairs, harsh lighting, patient waiting",
            "Worker's hardhat on hook at factory entrance, end of shift, amber light",
            "Pension statement document, macro close-up, bureaucratic detail on paper",
            "Assembly line at dawn, machines starting up, industrial awakening",
            "Suburban home mailbox, morning light, routine and vulnerability",
            "Union hall interior, folding chairs in rows, working class institutional space",
            "Construction site at dusk, crane silhouette, infrastructure and labor",
            "Office coffee mug beside calculator, morning light, everyday financial reality",
        ]
    },
    12: {  # THE INSURANCE PREMIUM
        "themes": ["insurance costs", "healthcare", "premium increases", "corporate profit"],
        "visuals": [
            "Insurance office waiting room, plastic chairs, fluorescent light, institutional anonymity",
            "Medical bill envelope being opened, close-up on hands, domestic kitchen table",
            "Hospital corridor at night, empty stretchers, clinical fluorescent glow",
            "Prescription bottles on bathroom shelf, macro detail, healthcare cost visualization",
            "Insurance company headquarters, glass tower, cold corporate architecture",
            "Emergency room entrance at dawn, ambulance bay, institutional readiness",
            "Doctor's office examination room, empty, medical equipment, clinical sterility",
            "Pharmacy counter, medication being counted, institutional healthcare detail",
            "Family reviewing documents at kitchen table, lamp light, domestic financial stress",
            "Health insurance card, macro close-up, plastic detail, identity and cost",
            "Hospital parking lot at night, scattered cars, clinical atmosphere extending outside",
            "Stethoscope on desk beside computer, medical-corporate intersection",
        ]
    },
    13: {  # THE GROCERY BILL
        "themes": ["food prices", "inflation", "everyday economics", "supply chain"],
        "visuals": [
            "Grocery store produce section, fluorescent overhead, shopper examining prices",
            "Shopping cart wheel rolling over tile floor, low angle, consumer perspective",
            "Checkout conveyor belt, items passing scanner, routine commerce macro",
            "Empty grocery shelves, gaps in product rows, scarcity visualization",
            "Truck delivering goods to supermarket loading dock, dawn, supply chain endpoint",
            "Hand selecting item from shelf, price tag visible as blur, consumer decision",
            "Grocery store aisle from above, geometric pattern, consumer infrastructure",
            "Cash register drawer opening, coins and bills, commerce at point of sale",
            "Warehouse distribution center, conveyor systems, logistics scale",
            "Farm field being harvested, aerial perspective, agricultural supply chain origin",
            "Refrigerated truck interior, produce crates, cold storage logistics",
            "Family kitchen, meal preparation, domestic economy in action, warm light",
        ]
    },
    14: {  # THE RENT INCREASE
        "themes": ["housing costs", "rent", "landlords", "displacement"],
        "visuals": [
            "Apartment building exterior at dusk, windows lit unevenly, urban residential",
            "Keys on kitchen counter, close-up, domestic anxiety, warm lamp light",
            "For-rent sign on apartment building, urban street, late afternoon light",
            "Empty apartment interior, bare walls, sunlight through window, vacancy",
            "Moving boxes stacked in hallway, displacement in progress, harsh overhead light",
            "Apartment door lock being turned, extreme close-up, chrome and wood detail",
            "Urban streetscape at dawn, row houses stretching away, residential scale",
            "Landlord's office desk, property documents, institutional-domestic intersection",
            "Fire escape exterior, geometric pattern, urban housing infrastructure",
            "Neighborhood from above, rooftops and shadows, residential density pattern",
            "Apartment building lobby, mailboxes, institutional residential space",
            "Window reflection showing city skyline, interior-exterior boundary, urban living",
        ]
    },
    15: {  # THE INVISIBLE TAX
        "themes": ["hidden costs", "inflation as tax", "consumer burden", "economic pressure"],
        "visuals": [
            "Wallet being opened, worn leather, few bills inside, close-up, domestic light",
            "Gas pump display ticking upward, slow motion, each increment weighted",
            "Power lines stretching to horizon, infrastructure carrying hidden costs, amber sunset",
            "Water meter spinning, macro close-up, utility consumption, institutional detail",
            "Commuter train passing through suburban station, blur of motion, daily grind",
            "Thermostat being adjusted downward, wall-mounted, close-up, cost-conscious gesture",
            "Utility bill on kitchen counter, warm lamp light, monthly burden",
            "Highway toll booth at dawn, cars passing through, systematic extraction",
            "Light switch being flipped off in empty room, energy consciousness",
            "Laundromat machines spinning, institutional lighting, mundane economic reality",
            "Bus stop bench at dawn, single figure waiting, public transit dependence",
            "Parking meter close-up, mechanical detail, micro-taxation of daily life",
        ]
    },
    16: {  # THE INTEREST RATE
        "themes": ["Federal Reserve", "interest rates", "monetary policy", "debt burden"],
        "visuals": [
            "Federal Reserve building exterior, neoclassical architecture, institutional power",
            "Bank vault door, massive steel hinges, security and monetary control",
            "Mortgage documents on desk, fountain pen beside them, financial commitment",
            "Bank teller window, glass partition, institutional financial interface",
            "Federal building columns from low angle, monumental institutional power",
            "Calculator display showing rising numbers, macro close-up, financial computation",
            "Suburban house with for-sale sign, morning light, real estate and interest rates",
            "Central bank corridor, marble floors, institutional solemnity",
            "Banker's lamp illuminating documents, warm pool of light in dark office",
            "Construction crane frozen mid-swing, stalled development, interest rate impact",
            "Small business storefront, morning preparation, vulnerable to rate changes",
            "Treasury bond certificate, macro detail, ornate financial instrument",
        ]
    },
    17: {  # THE SECOND-ORDER
        "themes": ["cascading effects", "second-order consequences", "economic ripple"],
        "visuals": [
            "Ripple spreading across still water surface, macro detail, causation visualization",
            "Domino falling in slow motion, chain reaction beginning, mechanical causation",
            "Highway interchange from above, traffic flowing in complex patterns, systemic movement",
            "Factory conveyor belt stopping, sudden stillness, cascading halt",
            "Airport departure board, flight cancellations accumulating, systemic disruption",
            "Electrical grid transmission tower, power lines vibrating, infrastructure stress",
            "Rush hour traffic from above, brake lights cascading through lanes, systemic delay",
            "Warehouse going dark section by section, progressive shutdown visualization",
            "Bridge support cables under strain, close-up on steel, structural tension",
            "Assembly line halted mid-operation, unfinished product, economic interruption",
            "Train yard at dusk, stationary freight cars, logistics frozen",
            "Power plant cooling towers with diminishing steam, industrial slowdown",
        ]
    },
    18: {  # THE PROPAGANDA
        "themes": ["media narrative", "propaganda", "public opinion", "information war"],
        "visuals": [
            "Television screens showing static in electronics store window, media saturation",
            "Newsroom at dawn, empty anchor desk, broadcast infrastructure, cold set lighting",
            "Printing press rollers turning, ink and paper, media production machinery",
            "Radio tower at dusk, signal lights blinking, broadcast infrastructure against sky",
            "Stack of newspapers in dawn delivery, bundled information, media distribution",
            "Satellite uplink dish pointed at sky, communication infrastructure, dusk light",
            "Microphone on podium in empty press room, institutional media interface",
            "Television satellite truck at dawn, equipment ready, media deployment",
            "Cable news monitor wall, multiple channels, information overload visualization",
            "Printing facility at night, automated paper processing, media machine",
            "Broadcast control room, monitoring stations, institutional media management",
            "Cell phone tower against evening sky, telecommunications infrastructure",
        ]
    },
    19: {  # THE TIMELINE
        "themes": ["war timeline", "escalation", "diplomatic failure", "military deployment"],
        "visuals": [
            "Aircraft carrier deck at dawn, crew preparing, military operational readiness",
            "Military transport plane taking off, low angle, power and deployment",
            "Command center with large situation display, blue institutional glow, strategic overview",
            "Naval fleet formation from above, ships in pattern, military projection",
            "Desert landscape at dawn, vast emptiness, pre-conflict stillness",
            "Military helicopter on tarmac, rotors still, dawn preparation, anticipation",
            "Diplomatic conference table, empty chairs, negotiation space unused",
            "Military base gate at dawn, security checkpoint, institutional boundary",
            "Jet engines powering up on flight deck, heat distortion, raw military force",
            "Radar screen with sweeping line, command and control, surveillance technology",
            "Military cemetery rows of markers, solemn perspective, cost of conflict",
            "Empty embassy corridor, flags still, diplomatic architecture, abandoned process",
        ]
    },
    20: {  # THE HUMANITARIAN
        "themes": ["humanitarian crisis", "refugees", "civilian impact", "aid"],
        "visuals": [
            "Aid supplies on loading dock, white boxes stacked, humanitarian logistics",
            "Desert road stretching to horizon, single vehicle, humanitarian access",
            "Refugee camp from above, organized rows of shelters, humanitarian scale",
            "Water distribution point, plastic containers lined up, basic needs infrastructure",
            "Hospital generator running, vibration and exhaust, improvised medical power",
            "Humanitarian aid warehouse, high shelves, institutional care at scale",
            "Road with crater, detour visible, infrastructure damage, civilian impact",
            "Emergency medical tent interior, equipment arranged, field hospital readiness",
            "Child's shoe abandoned on dusty road, macro detail, displacement evidence",
            "Cargo plane unloading supplies, rear ramp open, aid delivery",
            "Emergency communication equipment, satellite phone and map, coordination tools",
            "Sunset over damaged urban skyline, silhouette of destruction, twilight aftermath",
        ]
    },
    21: {  # THE RECONSTRUCTION
        "themes": ["rebuilding", "reconstruction contracts", "profit from rebuilding"],
        "visuals": [
            "Construction crane against dawn sky, rebuilding beginning, industrial renewal",
            "Bulldozer clearing rubble, dust cloud, reconstruction machinery in action",
            "Architectural blueprints unrolled on desk, planning phase, institutional process",
            "Concrete being poured from mixer, close-up flow, reconstruction material",
            "Construction workers on scaffold, aerial perspective, human labor in rebuilding",
            "Damaged infrastructure being surveyed, engineer with equipment, assessment phase",
            "Procurement office with stacked bid documents, competitive contracting",
            "Heavy equipment parked at construction site, dawn, reconstruction staging",
            "Steel beams being lifted by crane, reconstruction progress, industrial strength",
            "Construction site from above, organized chaos, rebuilding pattern",
            "Worker welding structural steel, sparks cascade, industrial reconstruction detail",
            "Cement truck arriving at site, morning dust, logistics of rebuilding",
        ]
    },
    22: {  # THE AUDIT
        "themes": ["financial audit", "accountability", "oversight failure", "transparency"],
        "visuals": [
            "Auditor's desk with stacks of financial records, institutional lighting, oversight work",
            "Government accountability office exterior, institutional architecture, oversight institution",
            "Magnifying glass over financial document, macro detail, scrutiny visualization",
            "Filing room with floor-to-ceiling records, institutional archive, evidence scale",
            "Congressional hearing room, empty witness chair, accountability space",
            "Shredder processing documents, close-up, accountability gap",
            "Inspector's hand running down column of figures, institutional desk, oversight detail",
            "Evidence boxes stacked in storage, institutional archive, investigation material",
            "Empty witness stand in hearing room, microphone waiting, accountability theater",
            "Secure document vault, heavy door ajar, institutional security",
            "Audit trail of documents on conference table, institutional oversight process",
            "Government seal on building entrance, institutional authority, oversight mandate",
        ]
    },
    23: {  # THE FUTURE COST
        "themes": ["long-term costs", "veteran care", "future debt", "lasting impact"],
        "visuals": [
            "VA hospital corridor, long perspective, institutional medical care, cold lighting",
            "Prosthetic limb workshop, precision manufacturing, rehabilitation technology",
            "Veteran's medication bottles on nightstand, domestic medical reality, warm light",
            "Wheelchair wheel rolling on institutional floor, close-up, rehabilitation journey",
            "Debt ceiling documents on Congressional desk, institutional policy, future burden",
            "Calendar pages turning, close-up, time and accumulating cost",
            "Government bond certificate detail, financial obligation, institutional commitment",
            "Physical therapy room, parallel bars, institutional rehabilitation space",
            "Young soldier's boots beside veteran's cane, generational cost visualization",
            "Medical records filing system, institutional healthcare bureaucracy",
            "Sunset through hospital window, patient's perspective, healing and time",
            "Treasury building at dusk, institutional finance, long-term obligation",
        ]
    },
    24: {  # THE RECKONING
        "themes": ["accountability moment", "systemic failure", "democratic cost"],
        "visuals": [
            "Courtroom gallery, empty seats, institutional justice space, morning light",
            "Scales of justice silhouette, institutional symbol, balance and accountability",
            "Congressional chamber from above, empty seats, democratic architecture",
            "Gavel resting on bench, extreme close-up, wood grain detail, judicial authority",
            "Witness table with microphone, empty chair, accountability moment pending",
            "Public square at dawn, empty, democratic space unused, civic architecture",
            "Government document stamp pressing down, institutional action, macro detail",
            "Ballot box in empty polling station, democratic infrastructure, civic duty space",
            "Capitol steps at dawn, institutional grandeur, democratic power center",
            "Press conference podium, empty, microphones arranged, accountability stage",
            "Courthouse hallway, marble and wood, institutional justice corridor",
            "American flag at half-staff, slow motion fabric movement, national reflection",
        ]
    },
    25: {  # THE CONNECTION
        "themes": ["connecting the dots", "systemic picture", "who paid who profited"],
        "visuals": [
            "Cork board with connected strings in empty office, investigation visualization",
            "Dual image: gas pump nozzle and corporate tower, parallel worlds, split composition",
            "Highway connecting suburban homes to industrial district, aerial, systemic link",
            "Hands sorting through evidence documents, desk covered in files, investigation",
            "Suburban home at dawn alongside distant refinery smoke, connected landscapes",
            "Shipping lane marked by container vessels, aerial, global connection visible",
            "Tax form beside defense contract document, institutional paperwork, systemic link",
            "Worker leaving factory as executive enters office tower, parallel lives, dawn",
            "Road connecting small town to military installation, landscape perspective",
            "Supply chain warehouse connecting military and civilian goods, logistics link",
            "Morning commute and military convoy sharing same highway, dual purpose",
            "Bank building shadow falling over residential street, systemic power geometry",
        ]
    },
    26: {  # THE CLOSING
        "themes": ["final reflection", "the full picture", "who pays who profits", "conclusion"],
        "visuals": [
            "Dawn breaking over American cityscape, slow reveal, new perspective light",
            "Empty trading floor monitors going dark one by one, end of cycle",
            "Gas station at dawn, first customer arriving, daily cycle resuming",
            "Military cemetery at golden hour, rows stretching to horizon, ultimate cost",
            "Small-town Main Street awakening, first lights turning on, resilient community",
            "Oil tanker at sunset, distant horizon, global scale of consequence",
            "Family kitchen table cleared of bills, morning light, domestic hope",
            "Federal building at dawn, flag rippling, institutional persistence",
            "Factory shift change at dawn, workers arriving, economic cycle continuing",
            "Suburban street from above, morning routines beginning, ordinary life resuming",
            "Empty courtroom at dawn, light entering through high windows, justice pending",
            "Ocean horizon at dawn, vast and indifferent, natural scale dwarfing human conflict",
        ]
    },
}

def build_prompt(visual_desc, lighting, camera, palette):
    """Build a complete LTX-2.3 prompt within character limits."""
    # Core visual + lighting
    palette_str = ", ".join(palette)
    
    # Build prompt with style elements
    prompt = f"{visual_desc}, {camera.lower()}. {lighting}. {BASE_STYLE}, subtle film grain, color palette: {palette_str}"
    
    # Trim if too long (max 350 chars)
    if len(prompt) > 350:
        # Shorten by removing some style elements
        prompt = f"{visual_desc}, {camera.lower()}. {lighting}. {BASE_STYLE}, color palette: {palette_str}"
    
    if len(prompt) > 350:
        # Further trim
        prompt = f"{visual_desc}. {lighting}. {BASE_STYLE}, color palette: {palette_str}"
    
    if len(prompt) > 350:
        prompt = f"{visual_desc}. {BASE_STYLE}, color palette: {palette_str}"
    
    # Ensure minimum length (pad with atmosphere if needed)
    if len(prompt) < 150:
        prompt = f"{visual_desc}, {camera.lower()}, atmospheric depth. {lighting}. {BASE_STYLE}, anamorphic lens flare, subtle film grain, color palette: {palette_str}"
    
    return prompt


def generate_fill_clips():
    """Generate all fill clip prompts."""
    fill_clips = []
    summary = {}
    
    for scene_plan in plan:
        scene_num = scene_plan['scene_number']
        scene_title = scene_plan['scene_title']
        new_clips_needed = scene_plan['new_clips_needed']
        existing_count = scene_plan['existing_clips']
        
        if new_clips_needed <= 0:
            continue
        
        palette = get_color_palette(scene_num)
        lighting = get_scene_lighting(scene_num, scene_title)
        
        # Get available visuals for this scene
        if scene_num in SCENE_VISUAL_CONCEPTS:
            available_visuals = SCENE_VISUAL_CONCEPTS[scene_num]['visuals']
        else:
            # Fallback - use generic documentary visuals
            available_visuals = [
                "Wide establishing shot of urban skyline at dawn, institutional architecture",
                "Close-up of hands working at desk, institutional lighting, deliberate motion",
                "Aerial view of infrastructure, roads and buildings, morning light",
                "Interior corridor with institutional lighting, long perspective",
                "Industrial landscape at dusk, smokestacks and cranes silhouetted",
                "Government building exterior, columns and marble, institutional power",
                "Empty conference room, polished table reflecting overhead lights",
                "Suburban landscape from above, houses and roads, morning shadows",
                "Document close-up on desk, pen beside it, institutional detail",
                "Highway at dawn, empty lanes stretching to vanishing point",
                "Water surface reflecting sky, natural indifference to human activity",
                "Power lines at sunset, infrastructure stretching across landscape",
            ]
        
        scene_clips_generated = 0
        
        for fill_idx in range(new_clips_needed):
            clip_index = existing_count + fill_idx
            
            # Get narration context for this position
            narr_context = get_later_narration_context(
                scene_num, existing_count, fill_idx, new_clips_needed
            )
            
            # Select visual (cycle through available, ensuring variety)
            visual_idx = fill_idx % len(available_visuals)
            visual_desc = available_visuals[visual_idx]
            
            # Select camera work (varied across clips)
            camera_idx = (fill_idx * 3 + scene_num) % len(CAMERA_WORKS)
            camera = CAMERA_WORKS[camera_idx]
            
            # Build the prompt
            prompt = build_prompt(visual_desc, lighting, camera, palette)
            
            # Ensure no text/letters/numbers in prompt
            # Remove any references to visible text
            prompt = re.sub(r'\btext\b|\bletters\b|\bwords\b|\bsubtitles\b|\blogos?\b|\bnumbers? visible\b|\bsign\b|\blabel\b', '', prompt, flags=re.IGNORECASE)
            prompt = re.sub(r'\s{2,}', ' ', prompt).strip()
            
            clip_id = f"scene_{scene_num:02d}_fill{fill_idx:02d}"
            
            fill_clips.append({
                "clip_id": clip_id,
                "scene_number": scene_num,
                "scene_title": scene_title,
                "clip_index": clip_index,
                "target_duration_sec": 5,
                "ltx_clips_needed": 1,
                "prompt": prompt,
                "narration_context": narr_context if narr_context else f"Scene {scene_num}: {scene_title} - later narration segment"
            })
            
            scene_clips_generated += 1
        
        summary[f"Scene {scene_num}: {scene_title}"] = scene_clips_generated
    
    return fill_clips, summary


# Generate
fill_clips, summary = generate_fill_clips()

# Validate
print("=" * 60)
print("FILL CLIPS GENERATION SUMMARY")
print("=" * 60)
print(f"\nTotal fill clips generated: {len(fill_clips)}")
print(f"\nPer-scene breakdown:")
for scene_name, count in summary.items():
    print(f"  {scene_name}: {count} clips")

# Validate prompt lengths
too_short = [c for c in fill_clips if len(c['prompt']) < 150]
too_long = [c for c in fill_clips if len(c['prompt']) > 350]
print(f"\nPrompts under 150 chars: {len(too_short)}")
print(f"Prompts over 350 chars: {len(too_long)}")

if too_short:
    print("  Short prompts:")
    for c in too_short[:5]:
        print(f"    {c['clip_id']}: {len(c['prompt'])} chars")

if too_long:
    print("  Long prompts:")
    for c in too_long[:5]:
        print(f"    {c['clip_id']}: {len(c['prompt'])} chars")

# Check uniqueness
prompts_set = set(c['prompt'] for c in fill_clips)
print(f"\nUnique prompts: {len(prompts_set)} / {len(fill_clips)}")

# Check for text/letter references
text_refs = [c for c in fill_clips if any(w in c['prompt'].lower() for w in ['text', 'letter', 'word', 'subtitle', 'logo', 'sign', 'label'])]
print(f"Prompts with text references: {len(text_refs)}")

# Avg prompt length
avg_len = sum(len(c['prompt']) for c in fill_clips) / len(fill_clips) if fill_clips else 0
print(f"Average prompt length: {avg_len:.0f} chars")

# Save output
with open('/home/user/workspace/iran-war-doc/production/fill_clips_final.json', 'w') as f:
    json.dump(fill_clips, f, indent=2)

print(f"\nOutput saved to fill_clips_final.json")
print(f"Total entries: {len(fill_clips)}")
