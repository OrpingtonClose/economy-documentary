#!/usr/bin/env python3
"""
Generate narration-matched fill clip prompts for the Iran War Economy documentary.
Each fill clip is 5.04 seconds at 768x512, 24fps via LTX-2.3.
V2: Scene-specific visuals matched to actual narration content.
"""

import json
import re

# Load all input files
with open('/home/user/workspace/iran-war-doc/production/additional_clips_plan.json') as f:
    plan = json.load(f)

with open('/home/user/workspace/iran-war-doc/production/narration_script.json') as f:
    narration = json.load(f)

with open('/home/user/workspace/iran-war-doc/production/all_video_prompts.json') as f:
    existing_prompts = json.load(f)

# Build lookups
narration_by_scene = {e['scene_number']: e for e in narration}
existing_by_scene = {}
for p in existing_prompts:
    sn = p['scene_number']
    if sn not in existing_by_scene:
        existing_by_scene[sn] = []
    existing_by_scene[sn].append(p)

BASE_STYLE = "cinematic documentary, photorealistic, shot on Arri Alexa, 16:9 widescreen, shallow depth of field"

def get_color_palette(scene_number):
    if scene_number in existing_by_scene and existing_by_scene[scene_number]:
        return existing_by_scene[scene_number][-1].get('color_palette', ['#C8D8EC', '#40C0D8', '#3A4A5A', '#E8C060'])
    return ['#C8D8EC', '#40C0D8', '#3A4A5A', '#E8C060']

def split_narration_into_sentences(text):
    text = re.sub(r'\[pause\]', '', text)
    text = re.sub(r'V\d+\s*\([^)]*\)\s*:\s*', '', text)
    text = text.replace('\\n', ' ').replace('\n', ' ')
    sentences = re.split(r'(?<=[.!?"])\s+', text)
    sentences = [s.strip().strip('"').strip() for s in sentences if s.strip() and len(s.strip()) > 15]
    return sentences

def get_later_narration_context(scene_number, existing_clip_count, fill_idx, total_new_clips):
    if scene_number not in narration_by_scene:
        return ""
    narr_text = narration_by_scene[scene_number].get('narration_text', '')
    sentences = split_narration_into_sentences(narr_text)
    if not sentences:
        return ""
    total_sentences = len(sentences)
    
    plan_entry = next((p for p in plan if p['scene_number'] == scene_number), None)
    if plan_entry and existing_clip_count > 0:
        existing_duration = sum(c.get('target_duration_sec', 5) for c in existing_by_scene.get(scene_number, []))
        total_duration = existing_duration + plan_entry['gap_sec']
        existing_fraction = existing_duration / total_duration if total_duration > 0 else 0.5
    else:
        existing_fraction = 0
    
    remaining_fraction = 1.0 - existing_fraction
    if total_new_clips > 0:
        frac_start = existing_fraction + (fill_idx / total_new_clips) * remaining_fraction
        frac_end = existing_fraction + ((fill_idx + 1) / total_new_clips) * remaining_fraction
    else:
        frac_start, frac_end = existing_fraction, 1.0
    
    start_idx = max(0, min(int(frac_start * total_sentences), total_sentences - 1))
    end_idx = min(int(frac_end * total_sentences) + 1, total_sentences)
    if end_idx <= start_idx:
        end_idx = start_idx + 1
    
    return ' '.join(sentences[start_idx:min(end_idx, start_idx + 2)])

# Camera work variety pool - 20 distinct approaches
CAMERA_WORKS = [
    "extreme close-up, razor-thin focus",
    "close-up with shallow depth of field",
    "medium close-up, slow rack focus shift",
    "medium shot, gentle dolly forward",
    "medium wide, handheld with subtle drift",
    "wide establishing shot, locked tripod",
    "wide shot, imperceptible push in",
    "aerial drone descent, smooth and deliberate",
    "aerial wide, slow lateral tracking movement",
    "low angle looking upward, imposing perspective",
    "high angle looking down, overhead vantage",
    "tracking shot, smooth lateral glide",
    "slow crane rising to reveal landscape",
    "static locked frame, perfectly still composition",
    "pull-back reveal, steady backward dolly",
    "macro detail shot, extreme magnification",
    "telephoto compression, flattened perspective",
    "bird's-eye overhead composition",
    "slow tilt upward revealing full height",
    "steady tracking forward into space",
]

# ============================================================
# Scene-specific visual pools matched to actual narration content
# Each scene has visuals for its LATER narration (what fill clips cover)
# ============================================================

