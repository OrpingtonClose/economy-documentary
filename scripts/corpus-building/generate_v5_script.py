import json

# ============================================================
# V5 DOCUMENTARY SCRIPT GENERATOR
# "WARFLATION: The Global Economic Crisis of March 2026"
# Target: 330+ clips, ~13,500 words narration, 7 acts
# ============================================================

script = {
    "title": "WARFLATION: The Global Economic Crisis of March 2026",
    "description": (
        "On March 1, 2026, US and Israeli forces launched strikes on Iran. Within 48 hours, the Strait of Hormuz — "
        "the world's most critical oil chokepoint — fell effectively silent. Ship traffic collapsed 94%, from 138 vessels per day to just four. "
        "Brent crude surged past $115 a barrel. Then $120. LNG prices spiked 137% in five days. QatarEnergy declared force majeure. "
        "At the same moment, a private credit crisis that had been building for months finally broke into the open: BlackRock's $26 billion fund, "
        "Morgan Stanley's $8 billion North Haven fund, Cliffwater's $33 billion vehicle — all blocking redemptions. "
        "Analysts warned of $300 billion in contagion risk. The Federal Reserve faced its worst dilemma in fifty years: "
        "rising inflation it could not fight without crushing an economy already in recession for the bottom 90%. "
        "This is the story of the 13 days that shook the global financial system — told through the data, the analysts, "
        "and the voices of millions of ordinary investors who watched it unfold in real time.\n\n"
        "Featuring: Martin Wolf (Financial Times) • Joseph Stiglitz (Nobel Laureate) • Luke Gromen • Charles Gave (Gavekal) • "
        "Ronald Stoeferle (Incrementum) • Arthur Hayes • Steve Keen • Dr. Komal Sri-Kumar • Mark Thornton (Mises Institute) • "
        "Steve Hanke (Johns Hopkins) • Ed Yardeni • Jeremy Schwartz (WisdomTree)\n\n"
        "Chapters:\n"
        "00:00 Cold Open — The Day the Hormuz Went Silent\n"
        "08:30 Act I — The Iran War Begins\n"
        "18:00 Act II — Oil Shock: $115 Brent and the LNG Crisis\n"
        "28:00 Act III — Private Credit Collapse\n"
        "38:00 Act IV — The Fed's Impossible Bind\n"
        "47:00 Act V — Safe Havens: Gold, Silver, Bitcoin\n"
        "55:00 Act VI — Geopolitical Chess\n"
        "01:02:00 Act VII — What Comes Next"
    ),
    "negative_prompt": (
        "blurry, low quality, distorted, text, watermark, logo, subtitles, words, letters, numbers, UI overlay, "
        "cartoon, anime, illustration, painting, drawing, screen with text, monitor with data"
    ),
    "segments": []
}

clip_counter = [1]

def make_clip(narration, prompt, frames=193):
    n = clip_counter[0]
    clip_counter[0] += 1
    return {
        "id": f"clip{n:03d}",
        "frames": frames,
        "narration": narration,
        "prompt": prompt
    }

# ============================================================
# COLD OPEN
# ============================================================

cold_open_clips = [
    make_clip(
        "March 1, 2026. Before dawn broke over the Persian Gulf, the world changed. The first missile strikes hit Iranian territory at 3:47 AM local time — a coordinated assault launched jointly by the United States and Israel.",
        "Slow aerial drone shot over dark desert landscape, pre-dawn darkness broken by distant horizon glow, subtle orange light spreading across vast emptiness, camera drifting forward at low altitude over sand dunes",
        257
    ),
    make_clip(
        "Within the hour, Ayatollah Khamenei, Iran's Supreme Leader for over three decades, was killed. Senior commanders of the Islamic Revolutionary Guard Corps perished alongside him.",
        "Extreme close-up of an oil lamp flame flickering in a dark room, camera pulling back slowly to reveal ancient stone architecture, shadows dancing on carved walls, complete stillness before sudden wind snuffs the flame",
        257
    ),
    make_clip(
        "By sunrise, the Strait of Hormuz — the narrow waterway between Iran and Oman through which twenty percent of the world's entire oil supply flows every single day — had gone almost completely silent.",
        "Aerial wide shot from high altitude over vast open ocean at golden hour, no ships visible on the horizon, water surface rippling gently in morning light, camera slowly panning to reveal empty sea stretching to infinity",
        257
    ),
    make_clip(
        "Ship traffic through the Strait, which had averaged 138 vessels per day, collapsed almost overnight to just four. A ninety-four percent reduction. In the span of 72 hours, the global energy system entered a state it had never experienced in the modern era.",
        "Bird's-eye drone shot over a wide shipping channel at low tide, camera locked on a single small vessel creeping across the frame while the surrounding water is completely empty, vast industrial port infrastructure visible but idle in background",
        257
    ),
    make_clip(
        "This is the story of those thirteen days. The days that shook the world's financial system to its foundations. The days that gave a name to a new kind of economic catastrophe — warflation.",
        "Cinematic time-lapse of a calm harbor at night, still water reflecting city lights perfectly, camera mounted low to the water surface, slight fog rolling across the frame from left to right, total silence in the scene",
        257
    ),
    make_clip(
        "Warflation. War plus inflation. A collision of forces that most economists said could not happen simultaneously — and yet here it was, unfolding in real time, on screens and in spreadsheets and at gas pumps across the Western world.",
        "Slow dolly shot along an empty highway at dusk, cars have stopped, headlights on but stationary, camera drifting forward through the gridlock, golden light fading from the sky ahead, silence and stillness everywhere",
        257
    ),
    make_clip(
        "As the Financial Times chief economics commentator Martin Wolf would say just days after the strikes began — when asked what he thought about the war — he didn't know why it had happened, didn't know what the objectives were, didn't know how it would end.",
        "Wide establishing shot of a grand newspaper building at twilight, stone facade illuminated by warm street lamps, single figure with briefcase walking through revolving door, camera slowly pushing in on the entrance",
        193
    ),
    make_clip(
        "But one thing, Wolf said, he did know: 'If one wanted to think of a nightmare disruption scenario for the world economy, it would be a war in the Gulf.'",
        "Close-up of an antique globe spinning slowly on a dark wooden desk, camera focus pulling from the globe surface to a window behind it where storm clouds are gathering on the horizon, natural light fading dramatically",
        193
    ),
]

script["segments"].append({
    "act": "COLD OPEN: The Day the Hormuz Went Silent",
    "theme": "iran_war",
    "clips": cold_open_clips
})

# ============================================================
# ACT I — THE IRAN WAR BEGINS
# ============================================================

act1_clips = [
    make_clip(
        "To understand what March 2026 meant for the global economy, you have to understand what the Strait of Hormuz means to the world. A waterway barely twenty-one miles wide at its narrowest point.",
        "Aerial drone shot tracking slowly along a narrow dark waterway between two rocky coastlines, camera flying low and forward along the channel's center, dramatic cliffs on both sides catching the last rays of sunlight",
        193
    ),
    make_clip(
        "Every single day, in normal times, approximately seventeen to twenty million barrels of crude oil pass through those twenty-one miles. That is roughly one-fifth of all the oil consumed on the planet.",
        "Extreme close-up of crude oil pouring steadily from a wide pipe into a dark industrial reservoir, camera hovering just above the surface, iridescent rainbow patterns swirling slowly as the oil pools below",
        193
    ),
    make_clip(
        "Qatar sends nearly all of its liquefied natural gas through the Strait. Saudi Arabia moves most of its oil through it. Iraq, Kuwait, the UAE — their economic survival depends on those twenty-one miles remaining open.",
        "Wide aerial shot over a massive LNG tanker moving through a narrow channel, sun gleaming off silver storage tanks on deck, flat sea extending in all directions, industrial coastline barely visible on the horizon",
        257
    ),
    make_clip(
        "The strikes on Iran on March first were not spontaneous. They had been in preparation for months, coordinated between Washington and Tel Aviv at the highest levels. The targeting was surgical — or was meant to be.",
        "Slow cinematic dolly shot through an empty government building corridor at night, shadows cast long by emergency lighting, large empty desks and chairs visible through open doors, camera moving forward purposefully",
        193
    ),
    make_clip(
        "Within the first hour, US and Israeli precision strikes took out Iranian air defenses and command structures. Ayatollah Khamenei and the senior IRGC command were killed. It was, by any measure, an extraordinarily rapid decapitation of the Iranian state.",
        "Low-angle shot of military aircraft contrails crossing a dawn sky, multiple white lines dissecting the blue expanse, camera slowly rotating upward to follow the trails as they fade into high altitude clouds",
        193
    ),
    make_clip(
        "Ben Rhodes, former Deputy National Security Adviser, appeared on the Ezra Klein Show within days of the strikes. He called it 'The Great Lie of War.' He said Americans had been sold a story about quick, clean military action with clear objectives.",
        "Close-up of a person's hands turning the pages of a printed document slowly at a wooden desk, late afternoon light streaming through a window, dust motes visible in the air, focus pulling to show dense text on the pages",
        193
    ),
    make_clip(
        "The reality was messier. Iran did not collapse. The new Supreme Leader issued a statement immediately. Iran's strategy became clear: keep the regime standing, attack Gulf allies, and drive oil prices up to create domestic political pressure on Washington.",
        "Wide shot of oil derricks silhouetted against a blood-red sunset sky, three or four pump jacks nodding slowly in the foreground, smoke rising from a distant refinery on the horizon, heat shimmer visible in the air",
        193
    ),
    make_clip(
        "Ed Yardeni, the veteran Wall Street strategist, described his initial reaction: he had expected the strikes to be decisive. The decapitation of the regime suggested, he said, that Washington and Tel Aviv 'had everything under control, everything fully planned.'",
        "Wide establishing shot of a research library at night through large windows, warm interior light spilling out, single figure visible at a large desk surrounded by open books, camera slowly pushing toward the glass",
        193
    ),
    make_clip(
        "Then Tuesday came, Yardeni said. And the fog of war descended. 'No matter how well planned,' he noted — drawing on a truth as old as warfare itself — 'it doesn't necessarily go well.'",
        "Slow push shot along a quiet residential street at early morning, thick fog reducing visibility to a hundred yards, streetlights creating halos in the mist, camera moving steadily forward through the grey light",
        193
    ),
    make_clip(
        "What nobody had fully anticipated was the Iranian proxy network's response. Across the Gulf, Iranian-aligned forces began targeting oil infrastructure. Tankers sitting in the Persian Gulf waiting for clearance to transit the Strait became sitting targets.",
        "Aerial drone shot circling slowly around a massive oil tanker sitting completely still in glassy flat water, golden late-afternoon light casting dramatic shadows from the hull, camera rising as it circles to reveal total isolation",
        257
    ),
    make_clip(
        "By day three, tanker charter rates had already spiked dramatically. Rates that had been running at $130,000 per day leapt to $400,000. The math was simple and brutal: every dollar increase in charter costs was a dollar that would eventually show up at the pump.",
        "Close-up of a ship captain's weathered hands gripping a large steel wheel in a darkened navigation room, emergency red lights creating a tense atmosphere, blurred ocean visible through the forward windows",
        193
    ),
    make_clip(
        "On Capitol Hill, debates raged about what the objectives actually were. Ezra Klein invited a former Trump official to justify the war — a conversation that exposed the disturbing reality: there was no clear endgame, no defined exit, no articulated theory of victory.",
        "Wide shot of an empty hearing room with rows of empty chairs facing a raised wooden dais, late afternoon sunlight streaming through tall windows, abandoned water glasses and papers on the long tables",
        193
    ),
    make_clip(
        "A commenter on a financial YouTube channel, watching events unfold in real time, put it simply: 'Going to war as an economy collapses. Third time?' The resonance with 2003 and 2008 was palpable across social media.",
        "Close-up of a person's face reflected in a laptop screen in a darkened room, the blue light of the screen illuminating worried features, fingers hovering motionless over the keyboard, camera slowly pulling back to reveal more darkness around them",
        129
    ),
    make_clip(
        "What made this war different from every Middle East conflict since the 1970s was the convergence of crises it triggered simultaneously. Not just an oil shock. Not just a credit stress. Not just a geopolitical realignment. All three, at once, at the worst possible moment.",
        "Cinematic split composition: left half shows oil tankers at sea, right half shows empty trading floor screens, camera slowly pulling back to reveal the full frame as both halves fade to black simultaneously",
        257
    ),
    make_clip(
        "Professor Steve Hanke of Johns Hopkins warned of what he called a fifty-year crisis. One observer on the David Lin channel captured the mood perfectly: 'Oil at 108. If we hit 140, 2008 will look like a picnic.'",
        "Low-angle wide shot looking up at a massive oil refinery at dusk, towers of pipes and tanks rising into a purple sky, steam venting from multiple outlets, orange flame of a flare stack burning at the top of the frame",
        193
    ),
    make_clip(
        "The shock had begun. And by week two of March, thirteen days in, it was becoming clear: this was not going to be a twelve-day war. This was something else entirely.",
        "Slow aerial tracking shot over a sprawling industrial port at night, crane lights reflecting in dark water below, vast empty container yards stretching to the horizon, the scene suggesting arrested motion — a world paused",
        257
    ),
]

