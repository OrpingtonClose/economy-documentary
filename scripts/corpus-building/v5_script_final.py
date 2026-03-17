#!/usr/bin/env python3
"""
Final additions to reach 330+ clips
"""
import json

with open('/home/user/workspace/v5_script.json', 'r') as f:
    script = json.load(f)

segments = script['segments']

# Count current clips
total_current = sum(len(seg['clips']) for seg in segments)
print(f"Current clips: {total_current}")

counter = [total_current]

def clip(frames, narration, prompt):
    counter[0] += 1
    return {
        "id": f"clip{counter[0]:03d}",
        "frames": frames,
        "narration": narration.strip(),
        "prompt": prompt.strip()
    }

# ============================================================
# FINAL ADDITIONS - Interstitial and deepening clips
# Targeting 330 total (need 23 more)
# ============================================================

# Add 4 to Act I
act1_final = [
    clip(129, "The first financial markets to open after the Sunday night strikes were the Asian exchanges. Tokyo fell four point two percent. Hong Kong fell three point eight. Seoul fell five point one. The risk-off cascade was immediate, global, and severe.",
         "Aerial drone shot over the Tokyo financial district at dawn, the city waking up to a new market day, the lights of the trading buildings visible, the first reaction of Asian markets to the overnight news"),
    clip(193, "The dollar — as it always does in acute geopolitical crisis — strengthened. The yen, historically a safe haven, rallied. Emerging market currencies sold off hard. The Malaysian ringgit, the Indonesian rupiah, the Thai baht — currencies of economies deeply exposed to the Hormuz supply chain — fell two to four percent overnight.",
         "Close-up of currency exchange boards in an airport showing rapid rate changes, travelers pausing to read the boards, the real-time transmission of geopolitical events into the exchange rates that touched every traveler's pocket"),
    clip(129, "The crypto market, operating twenty-four hours a day seven days a week, had already processed the news. Bitcoin had dropped from seventy-four thousand to sixty-eight thousand in the hours after the strikes were confirmed. Ethereum fell twelve percent. The speculative assets that priced liquidity premium had immediately repriced.",
         "Cinematic aerial shot of a city's entertainment district at 3am, lights still on, people still moving, the perpetual wakefulness of the market that never closes, the night economy mirroring the global crypto market's continuous operation"),
    clip(193, "And then the commodity markets opened in London. Brent crude gapped twelve dollars higher at the open. Gold gapped two hundred dollars higher. Silver moved eight percent in the first ninety minutes of trading. The physical economy and the financial economy were simultaneously repricing the same event from their different perspectives.",
         "Wide shot of the London Metal Exchange trading floor at morning opening, traders at their positions, the physical commodity markets opening, the visual of commodity price discovery in its most concentrated form"),
]

# Add 4 to Act II
act2_final = [
    clip(129, "The shipping data from Kpler and Vortexa — the major maritime analytics firms that tracked global tanker movements — confirmed the 94% traffic reduction with granular detail. Of the four ships that had transited in the first seventy-two hours, two were already in mid-transit when the closure was declared. They were not new voyages. They were journeys that could not be stopped.",
         "Satellite view aesthetic shot of an ocean surface from very high altitude, the curvature of the earth barely visible, the vastness of the sea making the shipping lanes invisible, the monitoring perspective of the analytics firms"),
    clip(193, "The insurance market's response to the Hormuz closure was almost instantaneous. Lloyd's of London activated its Joint War Committee framework, adding the Strait of Hormuz to the list of areas where war risk surcharges applied. The surcharges — typically a fraction of a percent of vessel value — had jumped to two to three percent of hull value per voyage.",
         "Wide shot of Lloyd's of London underwriting room, the famous Lutine Bell visible, the syndicates at their boxes, the centuries-old institution of maritime insurance meeting a twenty-first-century energy crisis"),
    clip(129, "For a VLCC — a very large crude carrier worth approximately one hundred million dollars — a two percent war risk surcharge was two million dollars per voyage, on top of the already-elevated charter rate. The economics of a Gulf voyage had changed permanently, at least for the duration of the closure.",
         "Dramatic aerial shot of a VLCC tanker at sea, the vast scale of the vessel from altitude, the length of over three hundred meters visible, the economic value of the ship made comprehensible by the comparison to vessels around it"),
    clip(193, "The strategic petroleum reserve releases announced by the United States, Europe, and Japan in the third day of the crisis provided temporary psychological support to markets. But strategic reserves are finite. The combined reserve releases were calibrated to cover approximately thirty days of the disrupted supply. If the closure lasted ninety days — Martin Wolf's threshold — the reserves would be irrelevant.",
         "Wide aerial shot of a strategic petroleum reserve facility, the large cylindrical storage tanks in rows, the visual of the emergency buffer that governments maintained for exactly this kind of scenario, the question of whether it was adequate"),
]