SCENE_VISUALS = {
    1: [  # THE NUMBER - later: "the information is not hidden", "follow the money", "who paid, who profited"
        "CSIS research building exterior at dusk, institutional architecture, cold grey stone facade",
        "Goldman Sachs tower reflecting sunset, glass and steel monolith, financial power center",
        "Empty analyst workstation with multiple dark monitors, overnight stillness, blue ambient glow",
        "Oil tanker bow cutting through dark Atlantic water, white wake spreading, golden hour light",
        "Aerial view of port facility at dawn, container cranes motionless, industrial scale",
        "Filing cabinet drawer sliding open, institutional grey metal, cold fluorescent corridor",
        "Trading floor at closing bell, empty chairs scattered, aftermath atmosphere",
        "Satellite image of smoke column rising from coastline, orbital perspective, earth curvature visible",
        "Gas station forecourt at dawn, wet asphalt reflecting canopy lights, suburban stillness",
        "Spreadsheet printout on desk with coffee ring stain, institutional workspace detail",
        "Oil pipeline junction valve in desert landscape, industrial infrastructure isolated in sand",
        "Financial district canyon of towers at blue hour, cold light between glass facades",
        "Cargo vessel navigating narrow shipping channel, pilot boat alongside, maritime commerce",
        "Pentagon exterior at twilight, geometric facade stretching wide, institutional power",
        "Crude oil sample in glass vial held to light, amber liquid detail, commodity essence",
        "Small business owner's hands on counter, morning light through storefront window",
        "Ocean horizon line bisecting frame, tanker silhouette against dawn sky, global scale",
    ],
    2: [  # THE INVOICE - later: munitions depletion, emergency production, "every Tomahawk fired needs replacing"
        "Military warehouse with depleted ammunition racks, empty shelving stretching back, cold overhead light",
        "Defense factory floor, robotic welding arm assembling guidance housing, sparks in darkness",
        "Tomahawk cruise missile body on assembly cradle, precision manufacturing, institutional blue light",
        "THAAD interceptor launch rail empty and elevated, military base at dusk, readiness without supply",
        "Procurement office desk with stacked bid folders, institutional grey, fluorescent hum",
        "Munitions production line at dawn shift start, workers entering clean room, controlled environment",
        "Military logistics aircraft cargo bay, empty pallets, cavernous interior, operational void",
        "Defense contractor quality inspection station, magnified lens over circuit board, precision detail",
        "Empty missile storage bunker, concrete walls and blast doors, institutional military architecture",
        "Factory floor conveyor belt carrying unfinished ordnance components, assembly rhythm",
        "Government contract binder spine on shelf, leather and brass, institutional weight",
        "Military airfield at dawn, empty hardstands where aircraft should be, operational gap visible",
    ],
    3: [  # THE HUMAN SCALE - later: SPR release, deferred bill, refill cost, "not solved, just deferred"
        "Strategic Petroleum Reserve salt cavern entrance, industrial door in Louisiana landscape, golden hour",
        "Underground oil storage facility ceiling dripping, industrial cave, eerie amber illumination",
        "SPR pump station at dawn, pipeline headers and valve manifolds, Gulf Coast industrial",
        "Suburban family loading groceries into car, parking lot, afternoon light, domestic routine",
        "Oil futures trading pit, empty after hours, institutional floor littered with paper",
        "Aerial of Gulf Coast salt dome storage facility, circular access points in marshland",
        "Kitchen table with calculator and utility bills, warm lamp light, domestic accounting",
        "Gasoline tanker truck departing refinery gate at dawn, supply chain in motion",
        "Aerial of highway interchange at rush hour, commuter patterns, systemic fuel dependence",
        "Oil refinery cooling towers at sunset, steam rising, energy infrastructure silhouette",
        "Government budget office at night, single desk lamp illuminating deferred cost analysis",
        "Suburban cul-de-sac from above, morning shadows, American domestic landscape at scale",
        "Fuel depot storage tanks in morning fog, cylindrical forms in mist, supply reserves",
        "Woman's hands turning car ignition, dashboard glowing, daily fuel dependence moment",
    ],
    4: [  # THE DAILY BURN - later: temporal compression of war finance, "who decided it was affordable?"
        "Options trading screen showing crude oil contracts, blue-green institutional glow on glass",
        "Military cargo plane on night tarmac, engines running, loading in progress, operational pace",
        "Vietnam War era military photograph fading into modern equivalent, historical echo",
        "Empty congressional budget hearing room, microphones at curved desk, accountability void",
        "Defense budget document stack on analyst desk, institutional lighting, bureaucratic scale",
        "Military refueling operation at night, fuel flowing through hoses, operational burn rate",
        "Air Force Academy campus at dawn, institutional buildings, scale of what one day costs",
        "Crude oil futures chart curving sharply upward, monitor glow in dark room, financial acceleration",
        "Small-town storefront at blue hour, vacancy in window, economic casualty of distant decisions",
    ],
    5: [  # EIGHT WEEKS EARLY - later: Brent crude call options, specific strike prices, paid off in 48 hours
        "CFTC filing database interface, institutional screen, regulatory data infrastructure",
        "Hedge fund trading desk at dawn, multiple monitors with crude oil positions, pre-market",
        "Brent crude futures contract printout, institutional desk, financial instrument detail",
        "Oil options strike price display, clustered targets visible as data, institutional screen glow",
        "London financial district at pre-dawn, isolated lit windows in dark towers, early positioning",
        "Commodity trader reviewing position reports, single desk lamp, concentrated analysis",
        "Fuel receipt spread across truck driver's dashboard, three months of price history, personal record",
    ],
    6: [  # THE TANKER TRADE - later: gold ETF inflows, "19 billion in gold ETFs", safe haven
        "Gold bullion vault, bars stacked in secure storage, warm metallic glow against cold steel",
        "Supertanker hull from water level, massive steel wall moving through sea, industrial scale",
        "Gold ETF trading activity on institutional display, yellow-amber data against dark interface",
        "Tanker loading terminal at dusk, crude flowing through articulated arm, maritime industry",
        "Safe deposit vault door, heavy steel mechanism, wealth preservation infrastructure",
        "Aerial of tanker fleet anchored in formation, maritime staging, multiple vessels waiting",
        "Commodity exchange floor after hours, empty trading posts, institutional aftermath",
        "Ship propeller churning dark water, underwater perspective, maritime power and displacement",
    ],
    7: [  # THE LEGAL INSIDE TRADE - later: incentive structure gap, "legal doesn't mean uninformed"
        "Congressional office hallway, polished floor, institutional power, closed doors",
        "Financial disclosure form on government desk, bureaucratic paper, institutional requirement",
        "Defense sector stock portfolio display, gains visible as rising curves, institutional screen",
        "Capitol building dome interior from below, architectural grandeur, governance space",
        "Welder's sparks reflected in safety visor, blue-collar labor while markets profit, human contrast",
    ],
    8: [  # THE $1.7 BILLION DOOR - later: Tether stablecoins, sanctions circumvention, blockchain architecture
        "Server farm corridor stretching deep, blinking status indicators, crypto infrastructure",
        "Blockchain transaction visualization, node connections, digital financial architecture",
        "Tehran bazaar at dusk, merchant closing shop, ordinary commerce alongside hidden flows",
        "Cryptocurrency exchange server rack, cooling fans visible, digital financial plumbing",
        "Dubai financial district at night, modern towers lit, Gulf commercial hub",
        "Fiber optic cable junction box, technological infrastructure detail, data pathway",
        "Iranian rial exchange counter in Tehran market, currency under pressure, economic reality",
        "Stablecoin transaction node map glowing on dark monitor, digital financial network",
        "UAE port at dawn, dhow boats alongside modern cargo ships, traditional and modern commerce",
    ],
    9: [  # THE WAR CHEST - later: hawala networks, UAE front companies, shadow banking
        "Dubai commercial district at dawn, modern glass towers alongside traditional market, dual economy",
        "Hawala money transfer office, sparse interior, informal financial architecture",
        "UAE port facility with Iranian-flagged vessels, commercial interface between sanctions",
        "Real estate development in Dubai, construction cranes, value storage infrastructure",
        "Shadow banking ledger, handwritten figures, informal financial record-keeping",
        "Oil storage facility in Gulf state, unmarked tanks, commercial opacity",
        "Chinese refinery complex at night, flames from distillation towers, energy processing scale",
    ],
    10: [  # THE PARDON - later: regulatory gap, crypto vs legislation speed, institutional irony
        "DOJ headquarters at dusk, institutional facade, enforcement architecture",
        "Cryptocurrency mining facility, rows of machines, computational financial infrastructure",
        "Regulatory code book thick on institutional desk, legislation struggling to keep pace",
        "Federal courthouse entrance at dawn, columns and marble, justice architecture",
        "Binary data stream visualization on analyst monitor, digital speed versus regulatory pace",
        "Congressional hearing room, witness chair empty, accountability architecture",
        "Crypto exchange dashboard scrolling transactions, speed of digital finance",
        "Treasury Department exterior at golden hour, neoclassical authority, institutional weight",
        "Server room power supply, blinking indicators, always-on financial infrastructure",
        "Federal register documents stacked on desk, bureaucratic pace visualized, institutional delay",
        "Abandoned compliance office, empty desk with dormant monitors, enforcement gap",
        "Blockchain explorer display showing transaction flow, digital evidence trail on screen",
        "International wire transfer processing center, institutional banking infrastructure",
        "Newspaper front page layout visible through press room window, investigative journalism",
    ],
    11: [  # THE SECOND PAYMASTER - later: left hand vs right hand, DOD and financial system in opposition
        "Pentagon and Wall Street split composition, institutional duality, cold blue lighting",
        "Military logistics aircraft departing while commercial cargo arrives, parallel operations",
        "Two gas stations facing each other across intersection, competing economic forces",
        "Government wire transfer terminal beside crypto exchange server, institutional contrast",
        "Military supply convoy passing civilian fuel truck on shared highway, parallel economies",
    ],
    12: [  # THE PRICE AT THE PUMP DECIDES - later: Russian crude transaction, sanctions lifted, billion to Russia
        "Russian oil tanker at sea in rough weather, loaded hull riding low, dark water",
        "OFAC office interior, institutional desk with compliance paperwork, regulatory architecture",
        "Gas pump display frozen at price point, macro detail, consumer impact crystallized",
        "Russian port facility at dawn, oil loading infrastructure, commerce under sanction pressure",
        "White House exterior at dusk, institutional power, decision architecture",
        "Spring break highway traffic from above, family vehicles, domestic travel, fuel demand",
        "Oil barrel stack in storage facility, commodity at rest, strategic asset",
        "European ally's foreign ministry building exterior, institutional architecture, alliance strain",
        "Family minivan at gas station, vacation luggage visible through windows, domestic economics",
        "Port authority dock with tanker manifest paperwork, institutional commerce detail",
        "Crude oil flowing through transparent pipeline section, commodity in transit, warm amber",
        "State Department briefing room, empty podium, institutional communication space",
    ],
    13: [  # THE FRACTURE - later: "treated alliance like vendor contract", European diplomatic fallout
        "EU headquarters flag display, institutional architecture, alliance symbolism, cold morning light",
        "NATO conference table, empty chairs with country placards, diplomatic vacancy",
        "European foreign ministry corridor at dawn, institutional architecture, marble and formality",
        "German Bundestag exterior, institutional facade, European political architecture",
        "Diplomatic cable being sealed in envelope, institutional communication, macro detail",
        "Baltic state government building, small-nation institutional architecture, vulnerability",
        "European city newsstand at dawn, morning editions waiting, information distribution",
        "Embassy gate at dusk, institutional barrier, diplomatic boundary, security architecture",
        "Transatlantic fiber optic cable emerging from ocean, infrastructure of alliance communication",
    ],
    14: [  # THE POLITICAL ARITHMETIC - later: "who is the decision-maker optimizing for?"
        "Gas pump handle being squeezed by working-class hand, daily decision point",
        "White House situation room table, institutional furniture, decision architecture empty",
        "Split composition: suburban gas station and military command center, parallel realities",
        "Congressional voting gallery, institutional architecture, democratic mechanism",
    ],
    15: [  # THE BISHOPS AVENUE - later: Hilton Frankfurt, Mallorca, Dubai villa, Toronto penthouse
        "North London mansion behind stone wall, ornate iron gate, wealth concealment architecture",
        "Five-star hotel lobby, marble and chandelier, European luxury hospitality, institutional opulence",
    ],
    16: [  # THE PIPELINE - later: shell companies, 5 jurisdictions, zero single beneficial owner
        "Isle of Man corporate registry building, institutional architecture, offshore jurisdiction",
        "Caribbean tax haven office, modest exterior, palm trees, shell company registration",
        "Abu Dhabi bank tower at dusk, modern glass facade, Gulf financial infrastructure",
        "Corporate formation documents on solicitor's desk, institutional paperwork, shell structure",
        "UAE free trade zone office block, commercial registration, jurisdictional gateway",
        "Property deed document, macro detail on parchment, ownership chain evidence",
        "Five-jurisdiction network map on analyst wall, connected nodes, institutional investigation",
        "Saint Kitts and Nevis government building, Caribbean institutional architecture, offshore hub",
        "London property solicitor's office, Mayfair window, institutional real estate commerce",
        "Shell company registration stamp, macro detail, corporate formation machinery",
        "Global financial center skyline at dusk, interconnected towers, systemic architecture",
        "Accountant's ledger open to offshore entity page, institutional financial record",
        "Dubai Marina towers at night, vertical wealth storage, architectural extravagance",
    ],
    17: [  # THE IMMUNITY - later: networks faster than regulators, shell companies cheaper than lawyers
        "Mansion exterior behind security wall, curtains drawn, wealth hidden in plain sight",
        "Legal document shredder detail, paper entering teeth, evidence management",
        "Financial investigator's desk covered in jurisdiction maps and corporate trees",
        "Open padlock lying on stone surface, chain attached to nothing, security theater",
        "International law firm nameplate on brass, institutional legal infrastructure",
        "Offshore banking terminal, generic interface, financial anonymity architecture",
        "Supreme Leader's compound wall from outside, institutional barrier, untouchable wealth",
        "Anti-money laundering compliance screen, institutional software, regulatory technology",
        "Luxury property behind high hedge, aerial barely revealing roofline, concealment architecture",
        "Customs checkpoint at port, cargo containers passing through, enforcement limitation",
        "Corporate registration certificate, ornate border, institutional legitimacy machinery",
    ],
    18: [  # THE WHITE HOUSE MEETING - later: defense stocks surging, CEOs arrived wealthy, left wealthier
        "White House West Wing corridor, institutional architecture, power pathway, dawn light",
        "Defense stock chart climbing sharply on institutional display, green against dark background",
        "Lockheed Martin corporate campus aerial, institutional sprawl, defense industry headquarters",
        "Executive motorcade vehicles parked at White House, institutional arrival, power convening",
        "Northrop Grumman facility from above, defense manufacturing scale, institutional production",
        "RTX corporate tower reflecting sunset, defense industry architecture, glass and steel",
        "iShares defense ETF performance curve on analyst screen, sector outperformance visible",
        "White House gate at dawn, institutional security, power boundary architecture",
        "Defense industry boardroom, empty after meeting, institutional furniture, aftermath",
        "Military industrial production line, precision assembly, institutional manufacturing",
        "Stock portfolio display showing defense holdings, all positions in green, institutional gain",
    ],
    19: [  # THE MAN WHO SIGNS THE CHECKS - later: misaligned incentives, procurement conflict
        "Defense Acquisition Board hearing room, institutional desk arrangement, governance space",
        "Cerberus Capital Management office building exterior, private equity architecture",
        "DynCorp military contractor facility, operational compound, private military infrastructure",
        "Procurement contract signature page, institutional paperwork, authorization machinery",
        "Pentagon procurement corridor, institutional interior, bureaucratic pathway",
        "Private equity fund prospectus on executive desk, financial instrument, institutional detail",
        "Military contractor vehicle fleet parked in compound, operational assets, scale",
        "DOD budget spreadsheet on analyst dual monitors, institutional financial planning",
        "Revolving door metaphor: glass door spinning slowly in institutional lobby, distorted reflection",
        "Defense industry conference hall, empty before event, institutional networking space",
        "Pentagon exterior ring corridor, institutional architecture, decision pathway",
        "Military procurement warehouse, inventory shelves, supply chain governance point",
        "Executive elevator ascending in glass shaft, institutional vertical, power ascent",
        "Congressional defense budget markup hearing room, institutional governance architecture",
        "Private equity portfolio visualization on screen, diversified defense holdings displayed",
        "Government ethics office, institutional desk, compliance architecture",
    ],
    20: [  # THE PORTFOLIO AND THE COMMITTEE - later: measurable gains to decision-makers, 45-day filings
        "Congressional committee chamber from above, curved desk arrangement, institutional governance",
        "Financial disclosure filing cabinet, institutional records, forty-five-day paper trail",
        "Defense ETF performance chart on congressional aide's monitor, institutional data",
        "Senate office building corridor at dawn, institutional architecture, governance pathway",
        "Committee hearing room from witness perspective, empty elevated desk, institutional power",
        "Lockheed stock chart overlaid on calendar dates, institutional timeline correlation",
        "Congressional parking garage, luxury vehicles, institutional wealth proximity",
    ],
    21: [  # THE BUYBACK IRONY - later: government now paying emergency premiums for capacity shortfall
        "Defense factory floor operating at surge capacity, workers on overtime, production pressure",
        "Stock buyback transaction confirmation on corporate terminal, institutional financial action",
        "Empty production line that could have been expanded, industrial potential unrealized",
        "Emergency defense procurement order on government desk, institutional urgency paperwork",
        "Shareholder dividend check being printed, corporate financial distribution, macro detail",
        "THAAD production facility bottleneck, limited assembly stations, capacity constraint visible",
        "Corporate executive compensation report on institutional desk, financial reward document",
        "Military supply requisition form marked urgent, institutional logistics, operational need",
        "Defense factory expansion site, undeveloped land beside existing plant, missed investment",
        "Investor relations presentation slide on screen, financial engineering metrics displayed",
        "Government supplemental appropriation document, emergency budget mechanism, institutional",
    ],
    22: [  # THE SPR GAMBLE - later: refill cost 20 billion, future taxpayers, "not mentioned in press conference"
        "SPR underground cavern with lower oil level, industrial interior, strategic reserve depleted",
        "Gulf Coast salt dome facility pump running, drawdown in progress, institutional energy",
        "Future budget projection on government analyst screen, deferred cost growing, institutional data",
        "Oil barrel price indicator at hundred-dollar mark, commodity cost for refill, institutional display",
        "SPR control room operator monitoring drawdown gauges, institutional energy management",
        "Spring break highway from above, family vehicles, temporary consumer relief in motion",
        "White House press briefing room, empty podium, institutional communication, cost unmentioned",
        "Treasury bond auction interface, government borrowing mechanism, institutional finance",
        "Salt cavern cross-section revealing declining reserves, geological storage, institutional asset",
        "Future taxpayer: young person at gas station, inheriting deferred cost, generational burden",
    ],
    23: [  # THE MOST EXPENSIVE WATERWAY - later: insurance surge, supply chain disruption, margin compression
        "Strait of Hormuz aerial, narrow waterway between coastlines, geographic chokepoint",
        "Oil tanker navigating narrow channel, coastline visible on both sides, strategic vulnerability",
        "Lloyd's of London insurance market interior, institutional underwriting architecture",
        "Container ship bridge, navigation instruments, crew monitoring passage, operational tension",
        "Shipping insurance certificate on broker desk, institutional risk documentation",
        "LNG tanker in Persian Gulf at dawn, specialized cargo, energy security vessel",
        "Satellite view of Gulf waterway with vessel traffic, strategic monitoring perspective",
        "Ship engine room, massive machinery operating, maritime power infrastructure",
        "Port congestion from above, vessels waiting to transit, logistical bottleneck",
        "Pharmaceutical supply chain warehouse, partially empty shelves, disruption evidence",
        "Automotive parts container being unloaded, just-in-time supply chain fragility",
        "Electronics assembly line with component shortage gap, manufacturing disruption",
        "Grocery store shelf with gaps, consumer-facing supply chain impact, subtle scarcity",
        "Shipping broker's desk with rate charts climbing, institutional maritime commerce",
        "Fertilizer storage facility at port, agricultural supply chain node, strategic commodity",
        "Hormuz coastline at sunset, strategic geography, oil infrastructure visible on shore",
        "Marine fuel bunker facility, ships waiting for passage, maritime logistics chokepoint",
    ],
    24: [  # GOLDMAN'S CALL - later: family absorbing cost, 1 in 4 recession, transmitted from Hormuz
        "Goldman Sachs research report on analyst desk, institutional economic outlook, recession data",
        "Family kitchen at evening, bills on table, absorbing gas price increase, warm domestic light",
        "Consumer price chart climbing on institutional display, inflation transmission visible",
        "Credit card statement on kitchen counter, monthly balance not declining, domestic pressure",
        "Grocery store checkout total rising, consumer POV, everyday inflation impact",
        "Strait of Hormuz to American suburb: aerial of coastline transitioning to aerial of suburb",
        "Small business calculator and ledger, margin compression visible in figures, warm light",
        "Federal Reserve building at dusk, institutional monetary authority, economic governance",
        "Gas station at dusk, family finishing fueling, economic transmission endpoint",
        "Empty restaurant at lunch hour, small business impact, economic uncertainty visible",
    ],
    25: [  # CHINA'S BUFFER - later: European re-exposure, sulfur prices, fertilizer, food supply chain
        "Chinese strategic petroleum reserve facility aerial, massive tank farm, national preparedness",
        "Asian rice paddy at dawn, agricultural production, food supply chain origin",
        "Sulfur processing plant, yellow industrial material, fertilizer feedstock production",
        "Fertilizer warehouse, bags stacked high, agricultural supply chain infrastructure",
        "South Korean LNG terminal, storage tanks near capacity limit, energy vulnerability",
        "European gas pipeline valve station, institutional energy infrastructure, re-exposure risk",
        "Asian farmer's hands holding soil, agricultural foundation, food security at human scale",
        "Chinese refinery complex at dawn, operational capacity, energy processing independence",
        "Global shipping lane aerial, tankers in transit, energy trade route vulnerability",
        "European utility company control room, energy supply monitoring, institutional management",
        "Wheat field at golden hour, agricultural output, food production dependent on energy",
        "LNG carrier approaching Asian port, energy security delivery, strategic logistics",
        "Chemical fertilizer production facility interior, industrial agriculture dependency",
        "Container port in Southeast Asia, global supply chain node, interconnected commerce",
    ],
    26: [  # THE LEDGER CLOSES - later: all the affected people, "who pays, who profits" summary
        "Hedge fund trading desk at dawn, positions being reviewed, profit-takers at work",
        "Tanker ETF performance curve on institutional display, gains from conflict measured",
        "Defense CEO's executive office, morning light on corporate success, institutional wealth",
        "Suburban family at breakfast table, morning routine, unknowing participants in war economy",
        "South Korean apartment window looking out at winter, energy vulnerability, domestic moment",
        "Asian farming community at dawn, planting season beginning, fertilizer cost absorbed",
        "American gas station at sunrise, first customer of the day, transmission endpoint",
        "SPR facility at dawn, depleted caverns, national debt deferred, institutional consequence",
        "Pentagon corridor at dawn, institutional machinery continuing, operational persistence",
        "Small business owner opening shop at dawn, economic survival, human resilience",
        "European parliament at dawn, alliance framework under strain, institutional morning",
        "Goldman Sachs tower catching first light, recession probability calculated, institutional forecast",
        "Ocean horizon at dawn, tanker silhouette barely visible, global scale indifferent to individuals",
        "Empty courtroom at dawn light, accountability pending, institutional justice waiting",
    ],
}