script["segments"].append({
    "act": "Act I: The Iran War Begins",
    "theme": "iran_war",
    "clips": act1_clips
})

# ============================================================
# ACT II — OIL SHOCK
# ============================================================

act2_clips = [
    make_clip(
        "The price of Brent crude oil on the morning of March first, before the strikes, was approximately $82 per barrel. Within 48 hours, it crossed $100. By day six, it reached $118 and was still climbing.",
        "Cinematic close-up of crude oil dripping from a rusted valve fitting into a dark pool below, camera locked in extreme close-up, each drop sending ripples across the inky black surface, industrial background completely blurred",
        257
    ),
    make_clip(
        "Maggie Lake, the veteran market journalist, asked the question that was on everyone's mind: Is one hundred dollars per barrel the new normal? The answer, she suggested, might be the least dramatic possibility on the table.",
        "Wide establishing shot of a modern glass-fronted financial media studio building at night, a single broadcast light visible through a window on an upper floor, camera slowly pushing in, the rest of the city dark and still",
        193
    ),
    make_clip(
        "One viewer watching a financial live stream on March eighth posted: 'Watching this on Mar 8, 2026. Oil price at $118.8.' Just a number. But numbers like that have a way of restructuring everything downstream.",
        "Extreme close-up of a digital price display on a gas station sign, camera rack-focused to the price digits themselves, background completely blurred, warm afternoon light hitting the metal signage",
        129
    ),
    make_clip(
        "Polymarket, the prediction market platform, saw twenty-five million dollars in trading volume on a single contract: the crude oil price by end of March. The market was pricing a four percent probability that oil would reach two hundred dollars per barrel.",
        "Wide aerial drone shot of a crossroads in a major city at dusk, traffic moving in all four directions simultaneously, camera slowly rising above the intersection as rush hour builds, city lights beginning to illuminate below",
        193
    ),
    make_clip(
        "A four percent probability sounds small. But in options markets, a four percent probability on a catastrophic scenario demands enormous hedging. It was a signal that serious institutional money was buying protection against something genuinely extreme.",
        "Close-up of weathered hands counting gold coins on a dark wooden desk in a dimly lit room, natural window light casting long shadows, each coin placed deliberately in a growing pile, camera fixed and composed",
        193
    ),
    make_clip(
        "The mechanics of an oil shock through the Strait of Hormuz are not complicated. When ships cannot transit safely, they either wait or reroute. Waiting costs money. Rerouting — around the Cape of Good Hope — adds two to three weeks to every journey.",
        "Aerial drone shot tracking slowly over a queue of massive oil tankers anchored in open ocean, stretching toward the horizon, camera flying low and forward at sunrise, golden light catching the orange-painted hulls",
        257
    ),
    make_clip(
        "According to data analyzed by the financial channel Projekt 100X, ship traffic through the Strait fell from 138 vessels per day before the war to just four — a collapse of 94 percent in less than a week.",
        "Bird's-eye drone shot over a normally busy shipping lane, completely empty dark water stretching from edge to edge of the frame, camera hovering still before slowly tilting to reveal an equally empty horizon in every direction",
        257
    ),
    make_clip(
        "Those four remaining ships represented — to the extent any ships were transiting at all — vessels operating under private military escort, carrying emergency government cargoes. Commercial shipping had effectively halted.",
        "Slow low-angle shot of a single large cargo ship moving through calm dark waters at dusk, powerful bow wave pushing water aside, camera mounted very low and wide, dramatic clouds building on the horizon ahead of the vessel",
        193
    ),
    make_clip(
        "The LNG crisis was, in some ways, even more alarming than the crude oil disruption. Qatar is the world's largest exporter of liquefied natural gas. And Qatar sits on the Persian Gulf. With the Strait effectively closed, approximately twenty percent of the world's entire LNG supply went offline.",
        "Wide aerial shot over a massive LNG terminal at night, rows of silver storage spheres gleaming under industrial floodlights, loading arms frozen at their berths, camera slowly descending toward the eerily still facility",
        257
    ),
    make_clip(
        "Within five days of the Hormuz closure, global LNG spot prices had risen 137 percent. QatarEnergy declared force majeure on its LNG export contracts — the first time in the company's history.",
        "Close-up of vapor rising from industrial pipes against a cold dark sky, white clouds billowing upward in the night air, orange industrial lighting from below illuminating the steam dramatically, camera tilting slowly upward",
        193
    ),
    make_clip(
        "Force majeure is a legal term meaning circumstances beyond control. When major energy companies invoke it, it means they are telling their customers: we cannot supply you, and we are not liable for the consequences. It is, in contractual terms, an admission of systemic breakdown.",
        "Wide shot of empty loading docks at a major port facility, cranes stationary and lights dimmed, camera drifting slowly forward along the idle quayside, massive ship mooring chains hanging untensioned from bollards",
        193
    ),
    make_clip(
        "Joe Blogs, the UK-based financial journalist, reported that force majeure declarations were spreading across the energy sector. QatarEnergy. Shell. Other major suppliers. 'When companies start invoking force majeure,' he noted, 'that's a clear sign the situation has become extremely serious.'",
        "Cinematic establishing shot of a major European energy port in overcast weather, grey sky reflected in still harbor water, cranes and industrial structures silhouetted against the pale light, total stillness pervading the scene",
        193
    ),
    make_clip(
        "For Europe, which had already spent two years rebuilding gas reserves after the Russian supply disruption of 2022 and 2023, this was a nightmare scenario. The continent had worked desperately to diversify away from Russian gas, with Qatar as the cornerstone of that diversification.",
        "Slow tracking shot through a dense forest of industrial pipes and valves in an LNG regasification plant, camera moving through the labyrinthine infrastructure at eye level, cold grey light filtering through overhead structures",
        193
    ),
    make_clip(
        "Now, with Qatar's supply interrupted and no alternative at scale available on short notice, European storage levels — though not yet critical — began drawing down faster than seasonal models predicted. Energy traders across the continent began the grim calculation of a cold spring with limited supply.",
        "Wide aerial drone shot of a modern European city at dusk, streets and buildings lit but the camera angle revealing rooftop solar panels and wind turbines in the distance, a metaphor for energy vulnerability despite investment",
        193
    ),
    make_clip(
        "The food price angle was perhaps the least discussed but most globally consequential dimension of the Hormuz crisis. The Strait is not just an oil route. Fertilizer ingredients, agricultural chemicals, food commodities — they all move through those twenty-one miles.",
        "Slow dolly shot through a large supermarket aisle at eye level, shelves well-stocked but camera lingering on price tags, gradually revealing rising numbers, fluorescent lighting humming overhead, the mundane rendered quietly ominous",
        193
    ),
    make_clip(
        "Market Insider interviewed a professor of oil economics who captured the volatility: 'The other day when I went to sleep, the price of Brent was $119 a barrel. When I woke up, it was $87 eight hours later. Then back up to $93.' He had never seen volatility like it.",
        "Extreme close-up of liquid in a transparent container, camera watching the surface level drop then rise, drop then rise — the simple physics of liquid in a vessel transformed into metaphor for oil market chaos",
        193
    ),
    make_clip(
        "Meanwhile, the United States took the extraordinary step of easing sanctions on Russian oil exports in an attempt to stabilize energy markets. Joe Blogs observed the significance: when Washington starts allowing Russian oil into global markets in the middle of a crisis, policymakers are running out of options.",
        "Wide aerial shot of oil tankers flying Russian maritime flags anchored in open ocean, camera flying high and steady, dramatic clouds and ocean stretching to the horizon, suggesting geopolitical paradox made physical",
        257
    ),
    make_clip(
        "Charles Gave, the French economist and co-founder of Gavekal Research, had been thinking about oil and geopolitics longer than almost anyone on Wall Street. His analysis, presented through the Soar Financially channel, was characteristically contrarian.",
        "Close-up of an old wall map being unrolled and pinned flat on a large table, weathered paper showing oil routes and geographic features, hands smoothing the creases, warm lamplight illuminating the faded colors",
        193
    ),
    make_clip(
        "Gave's argument was structural: the oil shock was not just an energy event. It was an accelerant for a geopolitical realignment that had been underway for years — a reshaping of the relationship between Washington, Beijing, and the energy-producing world.",
        "Aerial drone shot slowly orbiting a massive junction of pipelines in a desert landscape, endless flat terrain in all directions, a single worker figure visible near one of the pipe junctions for scale, dramatic late afternoon shadows",
        193
    ),
    make_clip(
        "China, Gave pointed out, was significantly less exposed to a Hormuz disruption than most Western analysts assumed. Beijing had spent a decade building overland pipeline connections — through Central Asia, through Russia, through Myanmar — that bypassed the Strait entirely.",
        "Wide aerial tracking shot following a pipeline route through mountainous terrain, the steel structure snaking across the rocky landscape below, camera flying high and steady, vast geography suggesting strategic depth",
        257
    ),
    make_clip(
        "While American consumers faced gas prices rising fifty cents per gallon in a week, while European industry scrambled for LNG alternatives, China was importing Russian and Central Asian oil through pipelines that the war could not touch.",
        "Slow cinematic shot of a fuel pump being inserted into a car at a gas station, camera close on the transaction, station price sign blurred but visible in background, the mundane act of refueling rendered significant",
        129
    ),
    make_clip(
        "The oil shock was, in this sense, a geopolitical sorting mechanism. It revealed, in the starkest possible terms, which economies had built strategic energy resilience — and which remained dangerously exposed.",
        "Wide slow-motion shot of an oil refinery at night, the complex of lights and flares and columns creating an otherworldly landscape, camera on a long lens picking out individual flame stacks against the dark sky",
        193
    ),
]