# Add 4 to Act III
act3_final = [
    clip(129, "The SEC and the FCA — the American and British financial regulators — issued unusual joint statements acknowledging that they were monitoring the private credit market situation closely. In regulatory language, 'monitoring closely' means the situation has crossed a threshold of concern. It is the institutional equivalent of a yellow light.",
         "Wide shot of a regulatory agency building exterior, the official architecture of financial oversight, the organization that set the rules under which the crisis had developed, the regulator arriving at a scene it had been warned about"),
    clip(193, "The systemic risk designation — the formal label that triggers the most intensive regulatory scrutiny and, potentially, the backstop of public funds — had never been applied to a private credit fund. The reason: private credit funds were supposed to be isolated from the public financial system. The events of March 2026 were demonstrating that the isolation was theoretical rather than actual.",
         "Cinematic close-up of a safety certification plaque on a piece of industrial equipment, the official stamp of approval, the formal declaration of safety that was now being re-examined in light of the actual failure mode"),
    clip(129, "The private credit crisis was also a technology crisis in one specific sense: the systems that pension funds and endowments used to track their alternative investments had never been designed to handle simultaneous gating across multiple funds. The operational infrastructure of the institutional investment world was struggling to process the crisis in real time.",
         "Wide shot of a pension fund operations center, staff at workstations, multiple screens showing portfolio data, the practical infrastructure of managing retirement assets in a crisis, the human organization under pressure"),
    clip(193, "The fund-of-funds structure that characterized much of the private credit market added layers of opacity that made the situation worse. An endowment that invested in a fund-of-funds that invested in a private credit fund had no direct visibility into the underlying loans. They knew their fund was gating. They did not know precisely why or what the underlying exposure looked like.",
         "Cinematic shot of Russian dolls being opened to reveal progressively smaller dolls inside, each nested within the last, the visual metaphor of the fund-of-funds structure and its nesting of opacity within opacity"),
]

# Add 4 to Act IV
act4_final = [
    clip(129, "The consumer confidence data that arrived in the second week of March — collected before the war began, published after — showed a reading that was already weak. The University of Michigan Consumer Sentiment Index had fallen to sixty-two, below even the COVID panic lows. When the war's impact was folded in, economists expected the March reading to be lower still.",
         "Wide shot of a suburban mall food court with many tables empty that would normally be full, the visual of reduced consumer activity, the human-scale evidence of falling confidence in the everyday commercial spaces"),
    clip(193, "The corporate earnings season, which would begin in April, was already being pre-gamed by analysts. Energy companies would report extraordinary results. Airlines, trucking companies, and chemical manufacturers would report severe margin compression. Consumer discretionary companies would report demand destruction. The K-shaped economy would be legible in every quarterly earnings call.",
         "Wide shot of a corporate headquarters boardroom where an earnings presentation is being rehearsed, executives reviewing projected slides, the preparation for the formal disclosure of what the crisis had done to their business"),
    clip(129, "The Fed's dot plot — the projection of future interest rates that each FOMC member submits quarterly — was due for revision at the March meeting. The median projection had been pointing to three rate cuts in 2025 and two in 2026. Those projections were now inoperative. The crisis had reset the rate path in ways that no dot plot could currently capture.",
         "Cinematic close-up of a printed dot plot chart on a desk, the projected trajectory of interest rates now clearly outdated, a hand drawing a question mark beside the current projection, the formal structure of monetary communication rendered temporarily meaningless"),
    clip(193, "The Soar Financially interview with Dr. Sri-Kumar offered one specific channel of crisis escalation that had not yet materialized but was worth watching: the emerging market sovereign debt crisis. Countries that had borrowed in dollars, were paying for oil in dollars, and were now watching both the dollar strengthen and the oil price rise were facing a triple squeeze that could produce cascading defaults.",
         "Wide aerial shot of a developing nation's financial district, the smaller towers of an emerging market capital city, the less resilient financial infrastructure that was more exposed to the compound pressures from the global crisis"),
]