def get_scene_lighting(scene_number):
    """Get consistent lighting for each scene based on existing clips."""
    if scene_number in existing_by_scene and existing_by_scene[scene_number]:
        last_clip = existing_by_scene[scene_number][-1]
        prompt = last_clip.get('prompt', '')
        if 'cold blue-white institutional' in prompt:
            return 'cold blue-white institutional lighting'
        elif 'warm amber' in prompt:
            return 'warm amber light, lived-in textures'
        elif 'dramatic cinematic' in prompt:
            return 'dramatic cinematic lighting, high contrast'
    return 'dramatic cinematic lighting, high contrast'


def build_prompt(visual_desc, lighting, camera, palette):
    """Build a complete LTX-2.3 prompt within 150-350 character limits."""
    palette_str = ", ".join(palette)
    
    # Try full prompt first
    prompt = f"{visual_desc}, {camera}. {lighting}. {BASE_STYLE}, subtle film grain, color palette: {palette_str}"
    
    if len(prompt) > 350:
        prompt = f"{visual_desc}, {camera}. {lighting}. {BASE_STYLE}, color palette: {palette_str}"
    
    if len(prompt) > 350:
        prompt = f"{visual_desc}. {lighting}. {BASE_STYLE}, color palette: {palette_str}"
    
    if len(prompt) > 350:
        # Shorten visual description
        visual_short = visual_desc[:120].rsplit(',', 1)[0]
        prompt = f"{visual_short}. {lighting}. {BASE_STYLE}, color palette: {palette_str}"
    
    if len(prompt) < 150:
        prompt = f"{visual_desc}, {camera}, atmospheric depth. {lighting}. {BASE_STYLE}, anamorphic lens flare, subtle film grain, color palette: {palette_str}"
    
    return prompt