script["segments"].append({
    "act": "Act II: Oil Shock — $115 Brent and the LNG Crisis",
    "theme": "oil_shock",
    "clips": act2_clips
})

# ============================================================
# ACT III — PRIVATE CREDIT CRISIS
# ============================================================

act3_clips = [
    make_clip(
        "But the oil shock, as severe as it was, was not the only crisis unfolding in those thirteen days. Running in parallel, and in many ways more dangerous in its systemic implications, was a collapse in the private credit market.",
        "Slow dolly shot through an empty trading floor at night, abandoned desks still showing active screens glowing green and red, papers scattered as if everyone left in a hurry, fluorescent overhead lights casting cold light on the scene",
        257
    ),
    make_clip(
        "Private credit is not an obscure corner of finance. It is a $1.7 trillion market — larger than the entire US high-yield bond market. It emerged from the rubble of the 2008 financial crisis as an alternative to bank lending, promising investors higher yields in exchange for accepting illiquidity.",
        "Wide aerial shot of a modern glass skyscraper reflecting clouds, camera slowly pushing in toward the building's facade, the glass surface acting as a mirror showing distorted sky and neighboring buildings",
        193
    ),
    make_clip(
        "The promise of private credit was seductive: double-digit returns, lower volatility than public markets, exposure to the real economy rather than speculative financial assets. Pension funds, endowments, insurance companies poured money in. The market grew from $400 billion in 2010 to nearly $2 trillion by 2025.",
        "Close-up of neat stacks of financial documents in manila folders lined up in a filing cabinet drawer, camera slowly pulling back to reveal dozens of identical drawers in an enormous filing room, the systematic accumulation of capital",
        193
    ),
    make_clip(
        "What the growth obscured was a structural vulnerability. Private credit funds are, by design, illiquid. They cannot easily sell their loans if markets become stressed. They can, however, suspend redemptions — tell investors who want their money back that they will have to wait.",
        "Slow tracking shot along a row of closed and locked heavy bank vault doors, camera moving at eye level along the corridor, warm yellow light from sconces between each vault, the solidity of the doors conveying impenetrability",
        257
    ),
    make_clip(
        "When Stoic Finance published its analysis of the unfolding private credit crisis, the headline figure was stark: three hundred billion dollars in contagion risk. Not a theoretical worst case. A realistic assessment of what would happen if redemption freezes cascaded through the system.",
        "Wide establishing shot of a major financial district at dusk, towers of glass and steel reflecting the dying light, camera positioned to emphasize height and scale, tiny cars and people visible on the streets far below",
        193
    ),
    make_clip(
        "The cascade began with BlackRock. On March ninth, BlackRock suspended redemptions from its twenty-six billion dollar private credit fund. The announcement was made with the language of orderly management — 'protecting long-term investor interests' — but the market read it clearly: the exits were closing.",
        "Close-up of a heavy wooden door being shut slowly, camera tracking the movement of the door as it closes, the diminishing gap of light through the doorway the only visual, a soft click as the latch engages in darkness",
        193
    ),
    make_clip(
        "BlackRock. The world's largest asset manager. The company whose CEO, Larry Fink, had spent years positioning as the responsible, forward-thinking face of institutional finance. When BlackRock suspends redemptions, it is not a small story.",
        "Wide low-angle shot of the BlackRock corporate headquarters building in New York, camera looking up at the glass and steel facade, grey overcast sky visible above, wind moving American and corporate flags at the entrance",
        193
    ),
    make_clip(
        "The Eurodollar University channel, run by analyst Jeff Snider, noted the eerie historical echo: in 2007 and 2008, Bear Stearns had suspended two of its mortgage funds before the wider crisis erupted. The pattern of fund suspensions as leading indicator of systemic stress was a known phenomenon.",
        "Slow dolly shot through a financial archive, rows of old annual reports and filing cabinets, camera moving through the space at a measured pace, a single pool of lamplight at a reading desk in the background",
        193
    ),
    make_clip(
        "But BlackRock was only the beginning. Snider reported: Morgan Stanley's eight billion dollar North Haven Private Income fund was next. Massive withdrawal requests denied. Then Cliffwater — thirty-three billion dollars.",
        "Close-up of water dripping steadily from a leaking pipe fitting into a growing puddle on a concrete floor, camera fixed on the drip, the sound implied, the accumulation inevitable and unstoppable",
        129
    ),
    make_clip(
        "Then Blackstone's twenty-one billion dollar vehicle. Blue Owl. One after another, the largest private credit funds in the world were telling their investors the same thing: you cannot have your money right now.",
        "Wide shot of a queue of people waiting outside a bank branch, stretching down the street out of frame, camera positioned across the street to show the full length of the line, overcast sky above, everyone standing quietly",
        257
    ),
    make_clip(
        "Eurodollar University's Jeff Snider drew the 2008 parallel explicitly. He noted that even after Bear Stearns, most policymakers had been more worried about the oil shock of 2007 and 2008 than the credit crisis building beneath the surface. They had, he said, 'got it completely and totally backward.'",
        "Slow push shot toward a large framed historical photograph on a wall showing a bank queue from the Great Depression, camera gradually closing in until the faces of the waiting people fill the frame, the past and present merging",
        193
    ),
    make_clip(
        "The private credit crisis was not simply an investment problem. It was a liquidity problem. The money in those funds came, ultimately, from Main Street — from pension contributions, from retirement savings, from insurance premium reserves. As Ken McElroy's podcast explained clearly: all money comes from Main Street.",
        "Wide aerial shot of an ordinary suburban neighborhood at dusk, identical houses and cars, the camera rising steadily to show the vast scale of the residential landscape, the aggregate of millions of individual households",
        193
    ),
    make_clip(
        "When private credit funds suspend redemptions, the immediate victims are the institutions — the pension funds, the endowments — who cannot get their allocated capital back. But the second-order victims are the beneficiaries of those institutions: the retired teachers, the hospital workers, the university staff.",
        "Close-up of an older person's hands holding a small pension statement, camera slowly zooming in on the document, the numbers on the page becoming the entire frame before blurring out of focus",
        193
    ),
    make_clip(
        "The UK dimension of the private credit crisis was particularly acute. Stoic Finance's video — 'Private Credit Collapses British Economy As Contagion Spreads Globally' — documented what was happening in London.",
        "Wide establishing shot of the City of London financial district on a grey rainy morning, modern glass towers rising above the Victorian architecture, the Thames visible in the background, commuters moving quickly under umbrellas",
        193
    ),
    make_clip(
        "Market Financial Solutions, a UK mortgage lender with more than two billion pounds of borrowings from Barclays and Santander, collapsed amid accusations of widespread fraud. It was, Stoic Finance argued, a symptom of a system that had incentivized opacity and mispriced risk for years.",
        "Slow low-angle shot looking up at the exterior of a boarded-up commercial building, camera tilting from street level to the empty upper windows, weeds pushing through cracked pavement at the building's base",
        193
    ),
    make_clip(
        "The contagion was spreading in both directions across the Atlantic. UK firms failing created stress at American private credit lenders who had co-invested. American fund suspensions created withdrawal pressure at UK pension funds. The system was discovering, painfully, just how interconnected it had become.",
        "Wide aerial drone shot over the Atlantic Ocean at sunset, camera positioned to show both coastlines receding toward the horizon on either side, the vast ocean between them lit in deep orange, the geography of interconnection",
        257
    ),
    make_clip(
        "A commenter on the Stoic Finance channel — watching this unfold in real time — captured the fury of ordinary investors: 'Private profits but public losses. Liars, cheaters and thieves. Not a single one will go to jail. And we the taxpayer get to fund it all.' The sentiment was almost universally shared.",
        "Close-up of hands typing rapidly at a keyboard in a dimly lit home office, camera positioned over the shoulder slightly, the bright screen reflected in the person's glasses, the room dark except for the screen glow",
        129
    ),
    make_clip(
        "Adam Taggart at Thoughtful Money brought in analyst Chris Irons to discuss what he called 'a private credit meltdown threat.' Irons was not given to hyperbole. His assessment was clinical and deeply unsettling.",
        "Wide interior shot of a podcast studio with two chairs facing each other, microphones between them, one chair empty, camera slowly pushing toward the occupied chair where an unseen interviewer waits",
        193
    ),
    make_clip(
        "The core problem, Irons argued, was not just illiquidity. It was valuation opacity. Private credit loans are not marked to market daily the way public bonds are. Their value is assessed periodically, by the same asset managers who stand to benefit from high valuations.",
        "Slow tracking shot along a wall of framed certificates and licenses in a corporate office, camera moving left to right along the wall, each credential suggesting legitimacy, the uniformity becoming slightly ominous",
        193
    ),
    make_clip(
        "In an oil shock environment, with corporate revenues under pressure and debt service costs rising, the underlying collateral supporting those loans was deteriorating. But the reported valuations had not yet caught up with reality. The gap between stated value and true value was widening by the day.",
        "Close-up of a cracked sidewalk with weeds growing through the fissures, camera positioned at ground level, slow zoom on the largest crack, a metaphor for structures that appear stable while fundamental damage accumulates beneath",
        193
    ),
    make_clip(
        "The Bankless podcast's weekly rollup for the second week of March framed it perfectly: markets had entered what it called the chaotic era. Three things were driving the chaos simultaneously: oil, jobs, and private credit. And how all three affected crypto. But that is a story for later.",
        "Cinematic wide shot of a major city intersection at night from a high angle, multiple streams of traffic moving in different directions, their light trails crossing and weaving, the organized chaos of a system under stress",
        193
    ),
    make_clip(
        "For now, the combined weight of the oil shock and the private credit crisis was creating a feedback loop that threatened to accelerate into something far worse. And sitting at the center of that loop, with no good options available, was the Federal Reserve.",
        "Wide establishing shot of the Federal Reserve building in Washington DC at dusk, classical stone architecture lit from below, cars streaming past on Constitution Avenue with light trails in the long exposure, the flag rippling above",
        257
    ),
]