# Add 4 to Act V
act5_final = [
    clip(129, "The inflow into gold ETFs in the first week of March had been extraordinary. The GLD and IAU — the two largest US gold ETFs — had collectively seen inflows of over five billion dollars in five trading days. The institutional rotation was real, measurable, and continuing. This was not retail fear-buying. It was allocation.",
         "Close-up of a computer terminal showing ETF flow data, the green numbers indicating inflows, a financial analyst's hand scrolling through the data, the quantification of the institutional rotation that was driving the gold price"),
    clip(193, "The royalty companies — Franco-Nevada, Wheaton Precious Metals, Royal Gold — were particularly interesting from an investor standpoint. They provided gold and silver price exposure without the operational risks of mining. With silver at ninety dollars, the royalty streams on existing contracts were generating free cash flow at extraordinary rates.",
         "Aerial drone shot over a working gold mine with a royalty company's banner visible, the industrial extraction in full operation, the visual of the business model where revenue comes without the operational complexity, the financial structure of the royalty model"),
    clip(129, "The Bitcoin fear and greed index — the sentiment indicator that aggregated social media, volatility, and market momentum data — had fallen to eighteen during the first days of the war. Extreme fear. In the history of Bitcoin, extreme fear readings had consistently marked periods that subsequent analysis identified as excellent buying opportunities. The question was always timing.",
         "Close-up of an old analog fear gauge or emotion indicator, the needle in the extreme negative zone, the visual of maximum fear at the potential turning point, the contrarian investor's reference instrument"),
    clip(193, "The on-chain Bitcoin data that Benjamin Cowen analyzed told a specific story: long-term holders — addresses that had held Bitcoin for more than a year without moving it — were not selling. Their holdings had remained essentially unchanged through the market decline from peak. Short-term holders were the ones capitulating. The structural demand for Bitcoin as a long-term store of value was intact.",
         "Cinematic slow aerial shot over a mountain range, the peaks unchanged and permanent, snow on the high ridges, the valley below with changing weather, the permanent geological formation above the temporary meteorological event"),
]

# Add 4 to Act VI
act6_final = [
    clip(129, "The WTI-Brent spread — which reflected the premium that seaborne crude commanded over land-locked American crude — had widened dramatically. WTI, which was landlocked in the Permian Basin and had alternative routes to the Gulf Coast, traded at a significant discount to Brent. The spread was the market's real-time measure of the Hormuz premium.",
         "Cinematic wide shot of pipeline infrastructure in the American interior, the silver pipes carrying oil toward the coast, the physical routing of American crude to the global market, the premium of maritime access captured in the steel"),
    clip(193, "Venezuela, under US sanctions but still producing approximately eight hundred thousand barrels per day, had quietly been approached by the State Department about potential emergency production increases. The geopolitical calculus was brutal: in an energy emergency, the United States was considering suspending sanctions on Venezuela for the same reasons it had loosened them on Russia.",
         "Aerial drone shot over Venezuelan oil infrastructure, the aging production facilities, the oil derricks visible in the landscape, the untapped potential of a sanctioned nation whose resources the global market suddenly needed"),
    clip(129, "The Iran-Russia relationship — which had been deepening since the Ukraine war, based on shared interest in undermining US sanctions architecture — was now evolving again. Russia had sold Iran advanced air defense systems. Iran had provided Russia with drones for use in Ukraine. Now the oil price windfall from the war was enriching both parties simultaneously.",
         "Wide aerial shot of a border crossing between two large nations, trucks and vehicles in queue, the physical infrastructure of the strategic partnership, the trade relationship between sanctioned powers"),
    clip(193, "The long-term consequence of the March 2026 crisis for the global energy architecture was perhaps the most important story that current events could not fully tell. The crisis had demonstrated, definitively, that the energy transition could not move fast enough to prevent oil shocks from damaging the global economy. The transition needed to accelerate. The crisis was providing the financial incentive.",
         "Wide aerial shot at sunrise over a landscape showing both traditional oil infrastructure and new renewable energy installations, the coexistence of the old and new energy systems in geographic proximity, the transition in progress"),
]