# Banned words that imply on-screen text
BANNED_WORDS = ['text', 'letter', 'word', 'subtitle', 'logo', 'number', 'caption',
                'title', 'headline', 'label', 'writing', 'signage', 'banner',
                'placard', 'poster with', 'inscription']

def clean_prompt(prompt):
    """Remove any references to on-screen text, numbers, letters."""
    for word in BANNED_WORDS:
        prompt = re.sub(rf'\b{word}s?\b', '', prompt, flags=re.IGNORECASE)
    prompt = re.sub(r'\s{2,}', ' ', prompt).strip()
    # Fix double commas or comma-period
    prompt = re.sub(r',\s*,', ',', prompt)
    prompt = re.sub(r',\s*\.', '.', prompt)
    return prompt


def generate_fill_clips():
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
        lighting = get_scene_lighting(scene_num)
        
        # Get available visuals for this scene
        if scene_num in SCENE_VISUALS:
            available_visuals = SCENE_VISUALS[scene_num]
        else:
            # Fallback generic
            available_visuals = [
                "Wide establishing shot of institutional building at dawn, cold grey architecture",
                "Close-up of hands on desk surface, institutional lighting, deliberate stillness",
                "Aerial view of infrastructure at dawn, roads and structures, morning atmosphere",
                "Institutional corridor stretching deep, overhead fluorescent, geometric perspective",
                "Industrial landscape at dusk, infrastructure silhouetted against amber horizon",
                "Government building exterior at blue hour, institutional authority, monumental scale",
                "Empty conference table reflecting overhead lights, institutional meeting space",
                "Suburban landscape from above, residential patterns, morning light and shadows",
                "Document detail on polished desk, institutional paperwork, morning light through blinds",
                "Highway stretching to vanishing point at dawn, empty infrastructure, vast distance",
                "Water surface reflecting overcast sky, natural stillness contrasting human urgency",
                "Power transmission infrastructure at sunset, utility lines crossing landscape",
            ]
        
        scene_clips_generated = 0
        
        for fill_idx in range(new_clips_needed):
            clip_index = existing_count + fill_idx
            
            narr_context = get_later_narration_context(
                scene_num, existing_count, fill_idx, new_clips_needed
            )
            
            # Cycle through visuals ensuring we use all available before repeating
            visual_idx = fill_idx % len(available_visuals)
            visual_desc = available_visuals[visual_idx]
            
            # Varied camera work - different for each clip
            camera_idx = (fill_idx * 7 + scene_num * 3) % len(CAMERA_WORKS)
            camera = CAMERA_WORKS[camera_idx]
            
            prompt = build_prompt(visual_desc, lighting, camera, palette)
            prompt = clean_prompt(prompt)
            
            clip_id = f"scene_{scene_num:02d}_fill{fill_idx:02d}"
            
            fill_clips.append({
                "clip_id": clip_id,
                "scene_number": scene_num,
                "scene_title": scene_title,
                "clip_index": clip_index,
                "target_duration_sec": 5,
                "ltx_clips_needed": 1,
                "prompt": prompt,
                "narration_context": narr_context if narr_context else f"Scene {scene_num}: {scene_title}"
            })
            
            scene_clips_generated += 1
        
        summary[f"Scene {scene_num}: {scene_title}"] = scene_clips_generated
    
    return fill_clips, summary