script["segments"].append({
    "act": "Act III: Private Credit Collapse — The $300 Billion Contagion",
    "theme": "private_credit_crisis",
    "clips": act3_clips
})

# ============================================================
# ACT IV — THE FED'S IMPOSSIBLE BIND
# ============================================================

act4_clips = [
    make_clip(
        "The stagflation trap. It is the one scenario that conventional monetary policy cannot address. When prices are rising and the economy is simultaneously contracting, the central bank's tools become not just ineffective but actively harmful.",
        "Slow aerial drone shot over a large industrial city in winter, factory chimneys emitting steam that blows sideways in strong wind, camera drifting above the scene at moderate altitude, grey sky, the industrial landscape reduced in scale",
        193
    ),
    make_clip(
        "If you raise interest rates to fight inflation, you choke off the credit that struggling businesses need to survive. You accelerate unemployment. You push an already-weakening economy into a harder recession.",
        "Close-up of a delicate scale balance, one side holding a small pile of coins, the other a small pile of sand, the scale tipping first one way then the other as camera watches, impossibility of equilibrium made visual",
        193
    ),
    make_clip(
        "If you cut rates to stimulate growth, you pour fuel on the inflationary fire. With oil already at $115 and rising, any additional monetary stimulus would drive the cost of everything from gasoline to food to fertilizer even higher.",
        "Wide shot of a vintage oil lamp with flame at full brightness, camera slowly pushing in on the flame, the heat shimmer above the wick creating distortion, the excess of light suggesting destructive overabundance",
        193
    ),
    make_clip(
        "Martin Wolf of the Financial Times, in his interview with the Monetary Matters Network, framed the dilemma with the precision of someone who had studied every major economic crisis of the past fifty years.",
        "Wide interior shot of a quiet library reading room with high ceilings and wooden paneling, warm lamp light on a single reading desk in the center, camera slowly pushing from the doorway toward the desk",
        193
    ),
    make_clip(
        "'If the straits were closed for three months or more,' Wolf said, 'we would be looking at a major jolt to the world economy.' And crucially: 'Completely dependent on fuels imported through very dangerous places — this has really underlined it.'",
        "Close-up of an old oil lamp flame burning inside a glass chimney, camera close and still, the contained fire casting warm light on surrounding darkness, fragility and power simultaneously suggested by the burning wick",
        193
    ),
    make_clip(
        "Wolf's broader concern was historical. The 1973 oil embargo, he noted, had triggered one of the most painful decades in modern American history. But in 1973, the US had no debt of consequence. It had manufacturing capacity. It had demographic momentum.",
        "Wide cinematic shot of an empty American main street in a Rust Belt town at dusk, storefronts alternately open and shuttered, a diner with lights on across from a vacant lot, camera drifting slowly down the center of the road",
        193
    ),
    make_clip(
        "The ITM Trading analysis of the 2026 situation put the contrast starkly: 'Today, the starting conditions are far worse than they were fifty years ago.' National debt had crossed forty trillion dollars. Annual interest payments on that debt had reached nearly one trillion.",
        "Aerial drone shot over a massive government complex at night, lit windows suggesting endless bureaucratic activity, camera rising slowly above the buildings to reveal the scale of federal Washington, the machinery of state laid out below",
        257
    ),
    make_clip(
        "The Monetary Matters Network brought in Joseph Stiglitz, the Nobel laureate economist. His interview — 'Economic Chaos Threatened By Middle East War' — was characteristically direct. Stiglitz called the situation a near-textbook stagflation scenario with one crucial complication.",
        "Wide establishing shot of a prestigious university building, stone steps leading up to imposing columns, autumn trees visible on either side, camera slow push from the street level up toward the entrance, the weight of intellectual authority suggested",
        193
    ),
    make_clip(
        "The complication Stiglitz identified was what economists call the K-shaped economy. The standard models of stagflation assumed a unified economy where higher prices hit everyone and tightening credit affected all borrowers similarly. But 2026 America was not a unified economy.",
        "Wide cinematic split-level shot from above a city street, camera positioned to emphasize the visible contrast between gleaming corporate towers and the street life below, luxury cars and delivery workers in the same frame",
        193
    ),
    make_clip(
        "Rosenberg Research had been documenting the K-shape for months before the war began. Their finding: the United States was already in recession — but only for the bottom ninety percent of the population by income. The top decile was experiencing something entirely different.",
        "Wide aerial shot of two neighborhoods side by side from the air, one with manicured lawns and swimming pools, the other with densely packed smaller homes, camera slowly pulling back to reveal both simultaneously",
        257
    ),
    make_clip(
        "For those in the top ten percent, the wealth effect from a decade of extraordinary asset price inflation had created a spending capacity essentially immune to oil price increases. Their portfolios, their real estate, their stock options — these had shielded them from the cost pressures hitting everyone else.",
        "Close-up of a gleaming new luxury car parked outside an expensive restaurant, late evening light, valets moving around in the background, camera slowly panning along the car's polished flank",
        129
    ),
    make_clip(
        "For the bottom ninety percent, the math was brutal. Wages had not kept pace with cumulative inflation since 2021. Credit card debt had reached record levels. Savings rates had collapsed. And now, gas was fifty cents a gallon more expensive than last week.",
        "Slow tracking shot along a suburban gas station at night, a single car refueling, the camera moving from the pump to the driver's face briefly visible through the windshield, worry visible in the reflected light",
        193
    ),
    make_clip(
        "The payroll data arriving in March made the picture worse. December's employment numbers had been revised down by 65,000 — to negative 17,000. January had been revised down further. By February, the employment picture was unambiguously deteriorating.",
        "Wide cinematic shot of a job center waiting room, rows of hard plastic chairs, many occupied by people with documents on their knees, the scene shot from the doorway, fluorescent lights overhead, a quiet desperation in the postures",
        193
    ),
    make_clip(
        "A commenter on the Eurodollar University channel spoke for millions: 'Economist finally admits to a recession. That's how you know the depression started.' Another: 'We've been in recession since 2022.' A third, simply: 'No jobs out there. 1929 moment is here.'",
        "Close-up of a person's hands holding a phone showing a news article, the screen brightness high in a darkened room, camera slowly pulling back to show the person sitting alone at a kitchen table, the rest of the room dark",
        129
    ),
    make_clip(
        "The Azul financial channel posted a video in early March titled 'You Are Not Ready For What Comes Next.' Its central data point: employer-announced layoffs through November 2025 had reached 1.2 million — a fifty-four percent increase from the same period the previous year.",
        "Wide aerial drone shot over a sprawling office park, large corporate buildings with half-empty parking lots, camera slowly circling one building, the gaps in the parking lot telling a story of workforce reduction",
        193
    ),
    make_clip(
        "Against this backdrop, the Federal Reserve sat in session, staring at their models, watching oil prices surge and unemployment rise and credit conditions tighten all at once. Their policy toolkit was designed for normal economic conditions. These were not normal conditions.",
        "Wide establishing shot of the Federal Reserve's Marriner Eccles building in Washington DC, neoclassical facade bathed in early morning light, a lone security guard visible at the entrance, the building imposing and still",
        193
    ),
    make_clip(
        "The historical precedent the Fed was most studying was 1973 and 1974. But as Jeff Snider pointed out at Eurodollar University, there was a better, more recent, more directly relevant parallel: 2007 and 2008.",
        "Slow dolly shot through a central bank archive, filing cabinets and bound reports in chronological order, camera moving past decades of documentation, pausing briefly on a cabinet labeled with years in the late 2000s",
        193
    ),
    make_clip(
        "In 2008, even after Bear Stearns had collapsed, even after the first signs of credit crisis were visible, major central banks had turned their attention to fighting inflation — which was driven almost entirely by an oil shock. The European Central Bank had even raised rates in July 2008.",
        "Wide shot of the European Central Bank headquarters in Frankfurt reflected in the River Main, grey overcast sky, bare winter trees on the riverbank, the glass and steel building floating in its own reflection",
        193
    ),
    make_clip(
        "'They got it completely and totally backward,' Snider said. 'One major central bank even went so far as to raise rates in July 2008. People forget. They don't want you to remember.' The warning was explicit: the same mistake was available to be made again.",
        "Close-up of a chalkboard where someone has written an equation, camera slowly pulling back to reveal the rest of the blackboard is covered in similar calculations, a forest of numbers and variables that suggests the overwhelming complexity",
        193
    ),
    make_clip(
        "Dr. Komal Sri-Kumar, the former chief global strategist at TCW Group, appeared on Soar Financially to discuss what he called 'the war economy.' He painted a scenario of stagflation hitting in 2026 with two distinct vectors: the supply shock from oil, and the demand destruction from credit tightening.",
        "Wide cinematic shot of an empty lecture hall, tiered seating descending toward a lectern at the front, afternoon light through high windows, a single chair at the front suggesting the authority of expertise about to speak",
        193
    ),
    make_clip(
        "Sri-Kumar's key insight was about timing. Even if the war ended tomorrow — even if the Strait reopened immediately — the inflationary effects of an oil shock at this magnitude would take twelve to eighteen months to fully work through the global supply chain.",
        "Wide aerial drone shot over a vast container ship terminal, thousands of shipping containers stacked in precisely organized rows, camera rising to reveal the full scale of the terminal, the geometry of global trade",
        193
    ),
    make_clip(
        "Fertilizer prices. Plastics feedstocks. Industrial chemicals. Shipping costs. Every link in the modern supply chain runs on energy, and energy had just become dramatically more expensive. The inflation was already baked in. It would arrive whether the war ended or not.",
        "Slow tracking shot through a large greenhouse where crops are being grown under artificial lighting, camera moving between the rows, the intense artificial illumination contrasting with darkness outside the glass panels",
        193
    ),
    make_clip(
        "Meanwhile, on the other side of the ledger, the credit crisis was actively destroying demand. When private credit funds freeze redemptions, the wealth effect reverses. Companies that had been counting on revolving credit facilities suddenly found them renegotiated at higher rates — or not renegotiated at all.",
        "Close-up of a plant wilting in a pot on a windowsill, camera watching the slow collapse of leaves that have lost their water, the metaphor of credit withdrawal as botanical desiccation",
        129
    ),
    make_clip(
        "The K-shaped economy was now becoming a K-shaped crisis. For the wealthy, the stagflation represented an opportunity: their hard assets — gold, real estate, energy stocks — were appreciating. For everyone else, it was a vice, tightening simultaneously from inflation and unemployment.",
        "Wide aerial shot of an American city at dusk from directly above, the geometric pattern of streets and blocks revealing the city's structure, camera slowly rotating to show the full 360-degree panorama before fading",
        257
    ),
    make_clip(
        "The Steve Keen interview on Soar Financially was titled 'Warflation: Oil Shock Plus Debt Crisis Could Break the Economy.' Keen, the heterodox Australian economist, argued that the debt dimension of the crisis was being systematically underestimated.",
        "Wide shot of a modern economic modeling computer setup in a darkened research office, multiple monitors displaying complex data visualizations, camera pulling back to reveal a single figure studying the screens in the blue glow",
        193
    ),
    make_clip(
        "Keen's analysis centered on the debt deflation dynamic. When highly leveraged economies experience a supply shock, the interaction between rising prices and falling asset values creates a kind of economic autoimmune response — the body attacks itself.",
        "Slow tracking shot through a neighborhood where some houses show foreclosure signs and others are immaculately maintained, camera at street level, the contrast between maintained and abandoned properties widening with each house",
        193
    ),
    make_clip(
        "The Federal Open Market Committee faced, in March 2026, a genuinely unprecedented combination of pressures. They could not cut rates fast enough to address the growth problem without risking runaway inflation. They could not raise rates to address inflation without triggering a credit system implosion.",
        "Wide interior shot of an empty conference room with a very long table and high-backed chairs, afternoon light from floor-to-ceiling windows casting long shadows, the architecture of institutional decision-making empty and silent",
        193
    ),
    make_clip(
        "They chose, as they so often choose, to wait. To watch. To assess incoming data. The language of 'data dependency' is the language of a central bank that does not know what to do. And in the void of decisive action, markets drew their own conclusions.",
        "Slow cinematic shot of an antique clock face, camera close on the clock hands moving, the minute hand advancing in real time, the ticking implied, the passage of precious time made visual",
        129
    ),
]