# Add 3 to Act VII
act7_final = [
    clip(129, "The documentary has told a story of compound crisis. But it should also tell the story of compound resilience. The American economy has survived every previous shock. The global trading system has survived every previous disruption. Human ingenuity has, repeatedly, found ways to route around the obstacles that history places in its path.",
         "Wide aerial drone shot over a busy urban intersection at dusk, the organized flow of traffic and pedestrians and commerce continuing, the city as a system of voluntary cooperation and mutual benefit continuing despite everything"),
    clip(257, "The question for investors, for governments, and for citizens in March 2026 was not whether the crisis would resolve. It was what kind of world would exist after it resolved. Would the energy transition accelerate, making future supply disruptions less catastrophic? Would the private credit market develop genuine transparency? Would the Federal Reserve's independence be preserved through the political pressure that would follow?",
         "Sweeping final aerial drone shot starting close over a family home at dusk, then pulling back to reveal the neighborhood, the city, the landscape, the continent, the scale of the world that was navigating the crisis together, the final moment of the documentary framing the human scale of the systemic questions"),
    clip(193, "These questions would be answered in the months and years ahead. In the meantime, the world remained on fire. The war continued. The Hormuz remained effectively closed. The private credit funds were gated. The Federal Reserve was watching. And the gold price was rising, as it always rises, when paper systems lose the confidence of the people who depend on them.",
         "Final slow aerial drone shot over the Persian Gulf at night, the oil platforms lit in the dark water, distant coastlines glowing, a military vessel visible, the crisis at its epicenter, still burning, still unresolved, the world watching and waiting"),
]

# Apply additions
additions = {
    "Act I: Cold Open — The Iran War Begins": act1_final,
    "Act II: Oil Shock — Hormuz Dark, Brent at $115": act2_final,
    "Act III: Private Credit Crisis — $300 Billion Contagion": act3_final,
    "Act IV: The Fed's Impossible Bind — Stagflation Returns": act4_final,
    "Act V: Safe Havens — Gold $5,200, Silver $90, Bitcoin as Smoke Alarm": act5_final,
    "Act VI: Geopolitical Chess — Russia Wins, China Adapts, Europe Fractures": act6_final,
    "Act VII: The Reckoning — Synthesis and What Lies Ahead": act7_final,
}

for seg in segments:
    act_name = seg['act']
    if act_name in additions:
        seg['clips'].extend(additions[act_name])

# Renumber all clips sequentially
global_counter = 0
for seg in segments:
    for c in seg['clips']:
        global_counter += 1
        c['id'] = f"clip{global_counter:03d}"

# Write final file
with open('/home/user/workspace/v5_script.json', 'w', encoding='utf-8') as f:
    json.dump(script, f, indent=2, ensure_ascii=False)

# Final statistics
total_clips = sum(len(seg['clips']) for seg in segments)
total_narration_words = sum(
    len(c['narration'].split())
    for seg in segments
    for c in seg['clips']
)
duration_minutes = total_narration_words / 135

print(f"\n{'='*60}")
print(f"FINAL DOCUMENTARY SCRIPT SUMMARY")
print(f"{'='*60}")
print(f"Title: {script['title']}")
print(f"\nTotal clips: {total_clips}")
print(f"Total narration words: {total_narration_words:,}")
print(f"Estimated duration at 135 WPM: {duration_minutes:.1f} minutes")
print(f"\nClips per act:")
for seg in segments:
    act_words = sum(len(c['narration'].split()) for c in seg['clips'])
    act_duration = act_words / 135
    print(f"  {seg['act']}")
    print(f"    → {len(seg['clips'])} clips | {act_words:,} words | {act_duration:.1f} min")

print(f"\nFrame distribution:")
frame_counts = {}
for seg in segments:
    for c in seg['clips']:
        f = c['frames']
        frame_counts[f] = frame_counts.get(f, 0) + 1
for frames, count in sorted(frame_counts.items()):
    duration_s = frames / 24
    print(f"  {frames} frames ({duration_s:.1f}s at 24fps): {count} clips")

total_frame_duration = sum(c['frames'] for seg in segments for c in seg['clips'])
print(f"\nTotal video duration from frames: {total_frame_duration/24/60:.1f} minutes")

import os
size = os.path.getsize('/home/user/workspace/v5_script.json')
print(f"File size: {size/1024:.1f} KB")
print(f"\nFile saved: /home/user/workspace/v5_script.json")

# Validation
print(f"\n{'='*60}")
print(f"VALIDATION")
print(f"{'='*60}")
print(f"Clips >= 330: {'PASS' if total_clips >= 330 else 'FAIL'} ({total_clips})")
print(f"Words >= 13500: {'PASS' if total_narration_words >= 13500 else 'FAIL'} ({total_narration_words:,})")
print(f"All clip IDs sequential: ", end='')
ids = [c['id'] for seg in segments for c in seg['clips']]
expected = [f"clip{i:03d}" for i in range(1, total_clips+1)]
print('PASS' if ids == expected else 'FAIL')