# Generate
fill_clips, summary = generate_fill_clips()

# ========== VALIDATION ==========
print("=" * 70)
print("FILL CLIPS GENERATION SUMMARY")
print("=" * 70)
print(f"\nTotal fill clips generated: {len(fill_clips)}")
print(f"\nPer-scene breakdown:")
total_expected = sum(p['new_clips_needed'] for p in plan)
for scene_name, count in summary.items():
    print(f"  {scene_name}: {count} clips")
print(f"\nExpected total: {total_expected}")

# Length validation
too_short = [c for c in fill_clips if len(c['prompt']) < 150]
too_long = [c for c in fill_clips if len(c['prompt']) > 350]
print(f"\nPrompts under 150 chars: {len(too_short)}")
if too_short:
    for c in too_short[:5]:
        print(f"  {c['clip_id']}: {len(c['prompt'])} chars - {c['prompt'][:80]}...")
print(f"Prompts over 350 chars: {len(too_long)}")
if too_long:
    for c in too_long[:5]:
        print(f"  {c['clip_id']}: {len(c['prompt'])} chars - {c['prompt'][:80]}...")

# Uniqueness
prompts_set = set(c['prompt'] for c in fill_clips)
print(f"\nUnique prompts: {len(prompts_set)} / {len(fill_clips)}")
if len(prompts_set) < len(fill_clips):
    from collections import Counter
    counts = Counter(c['prompt'] for c in fill_clips)
    dupes = {k: v for k, v in counts.items() if v > 1}
    print(f"  Duplicates found: {len(dupes)}")
    for p, cnt in list(dupes.items())[:3]:
        print(f"    [{cnt}x] {p[:80]}...")