script["segments"].append({
    "act": "Act IV: The Fed's Impossible Bind — Stagflation and the K-Shaped Crisis",
    "theme": "fed_stagflation_bind",
    "clips": act4_clips
})

# ============================================================
# ACT V — SAFE HAVENS
# ============================================================

act5_clips = [
    make_clip(
        "In times of crisis, money moves. It does not sit still and accept losses. It migrates — from risk assets to safe havens, from paper to hard assets, from the uncertain to the perceived-certain. In March 2026, three destinations competed for that panicked capital.",
        "Aerial drone shot slowly orbiting a mountain peak above the clouds, white clouds rolling below the summit, the solid immovable stone of the mountain contrasting with the churning cloudscape below",
        257
    ),
    make_clip(
        "The first and oldest safe haven was gold. And by March 2026, gold had already had an extraordinary run. It was trading above five thousand dollars per ounce — having effectively doubled in the two years prior. But the analysts were saying the run was not over.",
        "Extreme close-up of a gold bar resting on dark velvet cloth in a dimly lit vault, camera focused on the stamped assay mark, light from a single source catching the metal's surface, the depth and warmth of gold visible",
        257
    ),
    make_clip(
        "Ronald Stoeferle, the Austrian fund manager and author of the annual 'In Gold We Trust' report at Incrementum, had been among the most accurate gold forecasters of the previous decade. His target for gold was five thousand, two hundred dollars per ounce.",
        "Wide shot of the Austrian Alps at golden hour, snow-capped peaks in the background, a small Austrian village in the valley below, camera slowly pushing in on the scene, the solidity and permanence of the mountains",
        193
    ),
    make_clip(
        "Stoeferle's argument, presented through Soar Financially, was that gold was no longer contrarian. What had been a fringe investment — the domain of gold bugs and Austrian economists and apocalyptic preppers — had become mainstream allocation. Central banks now held more than fifty percent of their reserves in gold.",
        "Wide shot inside a national bank vault, rows of gold bars stacked with perfect precision on wooden shelves, warm vault lighting catching the metallic surfaces, camera slowly tracking along the stacks to convey quantity",
        257
    ),
    make_clip(
        "Dr. Mark Thornton of the Mises Institute, appearing on the ITM Trading Daniela Cambone Show, argued that gold was not just rising — it was signaling. 'Gold isn't just rising,' he said. 'It's sounding the alarm bell.' He had been arguing for two years that the US was on the on-ramp to hyperinflation.",
        "Close-up of an alarm bell on the wall of an old building, the brass dome polished but slightly tarnished, camera slowly pushing in until the bell fills the frame, the potential energy of an alarm about to ring",
        193
    ),
    make_clip(
        "Thornton's analysis pointed to two simultaneous forces: the fall in global demand for US dollar-denominated assets, and the explosive growth in the dollar supply. On the demand side, central banks were diversifying away from Treasuries into gold. On the supply side, war costs required printing.",
        "Wide aerial shot of the US Treasury Building in Washington DC from a high angle, the neoclassical white building surrounded by mature trees, camera slowly rotating around the building, the geometry of monetary authority",
        193
    ),
    make_clip(
        "The retail investor community on YouTube was tracking gold with a mixture of validation and frustration. One commenter noted that gold was 'falling off a cliff' during certain sessions even as the war raged. Another, with studied patience, replied: 'Don't worry about short-term fluctuations. Fiat will go to zero. They always do. Precious metals will prevail.'",
        "Close-up of a person's hands holding a single gold coin up to lamplight, the coin rotating slowly to catch the light, the details of the surface visible, camera steady and fixed while the coin turns",
        129
    ),
    make_clip(
        "David Lin — one of the most followed financial journalists on YouTube — ran a video titled: 'Gold About to Double Again as Financial Crisis Now Inevitable.' The guest, Rob Bruggeman, argued that gold was only halfway through its current super-cycle.",
        "Wide shot of dawn light hitting a mountain range, the peaks emerging from darkness into golden morning light, camera slow and still, the metaphorical resonance of gold light and gold metal",
        193
    ),
    make_clip(
        "The silver market told an even more dramatic story. Silver, trading at ninety dollars per ounce in March 2026, had already achieved what silver investors had been predicting for two decades. It had broken out from its long consolidation. But now, the question was: where next?",
        "Extreme close-up of silver coins being poured from a leather pouch onto a dark wooden desk, camera close and still, each coin landing with implied sound, a gleaming pool of metal forming",
        193
    ),
    make_clip(
        "Soar Financially's video, titled 'Silver Boom 2026: What the Biggest Silver Miners Are Doing Now,' brought together executives from First Majestic Silver, Pan-American Silver, Endeavor Silver, and Hecla Mining. The message was unanimous: they were reporting earnings in billions, not millions.",
        "Wide aerial shot of a silver mine operation in a mountainous landscape, excavation equipment visible as tiny machines working terraced cuts into a hillside, the scale of industrial resource extraction from 2,000 feet",
        193
    ),
    make_clip(
        "Michael Bhaskara of Pan-American Silver captured the moment: 'Isn't it amazing? One year later and we're looking at over ninety dollars. I'm now reporting earnings in billions, not millions anymore.' First Majestic Silver was forecasting a price target of $150 to $175.",
        "Close-up of industrial silver bars being stacked by gloved hands in a secure storage facility, the cool grey metal contrasting with bright overhead lighting, each bar placed with precise care",
        193
    ),
    make_clip(
        "What drove silver above gold proportionally was the industrial demand component. Silver is not just a monetary metal. It is an essential industrial input: for solar panels, for electric vehicles, for semiconductors, for medical imaging, for five-G infrastructure.",
        "Wide aerial shot of a massive solar farm in a desert landscape, thousands of reflective panels arranged in precise geometric patterns, camera flying over the array at low altitude, the energy of captured sunlight made visible",
        193
    ),
    make_clip(
        "The AI energy super-cycle — the enormous power demands of data centers being built to run artificial intelligence workloads — was creating a derived demand for silver-intensive solar generation that dwarfed any single application that had existed before.",
        "Wide low-angle shot inside a massive data center, row upon row of server racks stretching to the horizon, cooling systems and cable management creating industrial texture, blue LED indicator lights creating an otherworldly environment",
        257
    ),
    make_clip(
        "Jeremy Schwartz of WisdomTree, speaking to Wealthion's Hard Assets community, identified this as the defining investment theme of the decade: the copper and silver super-cycle driven by electrification, AI energy demand, and the structural underinvestment in mining that had characterized the previous fifteen years.",
        "Wide aerial drone shot slowly circling a copper mine, the massive open-pit excavation revealing layers of geology in the terraced walls, small mining trucks visible at the bottom like toys, the raw earth exposure of civilization's foundation",
        257
    ),
    make_clip(
        "Gold and silver were the familiar safe havens. But the third destination for crisis capital in March 2026 was less familiar, more contested, and more philosophically interesting: Bitcoin.",
        "Wide exterior shot of a modern institutional investment building at night, single lit window in an otherwise dark facade, camera pushing slowly toward the light, the implication of clandestine analysis underway",
        193
    ),
    make_clip(
        "Bitcoin was trading at approximately seventy thousand dollars in the first two weeks of March 2026. That represented a significant decline from its all-time high of $126,000 reached in late 2025. But the analysts who had been most right about Bitcoin were using the decline to make a specific argument.",
        "Aerial drone shot tracking over a city at night, the camera following a highway's light trails from above, the organized flow of data and capital implied by the arterial pattern, crypto nodes as urban infrastructure",
        193
    ),
    make_clip(
        "Luke Gromen, the macro analyst and founder of Forest for the Trees, had developed a thesis about Bitcoin that was gaining significant institutional traction. He called it 'the last functioning smoke alarm for the global financial system.'",
        "Close-up of an old fire alarm on a painted wall, camera pushed in on the red device, the implied urgency of its potential activation, dust on the housing suggesting long quiescence, the alarm that hasn't been needed — until now",
        193
    ),
    make_clip(
        "Gromen's argument was precise: when governments face the impossible choice between defaulting on their debt or inflating it away, they always choose inflation. Bitcoin, as a fixed-supply asset that cannot be debased by any government decision, was not a speculative vehicle — it was a hedge against the inevitable.",
        "Wide shot of a printing press running at full speed, mechanical arms and paper moving in blur, camera at a stable medium distance, the industrial scale of money creation rendered as mechanical precision",
        193
    ),
    make_clip(
        "Arthur Hayes, the former BitMEX CEO and one of the most analytically rigorous voices in crypto, appeared on Wealthion with a characteristically blunt assessment: 'The Fed will always print money.' His implication was clear: the debate was not whether monetary expansion would continue, but how fast.",
        "Wide establishing shot of the exterior of the New York Federal Reserve building at night, the fortress-like stone exterior lit from below, iron bars visible on the ground-floor windows, the building's physical solidity emphasizing its monetary power",
        257
    ),
    make_clip(
        "The Bankless podcast, in its weekly rollup for the second week of March, described markets as having entered what Ethereum co-founder Vitalik Buterin had called 'the chaotic era.' Moved from the stable era to the chaotic era. Wars. AI. Overall jitters in markets.",
        "Wide cinematic drone shot slowly rising above a major city at dusk, the organized grid of streets gradually shrinking into geometric abstraction as altitude increases, the chaos of city life resolving into pattern from above",
        193
    ),
    make_clip(
        "The EllioTrades channel, covering the Iran-Bitcoin relationship specifically, asked the question its audience most wanted answered: what happens next to your Bitcoin and stocks? The answer depended on a single variable — how long the war would last.",
        "Close-up of a compass needle rotating slowly in a close shot, camera watching the needle as it searches for north, settling, being disturbed, settling again, the quest for directional certainty in uncertain conditions",
        129
    ),
    make_clip(
        "If the war was short — weeks, not months — markets would likely recover. Risk assets, as Ed Yardeni argued, tend to represent buying opportunities during geopolitical crises that resolve. The pattern of history supports this.",
        "Wide aerial drone shot over a city skyline, camera positioned to show both the urban built environment and the horizon beyond, early morning light suggesting both the end of night and the beginning of a new period",
        193
    ),
    make_clip(
        "But if the war extended into months — if Iran's proxy networks continued disrupting Gulf oil infrastructure, if the Strait remained functionally closed, if the private credit cascade continued building — then the historical analogy shifted from 1991 Gulf War to something much darker.",
        "Wide aerial shot of a massive oil tanker sitting perfectly still on a completely flat, mirrorlike ocean at dusk, no movement anywhere in the frame, the stillness suggesting suspended time, the ship a monument to arrested commerce",
        257
    ),
    make_clip(
        "Coin Bureau Finance's video — 'The Only Winner in the Iran War is Unexpectedly Russia' — captured the geopolitical reality that most Western commentators were reluctant to state clearly. Russia, sanctioned and isolated since 2022, was experiencing a windfall.",
        "Wide aerial shot of a vast Siberian oil field at dusk, pump jacks nodding rhythmically across a frozen landscape, orange flare stacks burning against the purple sky, the scale of Russian energy wealth made geographic",
        193
    ),
    make_clip(
        "Every dollar that Brent crude rose above $80 represented additional revenue for Russia, which was pumping its oil regardless of Western sanctions — selling it to China, India, and Turkey at slight discounts that were becoming far less significant as the benchmark price rose.",
        "Close-up of a large pipeline junction, the steel pipes thick and utilitarian, valves and pressure gauges visible, a worker in protective gear checking gauges in the background, the infrastructure of energy geopolitics made physical",
        193
    ),
    make_clip(
        "The ITM Trading live stream on March fifth — 'Iran War Day Six: Gold, Oil and What Happens Next To Your Money' — had drawn tens of thousands of viewers. One commenter posted: 'When Fiat goes to zero, they take you to war.' The cynicism ran deep. And it was not entirely without historical foundation.",
        "Wide shot of a busy financial district street at rush hour from ground level, camera at eye level in the pedestrian flow, the crowd moving around the camera, faces in various states of preoccupation and worry",
        129
    ),
    make_clip(
        "The retail investor community was not passive in this crisis. Across YouTube, across Reddit, across prediction markets, millions of ordinary people were doing their own analysis, making their own bets, hedging their own savings. The democratization of financial information had created a new kind of collective intelligence.",
        "Wide aerial drone shot over a sprawling suburb at dusk, thousands of lit windows in the residential grid below, each one representing a household watching, reading, deciding, camera rising slowly to reveal the full scale",
        193
    ),
]

script["segments"].append({
    "act": "Act V: Safe Havens — Gold at $5,200, Silver at $90, and Bitcoin as Smoke Alarm",
    "theme": "gold_silver_surge",
    "clips": act5_clips
})

# ============================================================
# ACT VI — GEOPOLITICAL CHESS
# ============================================================

act6_clips = [
    make_clip(
        "Beneath the immediate market turbulence, something more structural was happening. The Iran war and the oil shock were not merely disrupting existing arrangements — they were accelerating a geopolitical reorganization that had been building for a decade.",
        "Slow aerial drone shot over the South China Sea at dusk, oil tankers of multiple national flags visible from high altitude, camera slowly rotating to show the full horizon, the theater of global energy competition",
        257
    ),
    make_clip(
        "Charles Gave's thesis, presented through his partnership with Soar Financially, was that the oil shock was acting as a geopolitical sorting mechanism. It was separating the world into two camps: those who could absorb the disruption, and those who could not.",
        "Close-up of an old-fashioned brass compass sitting on a detailed nautical map, camera locked in close, the compass needle steady, the map's lines and contours suggesting strategic geography",
        193
    ),
    make_clip(
        "China, Gave argued, belonged firmly in the first camp. Beijing had spent fifteen years building what he called strategic energy depth — multiple supply routes, domestic production capacity, strategic petroleum reserves, and crucially, pipeline connections to Russia and Central Asia.",
        "Aerial drone shot over a long pipeline route through steppe landscape, the infrastructure cutting through vast flat terrain, camera tracking along the pipeline at low altitude, the length of the route suggesting strategic planning",
        257
    ),
    make_clip(
        "When Beijing looked at the Hormuz closure, it saw an inconvenience — not an existential threat. Some marginal Middle Eastern supplies would be disrupted. But Russia was pumping at capacity through overland pipelines. Kazakhstan, Turkmenistan, and Russia could supply most of China's incremental needs.",
        "Wide aerial shot over a major Chinese industrial city from high altitude, the city's scale and density creating a visual argument for economic mass, camera drifting slowly from industrial zones to residential areas",
        193
    ),
    make_clip(
        "For Japan and South Korea, by contrast, the picture was far grimmer. Both countries depend on Middle Eastern oil for the majority of their energy. Both face the Hormuz route with no overland alternative. The war was not just an economic problem for Tokyo and Seoul — it was an existential industrial challenge.",
        "Wide aerial shot over a large Japanese port city, modern industrial infrastructure and shipping facilities, Mount Fuji visible on the horizon in the distance, camera slowly panning to emphasize the geography of sea dependency",
        193
    ),
    make_clip(
        "The United States occupied an intermediate position. As a major oil producer itself — the world's largest, actually — the US was somewhat insulated from pure supply shock compared to its allies. But American consumers still paid global prices at the pump. And American companies still relied on global shipping lanes.",
        "Wide aerial drone shot over the Permian Basin in Texas, oil pump jacks and wellheads stretching to the horizon, camera flying at low altitude over the production infrastructure, the paradox of domestic abundance and imported prices",
        257
    ),
    make_clip(
        "Market Insider analyst Michael Howell of CrossBorder Capital had identified a global liquidity cycle that had peaked in the fall of 2025 and was now turning down. The timing was, in retrospect, grimly perfect: the financial system was already becoming more stressed when the war erupted.",
        "Wide aerial shot over a major global financial center at night, the illuminated skyscrapers reflected in a river, camera slowly rising to emphasize the interconnectedness of the lit buildings, the city as a circuit board",
        193
    ),
    make_clip(
        "Global liquidity — the aggregate of credit creation, central bank balance sheets, and cross-border capital flows — is the tide that lifts all boats in financial markets. When it peaks and turns, the withdrawal of that tide exposes what had been hidden beneath the surface.",
        "Wide shot of a harbor at low tide, boats sitting on mud, the tideline clearly visible on wooden dock pilings, camera panning slowly along the exposed infrastructure that high tide normally conceals",
        193
    ),
    make_clip(
        "The copper market told a forward-looking story about the structural stresses within the energy transition. Jesse Day, CEO of Copper Giant, spoke to the Commodity Culture channel from Medellín, Colombia, where his company was advancing a major copper-molybdenum deposit.",
        "Aerial drone shot circling a large open-pit copper mine in South American terrain, dramatic red-orange walls of exposed rock in terraced formation, tiny mining trucks far below, the scale making the mountain's excavation surreal",
        257
    ),
    make_clip(
        "Day's assessment of the copper market was direct: 'Copper is the next major rotation.' The thesis was not complicated. Data centers for artificial intelligence consume copper at extraordinary rates. Solar panels require copper. Wind turbines require copper. Electric vehicles require copper.",
        "Close-up of copper wiring being manufactured, bright orange-red metal being drawn through industrial machinery, the shining surface catching overhead industrial lights, camera close on the material itself",
        193
    ),
    make_clip(
        "And yet, copper mining had been chronically underinvested. The lead times for bringing new copper mines into production span fifteen to twenty years from discovery to first production. The deficit that was building in the copper market could not be solved quickly, regardless of price signals.",
        "Wide establishing shot of an abandoned copper mine facility, rusted equipment standing still, overgrown access roads, camera slowly pulling back to reveal the full scale of the derelict operation, the past and future of mining in one frame",
        193
    ),
    make_clip(
        "Jeremy Schwartz of WisdomTree, speaking about what he called the AI energy super-cycle, identified copper as the defining commodity of the next decade. Not gold. Not oil. Copper. The metal that connects solar generation to electric vehicles to data centers.",
        "Wide aerial drone shot over a large data center campus with rooftop solar installation, camera flying slowly overhead, the interconnected systems of power generation and digital processing suggesting the copper nexus",
        193
    ),
    make_clip(
        "For Europe, the convergence of crises was particularly acute. The continent had already spent three years in energy transition after the Russian gas cutoffs of 2022. It had built LNG import terminals, connected northern and southern grids, and signed long-term supply deals with Qatar.",
        "Wide aerial shot over a major European port, LNG terminal visible with silver storage tanks, camera flying at moderate altitude, the industrial infrastructure of energy security built at enormous cost",
        193
    ),
    make_clip(
        "Now, with QatarEnergy's force majeure declaration and Hormuz effectively closed, those long-term supply deals were suspended by legal force. Europe found itself in an acute energy crisis within ten days of the war's start — with limited alternative supply sources and approaching summer, not winter.",
        "Wide shot of an empty industrial pier on a grey European morning, fog over the water, a mooring post with no ship attached, camera slowly pushing toward the vacant berth, the absence made tangible",
        193
    ),
    make_clip(
        "The deindustrialization scenario — European manufacturing unable to compete with energy costs — was no longer theoretical. German industrial electricity prices had tripled from their 2020 baseline. With LNG supplies disrupted, they were set to rise further.",
        "Wide aerial drone shot over a large German industrial complex, multiple factory buildings and chimneys, some actively producing, some idle, camera flying slowly over to reveal the patchwork of activity and closure",
        193
    ),
    make_clip(
        "Russia, meanwhile, observed the crisis from a position of genuine strategic advantage. As the Coin Bureau analysis documented, Russia was the war's only unambiguous winner in economic terms. Its oil revenues were rising with every dollar increase in Brent crude.",
        "Wide aerial shot over a vast Russian oil export terminal at the Baltic Sea, tankers lined up at loading berths, camera flying at altitude to show the scale of the facility, late afternoon light on the water",
        193
    ),
    make_clip(
        "Russian President Putin, who had spent three years absorbing Western sanctions, watching his economy contract and then adapt, was now watching oil prices do what Western sanctions had tried and failed to do to his Western adversaries: create economic pain at scale.",
        "Slow wide shot of the Kremlin at night from across the Moscow River, the illuminated towers and walls reflected in dark water, camera very still and low to the water surface, the reflection creating a perfect mirror image",
        257
    ),
    make_clip(
        "The geopolitical chess game that had been playing out since 2014 — since Crimea, since Ukraine, since the sanctions regime — was entering a new phase. Every move had consequences that extended far beyond the immediate board. And in March 2026, it felt as though multiple pieces were moving simultaneously.",
        "Wide aerial drone shot over a chessboard-like urban grid, the regular pattern of city blocks seen from high altitude, camera rotating slowly to reveal the full 360-degree cityscape, the strategy embedded in the geography",
        193
    ),
    make_clip(
        "Global liquidity contracting. Oil supply disrupted. Private credit freezing. A war that showed no sign of quick resolution. These were not four separate problems. They were one problem — a systemic fragility that had been building for years, now expressing itself simultaneously across multiple dimensions.",
        "Slow cinematic tracking shot through a forest where multiple trees have fallen and are leaning against each other, camera moving through the scene at low angle, the precarious interdependence of the fallen trees made visible",
        193
    ),
    make_clip(
        "The commenter on the Adam Taggart Thoughtful Money channel who wrote about the 1929 parallel — listing in careful detail the same catalysts: excessive speculation, overvalued stocks, tariffs, deportations, geopolitical escalation — signed off with a line that captured the moment: 'The Titanic has sailed again.'",
        "Wide cinematic night shot of a large dark ocean liner at sea from the deck of another vessel, the lights of the ship visible in the dark, camera slowly pulling back as the ship continues its course away into the darkness",
        257
    ),
]