# Text reference check
text_words = ['text', 'letter', 'word', 'subtitle', 'logo', 'sign ', 'label', 'banner',
              'headline', 'caption', 'placard', 'poster', 'inscription', 'writing']
text_refs = []
for c in fill_clips:
    pl = c['prompt'].lower()
    for tw in text_words:
        if tw in pl:
            text_refs.append((c['clip_id'], tw, c['prompt'][:80]))
            break
print(f"Prompts with potential text references: {len(text_refs)}")
for clip_id, word, snippet in text_refs[:5]:
    print(f"  {clip_id}: found '{word}' in: {snippet}...")

# Average length
avg_len = sum(len(c['prompt']) for c in fill_clips) / len(fill_clips) if fill_clips else 0
min_len = min(len(c['prompt']) for c in fill_clips) if fill_clips else 0
max_len = max(len(c['prompt']) for c in fill_clips) if fill_clips else 0
print(f"\nPrompt length stats: min={min_len}, avg={avg_len:.0f}, max={max_len}")

# Sample prompts
print("\n" + "=" * 70)
print("SAMPLE PROMPTS (first clip from each of first 5 scenes)")
print("=" * 70)
shown = set()
for c in fill_clips:
    if c['scene_number'] not in shown:
        print(f"\n[{c['clip_id']}] Scene {c['scene_number']}: {c['scene_title']}")
        print(f"  Prompt ({len(c['prompt'])} chars): {c['prompt']}")
        print(f"  Narration: {c['narration_context'][:120]}...")
        shown.add(c['scene_number'])
        if len(shown) >= 5:
            break

# Save
with open('/home/user/workspace/iran-war-doc/production/fill_clips_final.json', 'w') as f:
    json.dump(fill_clips, f, indent=2)

print(f"\n{'=' * 70}")
print(f"Output saved to fill_clips_final.json ({len(fill_clips)} clips)")
print(f"{'=' * 70}")