script["segments"].append({
    "act": "Act VI: Geopolitical Chess — US-China Oil Dynamics, Russia's Windfall, and the Copper Supercycle",
    "theme": "us_china_geopolitics",
    "clips": act6_clips
})

# ============================================================
# ACT VII — CLOSING
# ============================================================

act7_clips = [
    make_clip(
        "It is March thirteenth, 2026. Thirteen days since the first strikes hit Iranian territory. The Strait of Hormuz remains functionally closed. The war continues. The private credit cascade continues. The Federal Reserve watches and waits.",
        "Wide aerial drone shot over the Persian Gulf at dusk, the calm water surface unmarked by ship traffic, the horizon empty, camera hovering still at altitude, the vast emptiness of a closed waterway",
        257
    ),
    make_clip(
        "What have we learned? What does it mean? Let us be honest about what we know — and what we do not know.",
        "Slow push shot toward a single lit window in a dark building, camera approaching from the street, the warm interior light gradually becoming the entire frame, a figure's shadow visible on the illuminated curtains within",
        193
    ),
    make_clip(
        "We know that the oil market's vulnerability to a Hormuz disruption was not a theoretical risk in textbooks. It was a real and present danger that had been identified, analyzed, and — somehow — allowed to remain unaddressed. Martin Wolf had warned about it. Energy security analysts had warned about it. The warning signs were visible for years.",
        "Close-up of a well-worn book lying open on a desk, the pages slightly yellowed, camera focused on the text without allowing it to be read, the idea of unheeded wisdom made physical",
        193
    ),
    make_clip(
        "We know that the private credit market, which grew from a sensible post-2008 innovation into a two-trillion-dollar systemic risk, carried within it the same fundamental flaws as every credit bubble that preceded it: opacity, misaligned incentives, and the illusion of liquidity.",
        "Close-up of a soap bubble floating in sunlight, camera tracking the bubble as it drifts, its iridescent surface showing rainbow colors, the inevitable fragility of the sphere made beautiful and transient",
        193
    ),
    make_clip(
        "We know that stagflation — the combination of rising prices and falling economic output — is not just a historical curiosity from the 1970s. It is a recurring feature of economies that allow their energy dependencies to remain unresolved and their monetary systems to accumulate unsustainable obligations.",
        "Wide aerial drone shot over an American suburb in autumn, leaves turning orange and red on the trees, the seasonal change suggesting economic cycles, camera rising slowly to reveal the scale of the residential landscape",
        193
    ),
    make_clip(
        "We know that the K-shaped economy is not an accident. It is the result of decades of policy choices — monetary, fiscal, regulatory — that consistently favored capital over labor, asset owners over workers, financial complexity over productive investment.",
        "Wide aerial shot of a major American city, camera positioned to show the visible inequality of the built environment: gleaming new towers and adjacent neighborhoods of worn housing, the geography of economic divergence",
        193
    ),
    make_clip(
        "Joseph Stiglitz, the Nobel laureate, had spent his career documenting this divergence. His warning — 'Economic Chaos Threatened By Middle East War' — was not surprising to anyone who had read his work. What was perhaps surprising was how quickly the theoretical became actual.",
        "Wide shot of a Nobel Prize medal lying on a dark surface, camera in close focus on the medal, the engraved profile visible, warm light picking out the relief, the weight of recognized expertise",
        129
    ),
    make_clip(
        "And what do we not know? We do not know how long the war will last. We do not know whether the Strait of Hormuz will reopen tomorrow or in six months. We do not know whether the private credit cascade will be contained by policy action or whether it will metastasize into something that requires a systemic rescue.",
        "Wide aerial drone shot over a desert crossroads at dusk, four empty roads leading to the horizon in each cardinal direction, camera hovering directly overhead, the four directions equally uncertain",
        257
    ),
    make_clip(
        "We do not know whether the Federal Reserve has the capacity — institutional, political, analytical — to thread the needle between inflation and recession that its current situation demands. The history of central banks facing stagflation is not encouraging.",
        "Wide establishing shot of the Federal Reserve building at night, just the exterior lit, no visible human activity, camera slow and still, the architecture suggesting permanence while the policy moment suggests crisis",
        193
    ),
    make_clip(
        "We do not know whether the geopolitical realignment underway — the gradual decoupling of East and West, the emergence of alternative trade routes and payment systems, the rise of commodity-backed currencies — will accelerate into a genuine restructuring of the international financial system, or whether it will stabilize into a new equilibrium.",
        "Slow aerial drone shot over a major international port where ships from many nations share the same berths, flags of many countries visible, camera rising slowly to show the full port in context of the surrounding city",
        257
    ),
    make_clip(
        "What we do know — what history tells us, what the markets are signaling, what the analysts from Luke Gromen to Charles Gave to Martin Wolf are saying — is that the architecture of the global financial system that was built after 1971, after Bretton Woods, after the petrodollar arrangement, is under the most severe stress it has experienced since at least 2008.",
        "Slow cinematic shot of the foundation of a large stone building, camera close to the base of the structure, the weight of the edifice above implied, camera slowly tilting upward along the stonework to the sky",
        193
    ),
    make_clip(
        "Some walls develop cracks before they fall. Some systems show stress fractures long before they fail. The question — the only question that matters now, for investors, for policymakers, for anyone who must make decisions in uncertainty — is whether what we are seeing is a stress fracture, or the beginning of the fall.",
        "Close-up of a crack in a wall of a historic building, camera focused on the crack itself, the crack running from floor to ceiling in the background, natural light highlighting the fracture line against the solid stone",
        193
    ),
    make_clip(
        "For investors navigating this environment, the consensus among the analysts surveyed across these thirteen days was notable for its consistency. It transcended the usual left-right, bull-bear, traditional-alternative divides.",
        "Wide interior shot of an elegant investment conference room with multiple chairs arranged in a circle, camera rotating slowly through the space, the arrangement suggesting collegial decision-making, afternoon light from tall windows",
        193
    ),
    make_clip(
        "Brett Rentmeester of Windrock, speaking on Wealthion, articulated what many were thinking: hard assets matter more than ever in a world of money printing and currency devaluation. Think beyond stocks and bonds. Gold, silver, energy, real assets — these are the portfolio anchors for a chaotic era.",
        "Wide aerial shot of a working farm in early spring, fields being prepared for planting, the physical labor of food production from high altitude, a reminder that real assets begin in the earth itself",
        193
    ),
    make_clip(
        "The Market Insider analyst suggested a barbell approach: energy and resources on one side, consumer staples on the other. Diversify away from technology. Move toward things that have physical reality — commodities, infrastructure, tangible productive capacity.",
        "Wide aerial drone shot over a diverse landscape showing simultaneously farmland, industrial facility, and natural resource extraction, camera high and still, the integrated reality of physical economy",
        193
    ),
    make_clip(
        "Ronald Stoeferle's message on gold was perhaps the most succinct: gold is no longer contrarian. It is mainstream. The question is not whether to own it, but how much, in what form, and through what vehicle. That is the conversation the investment world is now having.",
        "Close-up of a scale balance holding a single gold bar on one side and balanced perfectly, camera watching the balance as it holds steady, the equilibrium of a properly weighted portfolio made physical",
        193
    ),
    make_clip(
        "On Bitcoin, the message from Arthur Hayes and Luke Gromen converged on the same logic: the direction of monetary policy is predetermined by the mathematics of sovereign debt. Governments will print. The question is the pace and the trigger. Bitcoin is the instrument that measures that pace most honestly.",
        "Wide aerial drone shot over a major tech campus at night, buildings lit with characteristic tech-company blue and white light, camera flying slowly overhead, the nodes of digital infrastructure as physical manifestation",
        193
    ),
    make_clip(
        "And on the broader crisis — the warflation that Ben Rhodes, Joseph Stiglitz, Martin Wolf, and dozens of other analysts have been trying to make legible for the public — the message is sobering but not hopeless.",
        "Wide shot of dawn breaking over a major city, first light touching the tops of buildings, the transition from darkness to light captured in slow time-lapse, the city awakening after a long and difficult night",
        257
    ),
    make_clip(
        "Economies have survived oil shocks before. They have survived private credit crises. They have survived stagflation. They have even survived wars — though rarely without permanent scars. The human capacity for adaptation is not unlimited, but it is real.",
        "Wide aerial drone shot over a city that shows both damage and reconstruction simultaneously, cranes working on new buildings adjacent to older structures being repaired, the perpetual cycle of construction and renewal",
        193
    ),
    make_clip(
        "What they have not survived — what no financial system in history has survived — is the combination of unsustainable debt, monetary debasement, and geopolitical conflict without fundamental restructuring. The restructuring may be orderly or disorderly. It may come quickly or slowly. But it comes.",
        "Slow cinematic wide shot of a river in full flood, brown water surging around trees and structures, the power of the natural force visible but not catastrophic, the landscape absorbing the stress of the overflow",
        193
    ),
    make_clip(
        "The analyst community watching these thirteen days was not watching a random sequence of events. They were watching a system under compound stress express its accumulated vulnerabilities — all at once, in a compressed time frame, in a way that made the connections visible for anyone paying attention.",
        "Wide aerial drone shot over a dense urban area at night, the grid of streets and lit windows from above, a river running through the center, the organized complexity of human civilization as fragile geometry",
        257
    ),
    make_clip(
        "The retail investors watching on YouTube, posting their comments in real time — the commenter who noted the '1929 moment,' the one who predicted '$200 oil,' the one who said simply 'everything sucks' — were not unsophisticated. They were expressing what the data confirmed.",
        "Close-up of a person's hand reaching toward a glowing screen in a dark room, finger hovering before making contact, the intimacy of one person connecting to the information of the world, a quiet moment of decision",
        129
    ),
    make_clip(
        "Things were bad. And the conditions that made them bad had been accumulating for years, in plain sight, in publicly available data, in the reports of economists and analysts who had been raising alarms that policy makers, comfortable in their cycles of expansion, had chosen not to hear.",
        "Wide shot of an empty amphitheater with a single spotlight on the empty speaking podium at the center, rows of vacant seats receding into shadow, the set for warnings that went undelivered or unheeded",
        193
    ),
    make_clip(
        "Martin Wolf, a man who has studied every major economic crisis of the past half century, who has advised governments and central banks and international institutions, closed his Monetary Matters interview with a sentence that felt less like analysis and more like testament.",
        "Close-up of an elderly person's hands resting on the open pages of a thick book on a wooden table, camera close and still, the texture of the hands and the pages suggesting a lifetime of engagement with ideas",
        193
    ),
    make_clip(
        "'I have become very suspicious,' Wolf said, 'of outsiders who say they know how Iran can be guided. If you push people into a corner, they don't get less dangerous. They get more dangerous.' The observation applied to more than just Iran. It applied to the entire system.",
        "Wide aerial drone shot of a walled city, camera positioned to see both the interior and exterior of the walls simultaneously, the inside and outside worlds divided by a single structure, the metaphor of geopolitical encirclement",
        257
    ),
    make_clip(
        "Persia, Wolf noted, was the first great empire in human history. They fought the Roman Empire to a standstill over centuries. The systems we are dealing with — in energy, in credit, in geopolitics — have their own forms of historical depth and resilience. Not to be underestimated.",
        "Wide aerial drone shot over the ruins of an ancient civilization in a Middle Eastern landscape, the remnants of walls and structures visible from above, the geometry of a great and fallen empire, the long arc of history",
        257
    ),
    make_clip(
        "As of March thirteenth, 2026, this story is not over. It is, in fact, still in its opening chapters. What happens next — in the Strait, in the private credit markets, in the Federal Reserve's board room, in the gold and silver and Bitcoin markets — will define the economic landscape for years to come.",
        "Wide aerial drone shot flying toward the horizon over open ocean at sunrise, camera pointed directly at the rising sun, the light growing in intensity as the camera moves forward, the future as blinding unknown light",
        257
    ),
    make_clip(
        "The question is whether we — investors, citizens, policymakers — are watching carefully enough, thinking clearly enough, acting decisively enough to navigate what comes. History suggests we often are not. But history also suggests that, eventually, we find a way.",
        "Wide cinematic aerial drone shot over a coastal city at golden hour, the last light of the day hitting the water, the city's lights beginning to come on in the buildings, the transition between day and night suggesting both ending and beginning",
        257
    ),
    make_clip(
        "This has been the story of warflation. Of the thirteen days that began the global economic crisis of 2026. Of the oil shock, the credit cascade, the stagflation dilemma, the rush to safe havens, and the geopolitical realignment that may define the decades ahead.",
        "Slow aerial drone shot rising above a global city at dusk, pulling back and rising until the city becomes a point of light in the larger darkness, the full context of a small civilization in a large world",
        257
    ),
    make_clip(
        "Stay informed. Stay diversified. And remember, as the most seasoned voices in this story have reminded us, again and again: the greatest risk in unprecedented times is the assumption that the future will resemble the past.",
        "Final wide aerial drone shot over a calm dark ocean at night, the full moon reflected in the water below, camera slowly rising straight up until only the moonlit ocean and the dark horizon are visible, the world reduced to its most fundamental elements",
        257
    ),
]

script["segments"].append({
    "act": "Act VII: What Comes Next — Synthesis, Investor Implications, and the Road Ahead",
    "theme": "k_shaped_economy_recession",
    "clips": act7_clips
})

# Write the script
with open('/home/user/workspace/v5_script.json', 'w', encoding='utf-8') as f:
    json.dump(script, f, indent=2, ensure_ascii=False)

# Calculate statistics
total_clips = 0
total_words = 0
clips_per_act = []

for segment in script["segments"]:
    act_clips = len(segment["clips"])
    act_words = sum(len(clip["narration"].split()) for clip in segment["clips"])
    total_clips += act_clips
    total_words += act_words
    clips_per_act.append({
        "act": segment["act"],
        "clips": act_clips,
        "words": act_words,
        "estimated_duration_min": round(act_words / 135, 1)
    })

estimated_duration_min = total_words / 135

print("=" * 70)
print("V5 DOCUMENTARY SCRIPT — GENERATION COMPLETE")
print("=" * 70)
print(f"\nFile saved: /home/user/workspace/v5_script.json")
print(f"\nTOTAL CLIPS:     {total_clips}")
print(f"TOTAL WORDS:     {total_words:,}")
print(f"EST. DURATION:   {estimated_duration_min:.1f} minutes at 135 WPM")
print(f"\nCLIPS PER ACT:")
print("-" * 70)
for act_data in clips_per_act:
    print(f"  {act_data['act'][:55]:<55} {act_data['clips']:>3} clips  {act_data['words']:>5} words  {act_data['estimated_duration_min']:>5.1f} min")
print("-" * 70)
print(f"  {'TOTAL':<55} {total_clips:>3} clips  {total_words:>5} words  {estimated_duration_min:>5.1f} min")
