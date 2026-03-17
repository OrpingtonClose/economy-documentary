#!/usr/bin/env python3
"""
Expanded v5 documentary script - targeting 330+ clips, ~13,500 words
"""
import json

title = "The World on Fire: The Global Economic Crisis of March 2026"
description = """In thirteen days, the world changed. On March 1, 2026, US and Israeli strikes on Iran ignited a chain reaction that no economic model had fully priced in. The Strait of Hormuz — through which 20% of the world's oil flows — went dark. Ship traffic collapsed 94%. Brent crude spiked to $115. LNG prices surged 137% in five days. And in the shadows of the energy shock, a $300 billion private credit crisis began to crack open.

This is the story of March 2026: how a war triggered an oil shock, how an oil shock threatened a debt crisis, how a debt crisis paralyzed the Federal Reserve, and how ordinary investors were left searching for shelter in gold, silver, and Bitcoin.

Featuring analysis from Martin Wolf (Financial Times), Joseph Stiglitz (Nobel laureate), Charles Gave (Gavekal Research), Ronald Stoeferle (Incrementum), Luke Gromen, Arthur Hayes, and leading voices from across the financial world.

A Bloomberg Originals-style documentary examining the intersecting crises reshaping the global economy in real time."""

negative_prompt = "blurry, low quality, distorted, text, watermark, logo, subtitles, words, letters, numbers, UI overlay, cartoon, anime, illustration, painting, drawing, screen with text, monitor with data"

clips_list = []
clip_counter = [0]

def clip(frames, narration, prompt):
    clip_counter[0] += 1
    cid = f"clip{clip_counter[0]:03d}"
    c = {
        "id": cid,
        "frames": frames,
        "narration": narration.strip(),
        "prompt": prompt.strip()
    }
    clips_list.append(c)
    return c

# ============================================================
# ACT I: COLD OPEN — THE IRAN WAR BEGINS
# Target: ~50 clips, ~2,000 words
# ============================================================
act1_clips = []

c = clip(257,
    "March first, 2026. In the pre-dawn darkness over the Persian Gulf, the sky above Tehran erupted in blinding white.",
    "Extreme wide aerial shot over a vast dark desert at night, distant horizon erupting in massive orange and white explosions, shockwave rings rippling outward, stars still visible above the violence below, cinematic slow motion")
act1_clips.append(c)

c = clip(257,
    "American and Israeli aircraft had crossed Iranian airspace simultaneously. The targets were precisely chosen: nuclear facilities, IRGC command centers, missile launch infrastructure — and the Supreme Leader's compound in the north of the capital.",
    "Tight cinematic shot of afterburners blazing on military jets banking hard over black ocean water at night, orange flame trails cutting through total darkness, motion blur on wingtips, dramatic low angle perspective")
act1_clips.append(c)

c = clip(193,
    "Within hours, reports began filtering through encrypted military channels. Ayatollah Ali Khamenei — Iran's Supreme Leader for thirty-four years, the axis around which the Islamic Republic had rotated since 1989 — had been killed in the strikes.",
    "Slow push-in on a massive crowd gathered in a public square at night, lit only by candles and phone screens, hundreds of thousands of people in silent vigil, overhead wide drone shot pulling back to reveal the full scale")
act1_clips.append(c)

c = clip(257,
    "Senior commanders of the Islamic Revolutionary Guard Corps were eliminated alongside him. Generals whose names were known only to intelligence analysts, planners of operations from Yemen to Syria to Lebanon, were gone in a single night.",
    "Cinematic close-up of military medals and insignia laid out on a dark surface, single overhead light illuminating them, a slow dolly shot moving across the objects, empty chairs visible in soft background focus")
act1_clips.append(c)

c = clip(193,
    "Within forty-eight hours, Iran's new emergency leadership had issued one response. A single order. Close the Strait of Hormuz.",
    "Aerial drone shot looking straight down on the narrow blue-green waters of a strait between two rocky coastlines, a lone military patrol vessel cutting a white wake through otherwise calm water, ominous and perfectly still")
act1_clips.append(c)

c = clip(257,
    "Ben Rhodes, former Deputy National Security Advisor and architect of the 2015 Iran nuclear deal, called it — in a phrase that would echo across every financial news terminal on Earth — the Great Lie of War. A conflict sold on certainty, delivered into chaos.",
    "Slow dolly shot through an empty government briefing room, leather chairs pushed back from a long mahogany conference table, American flags drooping in stillness, late afternoon light slanting through venetian blinds across the polished floor")
act1_clips.append(c)

c = clip(193,
    "Rhodes, speaking to Ezra Klein, was direct: 'Would I want to be the one to justify the civilian casualties that come from this war? No, I would not.' The costs had already begun to manifest — not just in blood, but in the global economy.",
    "Wide aerial shot of a conference center at dusk, cars in the parking lot below, the building lit from within, the architecture of institutional power and accountability, security cameras visible at the corners")
act1_clips.append(c)

c = clip(257,
    "In Washington, the administration insisted the operation was proceeding ahead of schedule. Four to five weeks, the president said. Everything going swimmingly. The markets heard something different.",
    "Wide shot of a massive trading floor at the opening bell, hundreds of traders at screens, red indicators flashing across boards, a blur of urgent movement, overhead crane shot pulling back and up to reveal the full scale of the room")
act1_clips.append(c)

c = clip(193,
    "S&P futures dropped two and a half percent in overnight trading. The Dow opened down six hundred points. Oil futures gapped through resistance levels that had held for years.",
    "Extreme close-up of hands gripping the edge of a trading desk, knuckles white, blurred screens reflecting red light on the face of the unseen trader, cinematic depth of field with the room in soft focus behind")
act1_clips.append(c)

c = clip(257,
    "This is the story of March 2026. Thirteen days that shook the financial world. A war, an oil shock, a private credit collapse, and a Federal Reserve with no good options. And at the center of all of it, a debt load that made every response more dangerous than the last.",
    "Slow aerial drone push forward over a massive oil refinery lit up at dusk, towers of steel and pipe and flame glowing orange against a purple-pink sky, steam clouds drifting slowly, the industrial cathedral of the global economy")
act1_clips.append(c)

c = clip(257,
    "Martin Wolf of the Financial Times had been warning about exactly this scenario for years. As chief economics commentator for the world's most authoritative financial newspaper, Wolf had spent three decades studying oil shocks, wars, and their economic consequences.",
    "Close-up of a broadsheet newspaper on a dark mahogany desk, hands smoothing the pages open, reading glasses resting beside the paper, a cup of tea steaming in soft morning light, the ritual of serious financial journalism")
act1_clips.append(c)

c = clip(193,
    "Asked directly by The Monetary Matters Network whether the Iran war could cause a new oil crisis, Wolf's answer was precise: 'If one wanted to think of a nightmare disruption scenario for the world economy, it would be a war in the Gulf.'",
    "Wide establishing shot of a centuries-old stone library interior, tall shelves receding into shadow, a single reading lamp illuminating an open desk, heavy silence, dust motes drifting in a beam of afternoon light through a high window")
act1_clips.append(c)

c = clip(257,
    "Wolf continued: 'If the straits were closed for three months or more, we would be looking at a major jolt to the world economy.' Three months. The current closure was thirteen days old and showed no sign of ending.",
    "Wide shot of an antique globe on a library desk, the Middle East region visible, a hand slowly rotating the globe and stopping at the Persian Gulf, the cartographic weight of geography")
act1_clips.append(c)

c = clip(193,
    "Wolf added something that investors needed to hear. History as a warning: 'Persia was the first great empire in human history. They've been around a long time. The Persians fought the Roman Empire to a standstill over centuries. Not to be underestimated.'",
    "Tight shot of ancient stone relief carvings on a weathered wall, Achaemenid Persian figures in procession, deep shadows in the carved grooves, a hand slowly tracing the detail without touching, cinematic shallow focus on the carved surface")
act1_clips.append(c)

c = clip(257,
    "Nobel laureate economist Joseph Stiglitz, speaking to The Monetary Matters Network, was equally stark. The Middle East war, he said, threatened economic chaos — not as a distant possibility, but as an immediate, present reality for households and businesses around the world.",
    "Wide shot of a stone amphitheater at dusk, empty stone seats stretching up in a semicircle, a single lit podium at the base illuminating the surrounding darkness, the architecture of public argument and accountability")
act1_clips.append(c)

c = clip(193,
    "Stiglitz's transmission mechanism was elegantly simple: war disrupts energy supply. Energy supply drives input costs across every industry. Input costs drive inflation. And inflation destroys the purchasing power of everyone who cannot protect themselves with financial assets.",
    "Cinematic tracking shot moving slowly through an industrial warehouse, shelves stacked floor to ceiling with goods, a forklift frozen mid-aisle, overhead fluorescent lights casting clinical shadows, sense of stillness and vulnerable abundance")
act1_clips.append(c)

c = clip(257,
    "We have seen this movie before. The oil embargo of 1973. The Iranian revolution of 1979. Each time, the developed world was unprepared. Each time, the economic consequences lasted not months but years. Each time, the damage fell hardest on the least protected.",
    "Archival-style sepia-tone aesthetic shot of a long line of vehicles at a gas station in twilight, attendants in overalls, drivers standing outside their cars, the queue stretching around the block, the visual memory of energy vulnerability")
act1_clips.append(c)

c = clip(257,
    "But 2026 is different from 1973 in one critical respect. The global economy now carries forty trillion dollars in US federal debt alone. The shock absorbers have been stripped. The reserve capacity that allowed previous generations to absorb oil shocks has been consumed by decades of deficit spending.",
    "Extreme close-up of a rusted shock absorber spring on industrial equipment, corroded and compressed, held together by fraying metal threads, dark workshop light, depth-of-field focus on the stress fracture at the coil center")
act1_clips.append(c)

c = clip(193,
    "ITM Trading framed the historical stakes precisely: the 1973-74 oil embargo led to nationwide fuel shortages. Oil prices doubled, then quadrupled. It fed an inflation crisis that was already building, crushing Americans' purchasing power and raising interest rates to levels the country could barely survive.",
    "Cinematic slow push into a rusted industrial oil pump in a field at golden hour, the pump arm rising and falling in hypnotic rhythm, long dry grass swaying around the base, vast empty sky above the mechanism")
act1_clips.append(c)

c = clip(257,
    "The 1973 shock eventually resolved. But the purchasing power that American households lost in those years — the real wages, the savings, the certainty about tomorrow — those never fully came back. And the damage was measured not just in dollars, but in political disillusionment that shaped American politics for decades afterward.",
    "Wide shot of a shuttered factory exterior, windows boarded, overgrown parking lot with cracked asphalt, a lone security light still burning at the entrance, long shadows at dusk, the archaeological record of deindustrialization")
act1_clips.append(c)

c = clip(257,
    "Today, the starting conditions are worse. In 1973, the United States was at peak industrial production. The workforce was employed in manufacturing. The trade balance was manageable. In 2026, it carries the weight of decades of financialization, outsourcing, and debt accumulation that make each successive shock harder to absorb.",
    "Aerial drone shot over a modern automated factory, robotic arms moving with precision along assembly lines, few human workers visible, the replaced economy side by side with the replacing economy, golden hour light on the machinery")
act1_clips.append(c)

c = clip(193,
    "The Iran war was now entering its second week. Iranian proxies in Yemen, Iraq, and Lebanon had activated. US naval forces in the Persian Gulf were operating under heightened threat conditions. The military situation was fluid, the economic situation was deteriorating, and the political situation was hardening.",
    "Aerial shot looking down on a naval carrier strike group in formation on dark ocean water, ships arranged in protective pattern around the central carrier, white wakes cutting the blue-black surface, the precision of military deployment")
act1_clips.append(c)

c = clip(257,
    "On The Mark Thompson Show, former prosecutor David Katz described the administration's position in blunt terms: 'The president would have you and me believe that this is not going to be a long military engagement. He'll have you believe we're ahead of schedule, way ahead of schedule, that everything is going swimmingly.' The markets were pricing in a different reality.",
    "Wide shot of a press briefing room with rows of empty chairs, a podium lit by a single spotlight, an American flag to one side, the stage of official communication empty and waiting, the gap between claim and reality")
act1_clips.append(c)

c = clip(193,
    "Retail investors watching financial channels on YouTube were already sounding the alarm in real time. As one commenter put it on the ITM Trading live stream on Day Six: 'This month America has to sell three hundred billion dollars in treasuries, just to help pay one trillion in interest on its thirty-nine trillion dollar debt.'",
    "Close-up of a smartphone screen glowing in a dark room, the light reflecting on a face just out of frame, comments scrolling upward in a live feed, the blue-white glow the only light source, the democratized financial anxiety of the streaming era")
act1_clips.append(c)

c = clip(257,
    "When ordinary savers start talking about treasury auction mechanics, something fundamental has shifted in the public discourse. The war had not merely disrupted energy markets. It had ripped open a conversation about American fiscal sustainability that Wall Street had been carefully avoiding for years.",
    "Slow overhead drone shot over a residential suburb at night, houses lit from within in warm amber, the grid of streets stretching to the horizon, ordinary life visible in every window, a profound stillness that belies the economic pressure building inside each home")
act1_clips.append(c)

c = clip(257,
    "The FBI had issued a warning that Iranian drone attacks on California's West Coast were being actively planned. The Mark Thompson Show covered the report from March twelfth. The geography of the conflict was expanding. The risk was no longer confined to the Persian Gulf.",
    "Wide aerial shot of a coastline at dusk, the Pacific Ocean stretching to the horizon, military radar installations visible on high ground, the domestic security implications of a foreign war coming into focus")
act1_clips.append(c)

c = clip(193,
    "A military school in Iran had reportedly been struck by American aircraft. The administration initially denied knowledge. The new Iranian Supreme Leader's response, according to analysts on The Mark Thompson Show, was threefold: keep the regime standing, demonstrate capacity to retaliate, and internationalize the conflict.",
    "Wide aerial shot of a rubble field in an urban landscape, brick and concrete debris, rescue workers visible at the edges of the frame, the human cost of precision warfare and its imprecisions")
act1_clips.append(c)

c = clip(257,
    "The Iranian defense doctrine had always rested on the theory of deterrence-through-disruption. Iran could not match American military power in a conventional engagement. But it could — and now did — disrupt the global energy system in ways that imposed costs on America's allies in Europe and Asia that vastly exceeded the military cost of the strikes themselves.",
    "Dramatic wide shot of a chess board with a king piece surrounded by other pieces, a hand removing one of the supporting pieces from the board, the king exposed, the logic of asymmetric vulnerability rendered in game geometry")
act1_clips.append(c)

c = clip(193,
    "Ed Yardeni, recording his analysis late on a Sunday evening — he titled the session 'Between Iran and a Hard Place' — noted the asymmetry with characteristic calm: the United States had significant economic resilience. But resilience is not immunity. And the political pressure from rising gasoline prices was already building.",
    "Wide shot of a home study at night, bookshelves lining the walls behind an empty desk chair, a single lamp creating a pool of warm light, framed documents on the walls, a cup of coffee steaming, the intimate workspace of serious analysis")
act1_clips.append(c)

c = clip(257,
    "David Lin's interview with former Colonel Hal Kempfer captured the military dimension that the financial community was struggling to price: Iran had been developing its drone and missile arsenals for years precisely for this scenario. The war's second week might look very different from its first.",
    "Aerial drone shot slowly circling an airbase from altitude, aircraft on the tarmac visible as small shapes, the operational infrastructure of air power, the runway as the literal line between policy and consequence")
act1_clips.append(c)

c = clip(193,
    "And then, on March third, the data arrived that would define the crisis for historians, economists, and investors for years to come. From maritime tracking services monitoring the Strait of Hormuz: ship traffic had collapsed in a way that defied precedent.",
    "Dramatic aerial shot directly over the narrow strait between two landmasses, the water perfectly still and empty where dozens of vessels should be moving, eerie absence of the traffic that normally defines the chokepoint")
act1_clips.append(c)

c = clip(257,
    "According to Projekt 100X, drawing on AIS vessel tracking data, the Strait of Hormuz had gone from one hundred and thirty-eight ships per day — the normal baseline — to just four. A ninety-four percent reduction. The world's most important maritime chokepoint had effectively closed.",
    "Extreme wide drone shot over a vast empty harbor at dawn, enormous container ship berths sitting completely empty, cranes still, water flat and glassy, the scale of the emptiness only comprehensible from altitude")
act1_clips.append(c)

c = clip(193,
    "Four ships. Where a hundred and thirty-eight had been. The global maritime insurance market had effectively repriced war risk premiums to a level that made most voyages economically unviable. Owners were choosing to anchor and wait rather than risk their vessels and their crews.",
    "Cinematic wide aerial shot of a large anchorage where dozens of tankers sit motionless in calm water, anchors down, waiting, the visual of a system in suspension, the economic cost of inaction accumulating invisibly")
act1_clips.append(c)

c = clip(257,
    "Through that narrow waterway, twenty percent of the world's oil flows every single day. Twenty percent of global LNG. The petrochemical feedstocks that become fertilizers. The naphtha and propane and butane that feed refineries from Rotterdam to Tokyo. The jugular vein of the global economy had been cut.",
    "Cinematic slow-motion shot of a massive LNG tanker underway at full speed through blue open ocean, the enormous vessel gleaming white in strong sunlight, bow wave spreading wide, the scale conveyed by comparison to a distant escort vessel")
act1_clips.append(c)

c = clip(193,
    "The strait is twenty-one miles wide at its narrowest point. Since the 1980s tanker wars, every American president had maintained that keeping it open was a vital national interest. For decades, that commitment had been sufficient deterrence. Not anymore.",
    "Tight cinematic shot of water rushing through a narrow channel between two rocky outcroppings, current powerful and fast, white foam against dark rock, the water disappearing into shadow around a bend, the geography of vulnerability")
act1_clips.append(c)

# ============================================================
# ACT II: OIL SHOCK
# Target: ~55 clips, ~2,200 words
# ============================================================
act2_clips = []

c = clip(257,
    "Act Two. The Oil Shock. A thirty-five percent surge in less than a week. A hundred and fifteen dollars of Brent. And an LNG market that the word unprecedented was barely adequate to describe.",
    "Sweeping aerial drone shot over an oil refinery complex at dusk, flames from multiple flare stacks burning bright orange against a darkening purple sky, vast industrial structures stretching to the horizon, steam clouds drifting slowly eastward")
act2_clips.append(c)

c = clip(257,
    "The moment the Hormuz closure was confirmed by satellite imagery on March third, the oil market moved in ways traders had not seen since 2008. Brent crude, which had been trading around eighty-five dollars per barrel, gapped up through ninety, through one hundred, through one-ten in the space of seventy-two hours.",
    "Extreme close-up of a pressure gauge on a massive steel pipe, the needle climbing rapidly into the red zone, industrial grease and grime on the metal surface, ambient steam blurring the background through the heat shimmer")
act2_clips.append(c)

c = clip(193,
    "Within seventy-two hours of the closure confirmation, Brent had reached one hundred and fifteen dollars. At peak intraday trading, it touched one hundred and twenty. A thirty-five percent surge in less than a week. The speed of the move shattered institutional risk models across the global financial system.",
    "Cinematic shot of crude oil pouring from a large industrial pipe into a dark holding pool below, the black viscous liquid catching harsh industrial light, iridescent rainbow swirls forming on the surface as it settles, slow motion, beautiful and ominous")
act2_clips.append(c)

c = clip(257,
    "Maggie Lake, anchoring Talking Markets with her characteristic measured authority, posed the question that had become inescapable in every financial discussion: Is one hundred dollars oil the new normal? The answer from every analyst she interviewed was not just yes — but that one hundred dollars might be the floor, not the ceiling.",
    "Wide cinematic shot looking up at a towering oil drilling platform from deck level, steel lattice disappearing into a grey overcast sky, workers in hard hats small against the industrial scale, seabirds circling far above in the cold air")
act2_clips.append(c)

c = clip(193,
    "Her Talking Markets colleague Ian Winer of Center 15 Capital introduced a metric that captured the military economics: a twenty-thousand-dollar drone versus a three-million-dollar interceptor missile. 'The math doesn't work,' he said flatly. Iran could saturate US and Israeli air defenses at a fraction of the cost of defending against it.",
    "Close-up of a scale balance, a small light object on one side, a much heavier object on the other, the arms at a dramatic angle, the visual metaphor of asymmetric warfare economics")
act2_clips.append(c)

c = clip(257,
    "The physics of the rerouting crisis were brutal. You cannot redirect twenty percent of global oil supply through alternative passages without adding eleven to fourteen days to every journey from the Gulf to Europe via the Cape of Good Hope. Each extra day at sea is a day of operating cost, insurance cost, and opportunity cost.",
    "Dramatic wide shot of a massive oil tanker navigating around a rocky cape in rough seas, waves crashing against the hull, spray exploding upward, the ship straining visibly against the ocean swell, the physical reality of alternative routing")
act2_clips.append(c)

c = clip(193,
    "As Market Insider's analysis documented, tanker charter rates had already exploded. Rates that were one hundred and thirty thousand dollars per day just two weeks earlier were now four hundred thousand dollars a day. The Aframax and Suezmax classes, which do the workhorse carrying of the global oil trade, were in even more extreme demand.",
    "Close-up of a ship captain's hands navigating chart tables, thick fingers tracing a course across nautical maps, the charts weighted down at corners, compass and parallel rulers visible, the calculation of a longer, more expensive route")
act2_clips.append(c)

c = clip(257,
    "Those costs — every dollar of every extra day of every extra mile — were not absorbed by the shipping companies or the oil majors. They were passed through, as all energy costs ultimately are, to every driver, every farmer, every factory, every household. The oil shock was not an abstraction. It was an energy tax on the entire global economy.",
    "Slow motion close-up of a fuel pump nozzle entering a vehicle tank, the digital display numbers blurring upward, a hand gripping the handle firmly, a gas station canopy reflected in the car's metallic surface, the consumer interface of global commodity pricing")
act2_clips.append(c)

c = clip(193,
    "The food price dimension of the Hormuz closure deserved particular attention and received too little. The strait carries not just energy commodities, but the feedstocks of the fertilizer industry — ammonia precursors, urea, phosphate — that underpin global agricultural production.",
    "Cinematic wide shot of a vast wheat field in golden afternoon light, stalks bending gently in a warm breeze, the field stretching to a low horizon under a pale blue sky, solitary grain elevator visible in the far distance, the dependency hidden in the beauty")
act2_clips.append(c)

c = clip(257,
    "As Market Insider's economist put it: 'A closed oil route could hit food prices.' This was not speculation. It was the well-documented transmission mechanism from energy to fertilizer to food yield to consumer price. The 2026 growing season was already planted. The 2027 season was now in jeopardy if the closure extended through the spring planting window.",
    "Aerial drone shot slowly moving over a patchwork of agricultural fields from high altitude, geometric shapes of different crops in greens and yellows, irrigation channels glinting silver, the scale of global agriculture and its petrochemical dependencies")
act2_clips.append(c)

c = clip(193,
    "But oil was only the first shock. The second arrived with even greater force: liquefied natural gas. LNG. The fuel that heats European homes in winter, powers industrial facilities across Asia, and had become the cornerstone of Europe's energy security strategy after the Ukraine conflict.",
    "Wide aerial shot of an LNG export terminal at night, massive spherical storage tanks glowing under industrial floodlights, loading arms extended over a docked tanker, steam clouds rising into the dark sky, the infrastructure of liquid energy")
act2_clips.append(c)

c = clip(257,
    "Twenty percent of global LNG supply was now offline. QatarEnergy, the world's largest LNG exporter, had declared force majeure on March fifth. The Ras Laffan industrial city — the largest concentration of LNG production capacity on earth — had effectively suspended deliveries.",
    "Dramatic aerial shot of the Qatar coastline at dusk, vast industrial LNG facilities stretching along the shore, massive cryogenic storage tanks in rows, the Persian Gulf shimmering orange-gold in the setting sun, the scale of the production facility visible from altitude")
act2_clips.append(c)

c = clip(193,
    "LNG spot prices surged one hundred and thirty-seven percent in five days. Not five months. Five days. The speed of the move shattered every historical precedent for LNG price adjustment. European benchmark prices, which had been at thirty dollars per million BTU, touched seventy-one in intraday trading.",
    "Cinematic close-up of industrial natural gas flames burning at a processing facility, the blue-orange fire in perfect focus against a blurred refinery background, the raw power of energy made visible in the controlled combustion")
act2_clips.append(c)

c = clip(257,
    "Force majeure. Two words that, in contract law, release a party from its obligations when extraordinary circumstances beyond its control make performance impossible. When QatarEnergy invoked them, the message to global energy markets was clear: the supply certainty that European energy planning had been built on had evaporated.",
    "Close-up of formal legal documents on a conference table, a fountain pen resting beside them, hands visible at the edge of frame, the text obscured by angle, heavy signet ring glinting under boardroom lights, the weight of contractual failure")
act2_clips.append(c)

c = clip(193,
    "European energy ministers convened emergency sessions. Germany activated its gas emergency framework. France, which had been managing its nuclear fleet capacity, found its LNG import terminals suddenly processing orders with no corresponding deliveries.",
    "Dramatic wide shot of a European government building at night, lights blazing in the upper floors where emergency meetings were underway, the cold street below quiet, a few government cars visible, the architecture of crisis management")
act2_clips.append(c)

c = clip(257,
    "The United Kingdom, which had been importing roughly forty percent of its gas supply and was simultaneously managing the emerging private credit crisis, found itself at a dangerous intersection. Energy costs were spiking just as credit conditions were tightening. This was the precise combination that business investment models had always identified as maximally damaging.",
    "Cinematic wide shot of a British gas network infrastructure hub, pipes and valves and monitoring equipment in an industrial setting, a technician checking readings, the critical infrastructure of a nation's heating supply")
act2_clips.append(c)

c = clip(193,
    "The geopolitical logic behind the Hormuz closure was not irrational. Iran could not match US military firepower. But it had always held this single card: the chokepoint. With its leadership killed, its successor government had nothing left to lose and everything to gain by imposing maximum pain.",
    "Wide low-angle shot looking along the deck of a navy destroyer at sea, grey hull cutting through choppy dark water, radar array spinning on the superstructure, an overcast sky pressing down on the scene, the weight of military deterrence")
act2_clips.append(c)

c = clip(257,
    "Joe Blogs, reporting from his UK vantage point, documented the extraordinary step that the United States had taken: allowing additional Russian oil to be sold on global markets, through an emergency waiver from its own sanctions regime, in order to prevent the global energy market from completely seizing up.",
    "Cinematic aerial wide shot of a trans-Siberian oil pipeline corridor cutting through dense boreal forest, the silver pipeline a straight line vanishing to a point in both directions, frost on the birch trees, late winter light on the snow")
act2_clips.append(c)

c = clip(193,
    "The irony was historically extraordinary. A war ostensibly fought to reshape the Middle East had, within two weeks, forced the United States to loosen sanctions on Russia — generating a massive oil price windfall for a government that had spent three years under maximum economic pressure. One commenter's observation resonated: 'Oil to $200 a barrel, mark my words.'",
    "Close-up of oil rig machinery in full operation, heavy steel drill components turning and grinding, grease glistening under work lights, the weight and power of extraction visible in every bolt and weld, the geology of geopolitics")
act2_clips.append(c)

c = clip(257,
    "Ed Yardeni's structural insight cut through the noise: the shale revolution had made the United States a net oil exporter. Higher prices were a windfall for American domestic producers — Texas, North Dakota, the Permian Basin. This was not 1973. But the benefits and burdens of the shock were distributed with profound inequality.",
    "Aerial drone shot over a shale oil field in the Texas Permian Basin at golden hour, dozens of pump jacks operating across flat scrubland, each one nodding in rhythmic motion, the modern oil patch bathed in warm late afternoon light")
act2_clips.append(c)

c = clip(193,
    "Energy company shareholders — concentrated at the very top of the wealth distribution — would profit from every dollar of price increase. Every American who drives a car to work, heats a home, or operates any business with transportation costs would pay. The oil shock was a wealth transfer, operating in real time, from the working economy to the asset economy.",
    "Cinematic slow tracking shot through an upscale residential neighborhood at dusk, large houses lit from within, luxury vehicles in driveways, the transition into a working-class neighborhood with smaller homes and older cars, the spatial geography of the K-shaped economy")
act2_clips.append(c)

c = clip(257,
    "Steve Hanke, economist and Johns Hopkins professor, appeared on David Lin's channel with a historical warning drawn from decades of studying monetary systems: oil at one hundred and eight dollars at time of recording. If we hit one hundred and forty dollars, 'two thousand and eight will look like a picnic.' One commenter replied: 'IT WILL BE THE 70s AGAIN WITHOUT ALL THE GOOD MUSIC.'",
    "Dramatic close-up of a thermometer in extreme close-up, the red mercury column rising steadily past critical marks, the glass tube vibrating slightly in an industrial context, the universal instrument of dangerous temperature")
act2_clips.append(c)

c = clip(193,
    "Polymarket, the prediction market platform, was reflecting institutional hedging activity in its crude oil contracts. A four percent chance of two hundred dollar oil by end of March had attracted twenty-five million dollars in volume. As Bankless noted, that level of volume indicated not retail speculation but genuine institutional hedging of tail risk.",
    "Overhead close-up of a green felt gaming table with betting chips arranged in complex patterns, a hand hovering over the arrangement, calculating risk and odds, dramatic overhead single-source light creating sharp shadows on the felt")
act2_clips.append(c)

c = clip(257,
    "The Adam Taggart Thoughtful Money interview with Michael Lebowitz captured the market reaction with precision. Markets had been trying to price in the war's economic impact since day one, Lebowitz argued, but the range of outcomes was so wide that any single price represented not a consensus but a guess.",
    "Wide cinematic shot of a stock exchange floor with traders active in multiple pits simultaneously, the visual complexity of price discovery in progress, overhead crane shot revealing the organized chaos of a market struggling to find equilibrium")
act2_clips.append(c)

c = clip(193,
    "Retail investors in the comment sections were already acting. 'Most people are buying oil stock,' one Thoughtful Money commenter wrote. 'TPET stock has doubled in a week.' The crisis was creating its first wave of retail winners alongside its far broader wave of victims.",
    "Close-up of a hand scrolling through a brokerage app on a smartphone, green numbers and portfolio values visible in the warm screen glow, the face of the viewer reflected faintly in the glass, intent and calculating")
act2_clips.append(c)

c = clip(257,
    "Meanwhile, the refinery problem was adding another layer of complexity. Not all crude oil is interchangeable. The refineries of Europe and the United States are configured to process specific grades of crude — primarily the light, sweet grades from the Gulf. Rerouted crude from West Africa or the Americas was often the wrong grade, requiring expensive refinery modifications.",
    "Wide shot of a massive oil refinery complex, the towers and reactors and distillation columns visible in their complexity, the specific chemistry of petroleum processing made architectural, a controlled burn off flare visible above")
act2_clips.append(c)

c = clip(193,
    "Diesel was, as one Eurodollar University commenter noted with urgency, the critical fuel that was receiving insufficient attention: 'Without diesel the world turns into Mad Max. Getting supplies of diesel is what people will start to scramble for.' Agriculture, trucking, shipping, construction — every real economy activity runs on diesel.",
    "Cinematic close-up of a large diesel engine running in an industrial application, the mechanical reciprocation of pistons visible through an inspection panel, the controlled explosions of the internal combustion cycle captured in slow motion")
act2_clips.append(c)

c = clip(257,
    "The longer-term question haunting energy economists was duration. Diplomatic efforts to open the strait required either a ceasefire that Iran's new leadership had no incentive to accept, or a military clearing operation that risked an escalation no one wanted to contemplate. The market was pricing in three to six months of disruption. History suggested even that might be optimistic.",
    "Sweeping aerial shot of a busy shipping lane in open ocean, a dozen tankers visible at different distances stretching to the horizon, their wakes intersecting in the blue water, the ordinary miracle of global commerce captured in its fragility")
act2_clips.append(c)

c = clip(193,
    "The ships anchored in the Gulf of Oman and the Arabian Sea — one hundred and thirty-four of them that had been transiting daily just two weeks before — were not simply redirected. They were waiting. Owners watching the conflict, calculating insurance costs, waiting for a signal that had not yet come.",
    "Wide aerial shot of a massive anchorage area where dozens of large vessels sit motionless in calm turquoise water, anchors down, cargo not moving, the visual of suspended trade, the economic cost of waiting accumulating invisibly")
act2_clips.append(c)

c = clip(257,
    "Soar Financially's analysis with Dr. Komal Sri-Kumar, titled 'WAR ECONOMY: Stagflation Hits in 2026, Gold vs Dollar,' drew the explicit connection: the oil shock was not a temporary disruption but a structural repricing. The cost of energy — which had been suppressed for years by shale abundance and low geopolitical risk premiums — was being permanently repriced upward.",
    "Cinematic wide shot of a major international energy conference room, empty chairs around a long table, flags of oil-producing nations arranged behind the empty seats, the visual of an absent consensus")
act2_clips.append(c)

c = clip(193,
    "The oil shock of March 2026 had one dimension that made it qualitatively different from all prior oil shocks: it arrived simultaneously with a financial crisis that had nothing to do with oil. A crisis that had been quietly building in the shadows of the private credit market. A crisis that made the Fed's response options far more constrained than in any previous oil shock.",
    "Slow zoom into the dark glass facade of a massive financial headquarters tower in a city at night, reflections of the city skyline distorted in the glass, a single window lit on an upper floor, the rest dark, the financial system at work in silence")
act2_clips.append(c)

c = clip(257,
    "The intersection of the energy crisis and the credit crisis was the specific compound that economists had given a new name: Warflation. It was this compound — not either crisis individually — that represented the genuine systemic threat. And it was warflation that the Federal Reserve, as we will see in Act Four, had absolutely no good tool to fight.",
    "Dramatic wide aerial shot of a storm system over an ocean, two separate storm cells visible from altitude, both rotating, moving toward each other on a collision course, the vast scale of atmospheric violence captured from above")
act2_clips.append(c)

# ============================================================
# ACT III: PRIVATE CREDIT CRISIS
# Target: ~55 clips, ~2,200 words
# ============================================================
act3_clips = []

c = clip(257,
    "Act Three. The Private Credit Crisis. Three hundred billion dollars in contagion risk. A shadow banking system that had grown to over two trillion dollars globally, operating with minimal regulatory oversight, had begun to fracture.",
    "Slow dolly shot through a darkened bank vault corridor, polished steel safe deposit boxes receding into shadow on both sides, security lights creating a dim amber glow, silence and stillness suggesting enormous hidden value held in the dark")
act3_clips.append(c)

c = clip(257,
    "To understand what happened in private credit in March 2026, you need to understand what private credit actually is. It is money lent directly from large institutional investors — pension funds, endowments, sovereign wealth funds, family offices — to private companies that cannot access the public bond market.",
    "Cinematic wide shot looking down a long corridor of a modern financial institution, glass-walled offices visible on both sides, people in formal attire moving silently behind the glass, the institutional machinery of capital allocation")
act3_clips.append(c)

c = clip(193,
    "Unlike public bonds — which trade on exchanges, are priced daily, and must conform to disclosure standards — private credit loans are held on fund balance sheets at valuations that fund managers have significant discretion to set. This opacity, as Stoic Finance documented in detail, was both the appeal and the danger.",
    "Close-up of a magnifying glass hovering over printed fine-print contract language on a white page, the text in soft focus beneath the lens, the glass catching overhead light, the act of scrutiny itself made visual")
act3_clips.append(c)

c = clip(257,
    "The private credit market grew explosively after the 2008 financial crisis, when banking regulations — specifically Basel III capital requirements — forced traditional lenders to reduce risk-weighted assets. Private credit funds stepped into the gap. They were unregulated, offered higher yields, and operated with the freedom of unaccountable opacity.",
    "Aerial drone shot over a gleaming financial district at golden hour, towers of glass and steel casting long shadows, the geometry of modern capital management visible from above, the city humming with unseen transactions between institutional actors")
act3_clips.append(c)

c = clip(193,
    "What made private credit attractive to the pension funds and endowments that became its primary investors was yield. When interest rates were near zero for over a decade, private credit funds consistently offered six, seven, even eight percent returns. In a world starved of income, that was irresistible.",
    "Close-up of a thick printed investment prospectus on a conference table, performance numbers in columns visible but not legible, hands turning pages, a fountain pen held in one hand, the ritual of investment due diligence")
act3_clips.append(c)

c = clip(257,
    "What made private credit dangerous was what Stoic Finance called the 'opaque private credit environment that incentivizes lies and deception.' When fund managers mark their own loans, the incentive to declare a struggling borrower a healthy one — to avoid triggering redemptions — is structural. The problem was baked into the model.",
    "Cinematic wide shot of an auditor's office, one wall of filing cabinets, another wall of binders, a single desk lamp illuminating stacks of documents, the forensic archaeology of financial accounts")
act3_clips.append(c)

c = clip(193,
    "The first warning shot came in February, even before the Iran war began. BlackRock's twenty-six billion dollar private credit fund began receiving an unusual volume of redemption requests. Institutional investors, sensing that higher interest rates had created stress in the underlying loan portfolios, wanted their money back.",
    "Cinematic wide shot of the BlackRock headquarters building in Manhattan from the street, the imposing stone and glass facade, pedestrians rushing past below in the financial district, the scale of institutional asset management made architectural")
act3_clips.append(c)

c = clip(257,
    "BlackRock honored some redemptions. Then, in early March, it began denying the rest. 'Redemptions limited.' In private credit fund terms, this means the fund has determined it cannot sell assets fast enough to return investor capital without damaging the remaining portfolio. In plain terms: the money is locked up.",
    "Dramatic close-up of an imposing wooden door with a polished brass lock, the door closed and locked, hard shadows from overhead light, the weight of institutional refusal, a CLOSED placard just visible at the edge of frame")
act3_clips.append(c)

c = clip(193,
    "The dominos then fell with extraordinary speed. As Eurodollar University's Jeffrey Snider reported: 'Now it's Morgan Stanley's turn. Yesterday it was Cliffwater. Before that it was BlackRock and Blackstone and of course Blue Owl.' Morgan Stanley's eight billion dollar North Haven private income fund was the latest to gate redemptions.",
    "Slow zoom into the Morgan Stanley logo on a glass building facade in the financial district, the reflection of the city street in the curved glass surface, the institution's scale implied by the perspective and the surrounding buildings")
act3_clips.append(c)

c = clip(257,
    "The sequence: BlackRock twenty-six billion. Blackstone twenty-one billion. Blue Owl. Cliffwater thirty-three billion. Morgan Stanley eight billion. Each new name in the headline created a fresh wave of redemption requests across the entire private credit sector. Every institution with private credit exposure was simultaneously trying to reduce it.",
    "Dramatic cinematic shot of a line of dominoes arranged in a long curve on a dark table, the first falling in slow motion, the cascade beginning, each piece toppling the next in succession, extreme close-up, slow motion capture")
act3_clips.append(c)

c = clip(193,
    "Stoic Finance's analysis identified the contagion mechanism with precision: three hundred billion dollars is not the direct loss figure. It is the exposure that triggers further failures. Each denied redemption means an investor who cannot meet their own obligations — their own redemptions to their own investors, their own margin calls, their own debt service.",
    "Slow motion water drip falling into a still pool, the perfect concentric rings spreading outward from the impact point, each ring generating the next, the pattern of contagion made beautiful and visible in the physics of surface tension")
act3_clips.append(c)

c = clip(257,
    "This is how financial contagion spreads through a system. Not through direct loss from the original event, but through the chain of obligations that can no longer be met because the original capital is frozen. In 2008, it was mortgage-backed securities. In 2026, it was private credit. The mechanism was identical.",
    "Wide shot of an emergency plumbing repair in a building corridor, a section of pipe burst open, water spreading across the floor in multiple directions, maintenance workers arriving from both ends of the corridor, the lateral spread of damage")
act3_clips.append(c)

c = clip(193,
    "Eurodollar University noted the parallel to 2008 was precise — and haunting. In 2007-2008, there had been an oil price shock even bigger than the current one. It was overshadowed by the subsequent deflationary calamity. But before that deflationary calamity arrived, the credit system had been quietly dying for months, while central banks focused on inflation.",
    "Wide shot of a major urban intersection during rush hour, streams of vehicles flowing in four directions, normal from above, but a cinematic pull-back reveals one lane completely blocked, the backup spreading invisibly into the surrounding system")
act3_clips.append(c)

c = clip(257,
    "The mechanism connecting the two crises was elegant in its devastation. Private credit loans made during the zero-interest-rate era were issued at relatively thin spreads. When rates rose to five, six, seven percent, the borrowers — private companies with floating-rate debt — found their debt service costs doubling or tripling. Some could manage. Some could not.",
    "Close-up of a heavy iron chain under tension, the links stretching and straining, slight corrosion visible at the stress points, the implied weight enormous, a single link beginning to deform at the weakest point, the physics of structural failure")
act3_clips.append(c)

c = clip(193,
    "The borrowers who could not — the ones who had borrowed on the assumption that rates would remain low, who had used the capital for acquisitions, for share buybacks, for speculative real estate — were now negotiating with their lenders in private. Out of sight. At valuations that existed on paper only.",
    "Slow tracking shot through an empty corporate headquarters space after hours, framed company photos on the walls, plaques with past milestones, the artifacts of a business that may not exist much longer, the archaeological record of corporate confidence")
act3_clips.append(c)

c = clip(257,
    "The British dimension of the crisis received devastating coverage from Stoic Finance. 'Private Credit Collapses British Economy as Contagion Spreads Globally' was the headline. Two British firms had collapsed in the space of a week, for precisely the same reason as their American counterparts: too much debt in an environment of rising rates and tightening credit conditions.",
    "Wide aerial shot of the City of London financial district at dusk, the Gherkin and Walkie-Talkie towers illuminated against a darkening sky, the Thames winding through the frame, the ancient trading center of a kingdom navigating a new financial crisis")
act3_clips.append(c)

c = clip(193,
    "The UK private credit market had expanded faster than almost anywhere else in the world, relative to the size of the underlying economy. British pension funds had been among the most aggressive allocators to the sector. They were drawn by the yield in a post-Brexit environment of structural economic underperformance.",
    "Cinematic wide shot of a traditional British high street in a provincial town, a bank branch building prominent, a red double-decker bus passing, people walking with purpose, the quiet financial dependency of ordinary lives on institutions they cannot see")
act3_clips.append(c)

c = clip(257,
    "The pension fund exposure mattered enormously because of who ultimately bore the risk. Behind every pension fund redemption that was denied was a retiree or near-retiree whose retirement savings were locked. The private credit freeze was not an abstraction of the financial system. It was real money belonging to real people in their most financially vulnerable years.",
    "Close-up of an elderly woman's hands carefully sorting through financial documents on a kitchen table, reading glasses on, a pension statement among the papers, the warm domestic light of a home, the personal scale of institutional finance")
act3_clips.append(c)

c = clip(193,
    "When those funds restricted redemptions, the ripple effects reached quickly into the real economy. British companies that had been planning to refinance their private credit debt found their access suddenly cut. Investment plans were cancelled. Expansions were frozen. Jobs that would have been created were not. The credit contraction was translating into real economic activity in real time.",
    "Slow dolly shot through a half-built commercial construction site, scaffolding up but work halted, tools laid down on covered materials, hard hats on a table but no workers, late afternoon light catching the dust suspended in the still air")
act3_clips.append(c)

c = clip(257,
    "Chris Irons, speaking to Adam Taggart on Thoughtful Money, described the private credit meltdown threat with particular clarity. The problem was not just the direct losses. It was the uncertainty. Nobody knew which fund would gate next. Nobody knew what the true value of their private credit holdings actually was. And uncertainty of that kind freezes the entire financial system.",
    "Wide shot of a fog-covered valley in early morning, the landscape completely obscured, only the tops of mountains visible above the mist, the familiar made alien by the loss of visibility, the uncertainty of navigation without reference points")
act3_clips.append(c)

c = clip(193,
    "The YouTube commentary was furious and prescient. 'These institutions have ZERO oversight, regulation, or fiduciary obligations. Private profits but public losses.' Another viewer: 'They're operating like a bank without the regulations banks must follow. There is no security for their funds.' These were not financial experts. They were citizens who had seen this pattern before.",
    "Wide shot of ordinary people in a bank branch queue, waiting patiently at roped barriers, a teller window in the background, the timeless image of small depositors and their implicit trust in large institutions")
act3_clips.append(c)

c = clip(257,
    "One commenter on Stoic Finance offered what may be the most penetrating single observation in the entire crisis: 'If only we had an example of lending money to people who can't pay it back.' The parallel to 2008's subprime mortgage crisis was not metaphorical. It was structural. The same dynamics, applied to different financial instruments, producing the same instabilities.",
    "Wide aerial shot of a suburban housing development, rows of identical houses in neat grids, some with for-sale signs, some occupied, the geography of the 2008 crisis reinterpreted for a new generation of homeowners and borrowers")
act3_clips.append(c)

c = clip(193,
    "Ken McElroy, approaching the crisis from a real estate perspective, explained how liquidity worked — or stopped working — in practice: 'All money comes from Main Street. Main Street puts money into a bank. Main Street puts money into a pension. Main Street puts money into a private credit fund. And when that money can't get back to Main Street, everything seizes up.'",
    "Cinematic close-up of water flowing from a tap into a glass, the simple physics of a working liquid system, then a hand slowly closing the tap, the flow diminishing to a trickle, the visual metaphor of credit tightening")
act3_clips.append(c)

c = clip(257,
    "The Ken McElroy observation about liquidity tightening was particularly important for real estate. Commercial real estate had been under pressure since the post-pandemic work-from-home shift emptied office buildings. Now, with private credit gating, the refinancing market for commercial properties — which typically relied on private credit as a bridge to bank financing — had effectively frozen.",
    "Aerial drone shot over an urban business district, office towers visible from above, car parks mostly empty, the visual evidence of changed work patterns inscribed in the urban geography, the private credit crisis translating into emptiness")
act3_clips.append(c)

c = clip(193,
    "The compound nature of the crisis was what made it so dangerous. Stoic Finance had identified it correctly: an oil shock alone, the economy can absorb. A private credit crisis alone, regulators can manage. Both together, while the Federal Reserve has its hands tied by stagflation, while the UK economy is under siege from both? That is where systemic risk lives.",
    "Dramatic wide aerial shot of two rivers in flood converging at a town, the floodwaters from both streams meeting, doubling the inundation, the town visible at the confluence of the two crises, aerial shot conveying the overwhelming compound force")
act3_clips.append(c)

c = clip(257,
    "By the second week of March, the compound crisis had a name on financial channels: Warflation. Soar Financially's analysis with Dr. Steve Keen titled 'WARFLATION: Oil Shock Plus Debt Crisis Could Break the Economy' captured the synthesis. Keen's argument: the three simultaneous pressures — supply shock, credit contraction, and debt deflation — were mutually reinforcing in ways that standard economic models had no mechanism to address.",
    "Cinematic wide shot of a pressure cooker on a stove at maximum heat, steam escaping from three different release valves simultaneously, the pot vibrating, the kitchen blurred behind, the physics of multiple simultaneous pressures exceeding design capacity")
act3_clips.append(c)

c = clip(193,
    "Bankless captured the synthesis for their crypto-native audience with characteristic directness: 'Three things we're paying attention to: oil, jobs, private credit, and how all those things affect crypto.' In the chaotic era — the phrase Vitalik Buterin had used that week — every crisis was connected to every other crisis. The financial system had become a single, densely interconnected entity.",
    "Sweeping aerial drone shot over a major city at dusk in a light rain, streets reflecting red and white light trails of traffic, the city as a complex adaptive system, interdependent and fragile in ways invisible from the street level")
act3_clips.append(c)

# ============================================================
# ACT IV: THE FED'S IMPOSSIBLE BIND
# Target: ~50 clips, ~2,000 words
# ============================================================
act4_clips = []

c = clip(257,
    "Act Four. The Federal Reserve. The Impossible Bind. Stagflation, the K-shaped economy, and the paralysis of the most powerful central bank in the world.",
    "Wide establishing shot of the Federal Reserve building in Washington DC at dusk, classical columns illuminated by ground lighting, the American flag visible on the roof, cars streaming past with light trails in the long exposure, the institution of last resort")
act4_clips.append(c)

c = clip(257,
    "The Federal Reserve has two mandates under the Federal Reserve Act of 1977: maximum employment and price stability. In March 2026, it was failing both simultaneously — and the tools available to fix one would worsen the other. This is stagflation. Economists study it with dread. Central bankers have no playbook for it.",
    "Close-up of a traditional scale balance, both pans in perfect equilibrium, then a single heavy object placed on one side, the balance tilting decisively, the mechanism swinging to a new resting point with no way to restore the original balance without removing the weight")
act4_clips.append(c)

c = clip(193,
    "The arithmetic was simple and brutal. Oil at one hundred and fifteen dollars per barrel adds approximately one and a half percentage points to core inflation within ninety days, through the input cost transmission mechanism. Pre-crisis CPI was already running at three point four percent. The Fed's target: two percent. The gap was widening with every passing week.",
    "Cinematic close-up of a precision measuring instrument with a needle moving steadily into a red zone, calibrated in fine gradations, an industrial setting, a hand hovering near but not touching a warning switch, the tension of a reading approaching the danger threshold")
act4_clips.append(c)

c = clip(257,
    "Martin Wolf explained the inflation transmission mechanism with the precision of thirty years of economic journalism: 'Being completely dependent on fuels imported through very dangerous places like the Strait of Hormuz is very problematic.' The mechanism is not complex. Energy goes into everything. When energy costs rise, everything costs more. When everything costs more, that is inflation.",
    "Wide shot of a vast logistics hub, hundreds of trucks lined up at loading docks, workers moving freight with electric pallet jacks, the entire system visible as an interconnected web of movement and cost, every component touched by energy prices")
act4_clips.append(c)

c = clip(193,
    "The Fed's response options were paralyzed from the moment the oil price began its ascent. Raise rates to fight inflation? The economy was already slowing. Unemployment was rising at the margin. Corporate debt service costs were rising. The private credit system was cracking. Rate hikes would add fuel to a credit fire.",
    "Slow motion close-up of a hand turning a combination lock, clicking through numbers with precision, the mechanism of control and constraint made physical, each click deliberate, each position irreversible until the combination is complete")
act4_clips.append(c)

c = clip(257,
    "Cut rates to stimulate growth? Oil-driven inflation was already threatening to spiral. Cutting rates with inflation rising would destroy the Fed's credibility — built at enormous cost over three years of aggressive hiking — trigger a dollar selloff, and potentially unleash a currency crisis on top of an energy crisis on top of a credit crisis.",
    "Dramatic wide shot of a dam with water spilling over the top, controlled overflow through multiple spillways, engineers visible on the walkway above, the tension between containment and release made viscerally physical in the roaring water")
act4_clips.append(c)

c = clip(193,
    "Hold rates steady? That option — by definition doing nothing while multiple crises compound — would satisfy neither mandate, preserve credibility in the short term, and guarantee deeper problems in the medium term.",
    "Cinematic overhead shot of a chess game mid-match, pieces arranged in a complex position, one player's king in a difficult position with no clear escape, any move leading to a worse position, the geometry of constraint")
act4_clips.append(c)

c = clip(257,
    "The Monetary Matters Network's discussions with both Martin Wolf and Joseph Stiglitz centered on this bind. Stiglitz, whose Nobel Prize in 2001 was partly awarded for work on information asymmetry in markets, pointed to what he saw as the deeper structural failure: the Federal Reserve's models assumed a relatively homogeneous economy. The actual American economy of 2026 was deeply bifurcated.",
    "Wide aerial shot of a neighborhood where large luxury homes on one side of a boulevard directly face modest older houses on the other, the economic divide rendered as stark visual geography visible from above")
act4_clips.append(c)

c = clip(193,
    "The K-shape: two groups, moving in opposite directions. The top ten percent — asset owners, stockholders, property owners, private credit investors — riding an endless wave of capital appreciation that persisted through even significant market corrections. The bottom ninety percent — workers, renters, debtors — experiencing something that felt, in their daily economic lives, indistinguishable from a severe recession.",
    "Cinematic wide shot of a luxury hotel entrance, valets parking expensive cars, guests with designer luggage, immediately cutting to a slow shot of a dollar store parking lot at dawn, working families loading modest purchases, the two economies side by side")
act4_clips.append(c)

c = clip(257,
    "Rosenberg Research had been documenting this divergence with rigorous data analysis for months before the war. Their conclusion was unambiguous: for the bottom ninety percent of American households by income, a recession was not approaching. It was already underway. Credit card delinquency rates were at multi-decade highs. Auto loan defaults were climbing. Food bank usage was at record levels.",
    "Close-up of hands sorting through a handful of credit cards on a kitchen table, a utility bill visible in the background, the quiet economic anxiety of a household working through its financial options, natural window light in the morning")
act4_clips.append(c)

c = clip(193,
    "The K-shaped economy created a specific and dangerous problem for the Federal Reserve. The policy tool — the interest rate — works through the credit channel. When the bottom ninety percent are already credit-stressed and the top ten percent are largely insulated from rate levels by their asset holdings, raising rates punishes the bottom and barely touches the top.",
    "Wide aerial shot of a residential neighborhood at dusk, houses with lights on, cars in driveways, the apparently uniform suburban landscape concealing an enormous range of individual financial situations")
act4_clips.append(c)

c = clip(257,
    "The payroll data had been flashing warning signs before the war. Negative revisions to prior months' job numbers — a pattern where the initially reported job creation is subsequently revised downward — had been appearing with quiet regularity in the monthly releases. This pattern historically precedes official recession declarations by three to six months.",
    "Slow zoom into an open laptop on a home desk, a spreadsheet of numbers visible but not legible, a coffee mug steaming beside it, the ambient hum of a home office in early morning, the individual investor trying to make sense of the data")
act4_clips.append(c)

c = clip(193,
    "Azul, the financial advisor with over twenty years of client-facing experience, had been warning his audience for months about rising unemployment and job losses. 'If you're like most of us, you're not ready for what's coming next,' he told his viewers. 'Rising unemployment, rising job losses, and potentially a lot of American jobs at risk.'",
    "Wide cinematic shot of an employment office exterior, a line of people waiting on the sidewalk, various ages, various attire, the universal waiting posture of economic dislocation, morning light on the queue")
act4_clips.append(c)

c = clip(257,
    "With the oil shock now adding to household energy costs, the consumer economy — which accounts for approximately seventy percent of US GDP — was under pressure from multiple directions simultaneously: higher energy costs, tighter credit conditions, and the accumulated weight of three years of above-target inflation that had already eroded real wages.",
    "Slow tracking shot through a suburban shopping mall, some stores busy, others visibly quieter, a few storefronts empty with paper in the windows, the retail economy showing the early signs of demand compression")
act4_clips.append(c)

c = clip(193,
    "Dr. Komal Sri-Kumar, speaking to Soar Financially, offered a historical comparison that cut to the heart of the dilemma: 'The stagflation scenario of 2026 is in some ways worse than the 1970s.' In the seventies, there was no twenty-six trillion dollar household debt overhang. There was no forty trillion dollar federal debt. The system then had genuine shock-absorbing capacity.",
    "Cinematic close-up of an electrical fuse box with every circuit breaker tripped to the off position, red indicators showing failure across the board, an electrician's hand moving across the panel, the problem replicated everywhere she looks")
act4_clips.append(c)

c = clip(257,
    "The bond market had been signaling distress in ways the equity market was slow to acknowledge. As one ITM Trading commenter observed: 'Iran War Triggers Inflation Fears as Bonds Start Failing.' The Treasury market — the deepest, most liquid financial market in the world — was showing signs of stress in the auction demand that should have been front-page news.",
    "Dramatic wide shot of an auction house in operation, paddles raised in a packed room, the auctioneer working through lots at speed, the mechanism of price discovery through competitive bidding, applied to the world's most important market")
act4_clips.append(c)

c = clip(193,
    "The Fed's Jerome Powell had spent years building inflation-fighting credibility. He had earned it by hiking rates aggressively in 2022 and 2023, inducing genuine pain in the economy to break inflation expectations. That credibility was now the only thing preventing an inflation expectations spiral. But credibility in central banking is not a renewable resource. It can be consumed — and once consumed, it is extraordinarily difficult to rebuild.",
    "Dramatic slow zoom into the Federal Reserve seal on a polished wood surface, the eagle and shield in bas-relief, the symbolism of institutional authority, warm light catching the carved detail, the weight of institutional credibility made physical")
act4_clips.append(c)

c = clip(257,
    "If oil-driven inflation persisted for six months, the public's inflation expectations — anchored with such difficulty through years of rate hikes — could become unanchored again. And unanchored inflation expectations are, in the theoretical framework of central banking, the nightmare from which there is no easy waking. Prices begin to rise not because of supply and demand, but because everyone expects them to rise.",
    "Slow motion close-up of a tightly wound spring held under compression by two metal plates, the stored energy visible in the geometry of the coil, the potential for release enormous and contained only by the slenderest margin of the restraining plates")
act4_clips.append(c)

c = clip(193,
    "The Econ Lessons explanation for retail investors was clear and correct: 'When energy prices rise, firms face higher production costs. If they pass those costs on, you get cost-push inflation. If they absorb them, you get profit compression. Either way, investment falls.' There is no version of this story where the real economy escapes unscathed.",
    "Wide cinematic shot of a small manufacturing plant floor, workers at stations, machinery running, but the camera slowly tracking past idle equipment and unused workstations, the visible slack of a contracting industrial economy")
act4_clips.append(c)

c = clip(257,
    "The European Central Bank faced a version of the same dilemma, complicated further by the energy supply crisis that was hitting European manufacturers with particular force. Germany — the industrial engine of the eurozone, a country that had already slipped into recession — was facing potential deindustrialization as energy costs rose beyond what its manufacturing sector could absorb.",
    "Cinematic wide shot of a German automotive manufacturing facility, robotic assembly lines visible, a floor manager watching the production process, the precision engineering of German industry in full operation but under cost pressure")
act4_clips.append(c)

c = clip(193,
    "Arthur Hayes made the ultimate argument about the Federal Reserve's trajectory in his Wealthion appearance: 'The Fed will always print money.' This was not analysis of the current moment but of the structural logic of the entire modern monetary system. A government with forty trillion dollars in debt cannot afford the interest rates required to fight inflation conventionally. Eventually, the math forces the choice.",
    "Cinematic wide shot of an industrial printing facility at night through large windows, enormous machines operating, the rhythm of mechanical production continuous, warm amber light inside, dark outside, the metaphor of monetary creation")
act4_clips.append(c)

c = clip(257,
    "Hayes' argument connected directly to the bear case for the dollar and the bull case for every hard asset. If the Fed will ultimately inflate, then gold is not speculative. Silver is not speculative. Bitcoin is not speculative. They are rational allocations for a world where the central bank's ultimate tool is the printing press.",
    "Wide aerial drone shot over the New York Federal Reserve building in lower Manhattan, the stolid limestone exterior unchanged since 1924, the financial district streets around it teeming with movement, the gold in the vault seventeen floors below the street")
act4_clips.append(c)

c = clip(193,
    "One Ken McElroy comment thread captured the retail investor's confusion about the interaction between Fed policy and liquidity: 'If the government does nothing and liquidity is tightening, we can have another situation where people can't borrow, people can't buy anything.' The circular logic of credit collapse had become common knowledge at the kitchen table level.",
    "Close-up of a single drop of water falling in extreme slow motion into a still pool, the impact creating a perfect crown of water, then the crown collapsing back, the cycle of impact and withdrawal capturing the rhythm of credit expansion and contraction")
act4_clips.append(c)

c = clip(257,
    "The ISM Manufacturing Index had been below fifty — indicating contraction — for seventeen of the past twenty months before the war began. The Services sector, which employs the majority of Americans, was barely in expansion. Into this already-weakening economic backdrop, the oil shock was arriving like a torpedo into the hull of a ship that was already taking on water.",
    "Cinematic wide shot of a factory floor with production lines running but at reduced speed, some stations idle, overhead lights at reduced brightness, a supervisor studying a clipboard, the visual of managed decline in an industrial setting")
act4_clips.append(c)

# ============================================================
# ACT V: SAFE HAVENS
# Target: ~60 clips, ~2,500 words
# ============================================================
act5_clips = []

c = clip(257,
    "Act Five. The Safe Havens. Gold above five thousand dollars. Silver at ninety. Bitcoin at seventy thousand. And the question that every investor was asking: where do you put your money when the system itself is the risk?",
    "Cinematic extreme close-up of a single gold coin on a dark velvet surface, a beam of light catching the surface relief in perfect detail, the precious metal warm and luminous against the surrounding darkness, the simple ancient answer to a complex modern question")
act5_clips.append(c)

c = clip(257,
    "Gold had crossed four thousand dollars per ounce in late 2025. The crossing of that threshold — which had seemed unthinkable to mainstream financial analysts just two years before — had triggered a wave of reassessment across institutional portfolios. By the first week of March 2026, gold was trading above five thousand. The move was real.",
    "Wide aerial shot of a gold mine open pit operation, the massive terraced walls of earth spiraling downward in a double helix, heavy equipment visible as tiny specks on the benches, the scale of the extraction operation only comprehensible from altitude")
act5_clips.append(c)

c = clip(193,
    "Ronald Stoeferle of Incrementum — whose annual In Gold We Trust report is the most comprehensive and widely read institutional analysis of the gold market published anywhere in the world — had forecast five thousand two hundred dollar gold in his 2025 report. Speaking to Soar Financially, he stood by that target.",
    "Close-up of weathered hands carefully examining a gold coin through a jeweler's loupe, the magnified detail of the coin visible in the lens, the patient scrutiny of someone who has studied the metal for decades, warm natural desk light")
act5_clips.append(c)

c = clip(257,
    "Stoeferle's forecast rested on three structural pillars. First: central bank gold buying. Led by China, India, Russia, and Turkey, with participation from dozens of smaller emerging market central banks, institutional gold buying had reached its highest level on record for three consecutive years. These buyers were not speculating. They were making a considered judgment about dollar reliability.",
    "Cinematic wide shot of an enormous bank vault interior, rows of gold bars stacked precisely on shelves, a security guard visible in the far background, the sheer weight and density of stored sovereign wealth, warm vault lighting on the metal")
act5_clips.append(c)

c = clip(193,
    "The second pillar: dollar debasement. The United States government's fiscal trajectory — a trillion-dollar annual deficit that showed no path to reduction — was a structural argument for gold that required no geopolitical trigger. The trigger had simply accelerated the timeline.",
    "Aerial drone shot over the US Capitol building at dusk, the dome illuminated, the formal architecture of American governance, the building where spending decisions accumulate into the debt that gold seeks to hedge against")
act5_clips.append(c)

c = clip(257,
    "The third pillar: the ending of gold's contrarian status. Stoeferle's most important observation to Soar Financially was this: gold is no longer contrarian. For decades, owning gold was the eccentric choice — the domain of survivalists and conspiracy theorists and Austrian economists dismissed by the mainstream. By 2026, it had entered the allocation discussions of every serious institutional investor.",
    "Wide shot of a formal investment conference with hundreds of suits seated in rows facing a stage, a speaker at a podium, the visual of institutional consensus forming around an idea, serious faces absorbing an analysis that was once considered fringe")
act5_clips.append(c)

c = clip(193,
    "This matters because of what it implies for the remaining runway in the gold bull market. Contrarian trades end when they become consensus. But gold was still being dramatically under-allocated by most institutional investors globally. The average pension fund held less than two percent in gold. The recommended allocation, by many macro analysts, was ten to fifteen percent. The gap was enormous.",
    "Wide shot of a financial planning office, charts on the wall, an advisor and client seated across from each other, the conversation of portfolio allocation visible in posture and gesture, the institutional recommendation process in progress")
act5_clips.append(c)

c = clip(257,
    "Mark Thornton of the Mises Institute, speaking to ITM Trading's Daniela Cambone in a conversation that had attracted tens of thousands of views by its second day online, made the Austrian economic argument with characteristic directness: gold is not rising. The dollar is falling. The two statements describe the same phenomenon from different frames of reference. But the second frame is the truthful one.",
    "Slow motion extreme close-up of gold being poured in molten form from a crucible into a mold, the glowing liquid metal flowing in a thin stream, the heat visible in the shimmer around it, copper-gold sparks floating briefly upward against the dark background")
act5_clips.append(c)

c = clip(193,
    "Thornton's thesis: gold and silver are not assets that generate income. They do not pay dividends. They cannot be created by a government or a central bank. They are, in the deepest sense, money — the form in which human civilizations chose to store value for five thousand years before the twentieth century invented alternatives.",
    "Slow dolly shot through a museum display of ancient coins from different civilizations — Roman, Persian, Byzantine, Islamic — each civilization's monetary history visible in metal, dramatic museum lighting on the individual coins, the cross-cultural continuity of precious metal money")
act5_clips.append(c)

c = clip(257,
    "When fiat currencies debase — as every single fiat currency in recorded history eventually has — gold does not rise. It is revealed. The price in dollars increases not because gold has changed, but because the dollars have changed. You are not gaining purchasing power in gold. You are retaining it while the denominator falls beneath you.",
    "Cinematic time-lapse aesthetic of a candle burning down, the flame steady and consistent, the wax diminishing frame by frame, the light constant but the material being consumed, the metaphor of fiat erosion rendered in fire and wax")
act5_clips.append(c)

c = clip(193,
    "The commenter on the Stoic Finance channel who wrote 'Get it in your hands and put it in a safe place' — describing his father's conversion of four hundred thousand dollars of bank deposits into silver, which he reported had tripled in value — was expressing a sentiment that was reaching escape velocity from the fringe into the mainstream.",
    "Close-up of weathered hands carefully placing gold coins into a small safe box, the deliberate ritual of physical storage, a window in the background showing a suburban street, the domestic scale of financial preservation")
act5_clips.append(c)

c = clip(257,
    "Consider silver. At ninety dollars per ounce as of March thirteenth, silver had accomplished what silver bulls had predicted for two full decades. But the CEO of First Majestic Silver, speaking directly to Soar Financially's interviewer about their business plans at the current price level, offered a forecast range of one hundred and fifty to one hundred and seventy-five dollars as the eventual destination.",
    "Aerial drone shot over a silver mine in mountainous terrain, terraced excavation visible against grey rock, late afternoon light catching the exposed mineral earth, a processing facility at the base of the hill gleaming in the slant light")
act5_clips.append(c)

c = clip(193,
    "The case for silver outperforming gold over the next decade is structural and powerful. Silver is simultaneously a monetary metal and an industrial metal. The electrification of the global economy — electric vehicles, solar panels, high-efficiency electronics, advanced weaponry — requires silver in quantities that the existing mine supply pipeline cannot provide.",
    "Close-up of the internal circuitry of a solar panel in the manufacturing process, thin silver conductor lines visible across the silicon surface, a technician's gloved hands adjusting the alignment under clinical white light, the industrial demand embodied in the detail")
act5_clips.append(c)

c = clip(257,
    "The gold-to-silver ratio — how many ounces of silver it takes to buy one ounce of gold — had been running at over eighty to one for years, far above its historical average of fifty to one. If the ratio merely reverts to its long-run average, with gold at five thousand dollars, the implied silver price is one hundred dollars per ounce. If it reaches its historical extremes, the numbers become dramatic.",
    "Slow tracking shot moving along a museum display case holding both gold and silver artifacts side by side, the visual contrast of the two metals under glass, spotlights creating warm highlights on the gold and cooler brilliance on the silver")
act5_clips.append(c)

c = clip(193,
    "The short-term volatility in silver was genuine. 'PM's are falling off a cliff again at noon EST,' one ITM Trading commenter wrote with evident frustration. 'Silver heading below eighty and Gold below five thousand. Nothing is making sense.' This was the experience of every retail precious metals investor: the long-term thesis is clear, the short-term path is brutal.",
    "Cinematic close-up of a rollercoaster track from the perspective of the rider, the rails curving dramatically downward against a bright sky, the visual of vertiginous movement, the blur of the descent, the experience of short-term volatility")
act5_clips.append(c)

c = clip(257,
    "Another ITM Trading commenter explained the manipulation mechanism that created the frustrating short-term price behavior: 'Paper Market needs to burn.' On the COMEX futures exchange, it is legally possible to sell claims to gold and silver that do not physically exist. When large institutional players with short positions need to defend them, they sell paper, driving prices down regardless of physical demand.",
    "Overhead shot of a commodity exchange trading floor, pits of traders gesturing to each other, the chaos of open-outcry trading, the mechanism of price formation in a market where paper and physical commodities coexist and diverge")
act5_clips.append(c)

c = clip(193,
    "The sophisticated response to short-term manipulation was articulated with clarity in the comments: 'Don't worry about short-term fluctuations and manipulations. Fiat will go to zero. They always do. Precious metals will prevail.' This may be the clearest expression of the long-term precious metals thesis that a comment thread has ever produced.",
    "Wide aerial shot of a ghost town in the American Southwest at dusk, abandoned stone and adobe buildings, dust streets, skeletal remains of a once-thriving settlement, the long arc of economic history written in desertion and preservation simultaneously")
act5_clips.append(c)

c = clip(257,
    "David Lin's Gold About to Double Again interview framed the financial crisis not as a possibility but as an inevitability. Guest Rob Bruggeman's argument: there will be some kind of financial crisis that forces countries to reign in their spending. The only question is the form it takes. Inflation — the quiet default — is the historically preferred form. Gold is the historically preferred hedge.",
    "Slow zoom into the face of a grandfather clock on a wall, the hands moving in their measured arc, the pendulum swinging steadily, the weight of time and inevitability, the mechanism of the clock as metaphor for compounding obligation")
act5_clips.append(c)

c = clip(193,
    "David Lin's YouTube comment section contained one observation that distilled the geopolitical economy of the crisis into a single sentence: 'Bessent has been printing his ass off. Powell having to buy the debt because no one wants the Treasuries. So... more money printing. Trump has the market hanging on his every word.' The macro thesis of 2026 in five lines.",
    "Wide cinematic shot of a central bank printing facility through a window, machines in operation, the endless production of currency at industrial scale, the worker visible only as a silhouette against the industrial light, the political economy of money creation")
act5_clips.append(c)

c = clip(257,
    "Now Bitcoin. Trading at approximately seventy thousand dollars as of mid-March 2026, Bitcoin had retreated significantly from its all-time high of one hundred and twenty-six thousand dollars — a peak reached in late 2025. The decline of forty-four percent from peak had emboldened the bears and tested the patience of long-term holders.",
    "Dramatic aerial shot of Las Vegas at night from altitude, the strip a river of light through the dark desert, the scale of human speculative energy visible from above, the city built on the human impulse to bet on uncertain futures")
act5_clips.append(c)

c = clip(193,
    "Luke Gromen — macro analyst, founder of Forest for the Trees newsletter, and the analyst most consistently cited across the financial channel ecosystem for his macro framework — had a different interpretation of Bitcoin's position in the crisis. He described it as the last functioning smoke alarm for the global financial system.",
    "Extreme close-up of a smoke detector mounted on a white ceiling, a thin trail of smoke drifting toward it, the detector's LED indicator glowing steady red, the quiet vigilance of the warning system waiting to sound")
act5_clips.append(c)

c = clip(257,
    "The smoke alarm metaphor carries precise meaning. A smoke alarm does not cause the fire. It does not create the smoke. It detects what is already present. When Bitcoin prices move in unusual ways, Gromen argued, they are signaling something about the health of the global financial system — specifically, the degree to which institutional participants believe the dollar system is under stress.",
    "Wide shot of a residential street at night, a house fire visible far down the block, fire engines arriving, neighbors watching from their porches, the emergency lights casting red pulses across the facades of houses, the alarm having already sounded")
act5_clips.append(c)

c = clip(193,
    "Benjamin Cowen, whose on-chain Bitcoin analysis had built a substantial institutional following, presented detailed evidence that the four-year cycle — the fundamental rhythm driven by the Bitcoin halving events — remained intact despite the powerful counter-narrative of a structural market change. The data, he argued, did not support the view that the cycle had broken.",
    "Cinematic aerial shot over ocean waves in a long cyclical pattern, the regular swells approaching a rocky coastline in rhythmic succession, the predictable cycle of wave energy, each wave following the last in the deep structural rhythm of the ocean system")
act5_clips.append(c)

c = clip(257,
    "The Bankless analysis was particularly sharp on the relationship between Bitcoin and the macro environment. The three crisis variables — oil, jobs, and private credit — affected Bitcoin not through their own logic, but through their effect on global liquidity. When liquidity contracts, speculative assets fall first and hardest. When liquidity expands, they recover first.",
    "Wide aerial drone shot over a city financial district at golden hour, the towers of banking and finance casting long shadows, the light itself seeming to flow between buildings like a liquid responding to the gravitational field of capital")
act5_clips.append(c)

c = clip(193,
    "Michael Howell of CrossBorder Capital had identified global liquidity as having peaked in fall 2025 and now turning downward. Howell's liquidity framework — which tracks not just money supply but the full range of collateral and credit available in the global financial system — was one of the most watched indicators among sophisticated investors.",
    "Slow motion shot of a tide pulling back from a rocky beach, exposing the rocks and pools beneath, the water receding with each wave, the familiar and cyclical nature of the movement, the implied return in every withdrawal")
act5_clips.append(c)

c = clip(257,
    "Liquidity cycle downturns are historically the biggest risk factor for Bitcoin and all speculative assets. But Howell also noted what every previous liquidity cycle downturn confirmed: they are never permanent. They turn. And the assets that have suffered most in the downturn typically outperform most dramatically in the subsequent upturn.",
    "Aerial time-lapse aesthetic of a flower opening from bud to bloom, the movement suggesting the cycle of compression and expansion, the inevitable return of growth after dormancy, the deep biological rhythm that financial cycles approximate")
act5_clips.append(c)

c = clip(193,
    "Arthur Hayes, in his characteristically blunt style, offered the ultimate bullish thesis for Bitcoin in his Wealthion appearance: 'The Fed will always print money.' The mechanism: a government with forty trillion dollars in debt cannot afford the interest rates required to fight inflation conventionally. Eventually, the debt forces accommodation. And accommodation means expansion of the money supply.",
    "Cinematic wide shot of a river flowing endlessly through a landscape, the water constant and moving, a bridge spanning it, the river indifferent to any single moment in its continuous flow, the permanence of the current")
act5_clips.append(c)

c = clip(257,
    "EllioTrades analyzed the Iran Oil Crisis impact on Bitcoin for his audience of crypto-native investors: the volatility itself was the signal. When geopolitical shocks of this magnitude hit, they reveal which assets are correlated to risk sentiment and which are uncorrelated. Bitcoin's behavior in the first two weeks of the war was generating data that would inform allocation decisions for years.",
    "Extreme close-up of a barometer instrument, the needle oscillating slightly around a reading, the precision of the measurement, old polished brass and glass on the instrument face, the detective work of reading invisible atmospheric forces")
act5_clips.append(c)

c = clip(193,
    "The ITM Trading community — long-established in the precious metals world, deeply skeptical of crypto — watched the precious metals with growing frustration at short-term price suppression. One viewer's comment captured the generational frustration: 'And yet, silver and gold have stayed flat all week. I swear, if values don't move tomorrow, there's no truth left anywhere in economic markets.'",
    "Close-up of a precision watch face, the second hand moving in its measured arc, the dial markers precisely spaced, the instrument of time as the instrument of patience, the long view required of anyone holding against the short-term noise")
act5_clips.append(c)

c = clip(257,
    "Jeremy Schwartz of Wisdom Tree, speaking to Wealthion, identified the 2026 crisis as a stress test for traditional portfolio construction: 'Traditional risk models are failing investors. We're in war and precious metals are underperforming. The dollar was supposed to debase, but yet it's rallying.' The paradoxes required a more sophisticated framework.",
    "Wide shot of a radar screen in a control room, the sweep arm rotating, blips appearing at different distances from center, an operator studying the screen intently, the task of making sense of competing signals in a complex environment")
act5_clips.append(c)

c = clip(193,
    "Schwartz's observation about the dollar rally was itself a signal. In acute crisis moments, the dollar typically strengthens as global investors flee to the reserve currency — even if the long-term trajectory of the dollar is downward. This is the dollar smile: the currency that benefits from both US economic outperformance and from global risk-off flight to safety.",
    "Aerial drone shot over the New York Federal Reserve building, the stolid limestone building in lower Manhattan, the surrounding streets busy, the gold bars seventeen stories below the street in the most famous vault in the world")
act5_clips.append(c)

c = clip(257,
    "The interaction between gold and the dollar in times of crisis is more complex than the simple inverse relationship that financial education often teaches. In the long run, gold and the dollar are inversely correlated because the dollar's value is the denominator of gold's price. In the short run, both can rise together when global risk aversion is the dominant force.",
    "Wide shot of a currency exchange bureau with multiple currency displays and rates visible, the global market of currency relationships made visible in the comparing numbers, a queue of travelers waiting to exchange")
act5_clips.append(c)

c = clip(193,
    "The Wealthion conversation with Brett Rentmeester was titled 'Hard Assets Matter When Geopolitics and Markets Turn Chaotic.' Rentmeester's thesis was not that hard assets always outperform — they do not. It was that hard assets provide the portfolio with outcomes that soft assets cannot. When paper systems fail, real things hold value. The insurance premium is worth paying.",
    "Wide aerial drone shot over farmland at harvest time, combines working the fields in systematic rows, dust rising behind them, the real world production of real world value, the foundational economy beneath the financial superstructure")
act5_clips.append(c)

# ============================================================
# ACT VI: GEOPOLITICAL CHESS
# Target: ~55 clips, ~2,200 words
# ============================================================
act6_clips = []

c = clip(257,
    "Act Six. The Geopolitical Chess. Who wins. Who loses. And what happens to the map of global energy, trade, and financial power in a world reorganizing around new fault lines.",
    "Sweeping aerial drone shot at dusk over a geopolitical landscape, mountains and plains and waterways visible, the natural geography that underlies all political boundaries, the world as physical fact indifferent to human conflict")
act6_clips.append(c)

c = clip(257,
    "Charles Gave of Gavekal Research, speaking to Soar Financially, offered the most incisive geopolitical analysis of the oil shock available on financial media in March 2026. Gave's central thesis: the Iran war was reshaping the US-China relationship in ways that could prove as consequential for the twenty-first century as the war itself.",
    "Wide shot of a large conference room with an empty oval table, flags of major nations behind the chairs, the visual of high-stakes diplomacy in the absence of the diplomats, the empty seats suggesting the meetings that happen elsewhere")
act6_clips.append(c)

c = clip(193,
    "Gave's crucial structural insight: while China receives a significant portion of its oil from the Gulf, it is less vulnerable than it appears. Pipeline access from Russia and Central Asia — the Power of Siberia pipeline, the Central Asia-China pipeline system — provides an alternative supply route that bypasses the Strait of Hormuz entirely.",
    "Aerial drone shot over a massive oil pipeline corridor crossing a Central Asian steppe, the silver pipeline a straight line to the horizon in both directions, the immense infrastructure of energy sovereignty")
act6_clips.append(c)

c = clip(257,
    "This asymmetry has profound strategic implications. If the United States has effectively closed the Hormuz through the Iran war, and in doing so has disrupted the energy supply to Europe, Japan, South Korea, and Southeast Asia — but less so to China — then the unintended consequence of the war is a relative strategic advantage for Beijing.",
    "Cinematic wide shot of a massive container port at dusk, container cranes lit up in rows, ships at berth, the physical infrastructure of global trade, the port as the material expression of geopolitical relationships")
act6_clips.append(c)

c = clip(193,
    "Gave described this as oil reshaping the US-China relationship. China, as a net oil importer that is partially insulated from Hormuz disruption via pipeline alternatives, watches the war with a different calculus than Europe, which has no such alternatives and is heavily exposed to LNG supply disruption.",
    "Wide aerial shot of a sprawling industrial city in China at dusk, factory chimneys and power transmission lines visible, the urban-industrial scale of Chinese manufacturing, yellow haze catching the last amber light of the day")
act6_clips.append(c)

c = clip(257,
    "Europe's exposure was the critical factor in the geopolitical chess game. Since the Ukraine war began in 2022, European governments had spent enormous political capital and economic resources diversifying away from Russian energy. American LNG had been the centerpiece of that strategy. Now, with QatarEnergy declaring force majeure and American LNG production at capacity, that strategy had hit a wall.",
    "Dramatic aerial shot of a European port LNG terminal in winter, the regasification facility lit against grey sky, a tanker docked and transferring cargo, the critical infrastructure of European energy import dependency visible in steel and concrete")
act6_clips.append(c)

c = clip(193,
    "Germany, in particular, faced an existential economic challenge. The country's industrial model — Mittelstand manufacturers, the chemical industry, the automotive sector — is built on energy-intensive processes that require competitively priced energy. High energy costs had already pushed German industry into contraction before the war. The oil shock threatened to accelerate that contraction into structural deindustrialization.",
    "Cinematic wide shot of a German industrial facility at night, blast furnaces glowing orange, the scale of heavy industry, workers visible in protective gear, the industrial civilization that Europe built and may be losing")
act6_clips.append(c)

c = clip(257,
    "And then there was Russia. The Coin Bureau was direct in its headline: 'The Only Winner in the Iran War is Unexpectedly Russia.' At one hundred and fifteen dollar oil, Russia's war-era budget math transformed completely. Every barrel sold above eighty dollars per barrel — Russia's fiscal breakeven point — was a budget surplus, not a deficit. The oil price windfall was repairing three years of sanctions damage.",
    "Cinematic aerial drone shot slowly moving over a Siberian oil field in winter, machinery and wells visible in a snow-covered landscape, pipes and industrial infrastructure stark against the white, a grey overcast sky above the production zone")
act6_clips.append(c)

c = clip(193,
    "Joe Blogs documented the extraordinary irony with characteristic British directness: 'The United States has now taken the extraordinary step of allowing additional Russian oil to be sold in an attempt to stabilize global energy markets. And that tells you something very important about how serious the situation has become.' America had enriched Russia while fighting Iran.",
    "Wide shot of a pipeline terminal where multiple lines converge into a manifold, valves and meters and safety equipment, steam in cold air, the complex interdependency of global energy infrastructure made visible in metal and engineering")
act6_clips.append(c)

c = clip(257,
    "The Russia dynamic created a further geopolitical paradox. By generating the oil price windfall for Moscow, the Iran war was providing Russia with the fiscal resources to sustain its military operations in Ukraine. The administration in Washington had inadvertently strengthened the economy of the country it had spent three years trying to weaken.",
    "Aerial drone shot over Eastern European plains in winter, a long straight road cutting through flat agricultural land, the scale of the landscape connecting the different theaters of geopolitical conflict")
act6_clips.append(c)

c = clip(193,
    "The food security dimension of the Hormuz crisis was receiving inadequate attention given its potential scale of impact. The markets most vulnerable to the fertilizer supply disruption were not in North America or Europe, which had domestic production capacity. They were in South Asia, the Middle East, North Africa, and Sub-Saharan Africa — regions that import both energy and fertilizer.",
    "Wide aerial drone shot over agricultural fields in a developing nation, small-scale farming visible, irrigation channels, the essential vulnerability of food production systems built on global supply chain assumptions")
act6_clips.append(c)

c = clip(257,
    "Pakistan, with a population of two hundred and forty million people and a chronic dependency on imported energy, was among the most exposed. Egypt, whose population of one hundred million was already stretched by food subsidies consuming a significant fraction of the government budget, faced the prospect of those subsidies becoming unaffordable. These were not market problems. They were potential political destabilization events.",
    "Wide aerial shot of a densely populated urban neighborhood in a developing megacity, millions of people visible in the urban density, the human scale of food security as a civilizational question")
act6_clips.append(c)

c = clip(193,
    "Copper. In the midst of all these crises, Jesse Day of Commodity Culture reported on March eleventh that copper was 'next up to shock the market.' The copper deficit, he said, was serious — not caused by the war, but by a structural demand story that no war and no central bank decision could quickly reverse.",
    "Cinematic aerial shot over a massive open-pit copper mine, the terraced red-brown walls spiraling down hundreds of meters, heavy mining equipment visible on each bench, the scale of extraction that the copper civilization requires")
act6_clips.append(c)

c = clip(257,
    "The copper demand supercycle was being driven by three simultaneous forces, each of them individually enormous. The first: AI infrastructure. The build-out of artificial intelligence data centers — which require extraordinary quantities of copper for power distribution, cooling systems, and interconnect — was accelerating rather than slowing, despite the energy cost pressures.",
    "Wide shot of a massive data center interior under construction, rows of server rack frames being installed, extensive copper wiring being laid in organized bundles, the physical infrastructure of digital intelligence")
act6_clips.append(c)

c = clip(193,
    "Jeremy Schwartz of Wisdom Tree connected the copper thesis directly to the AI energy supercycle: data center electricity demand was on track to consume a significant fraction of the entire US power grid within a decade. Every megawatt of that power demand required copper in the transmission, distribution, and transformation equipment. The numbers were staggering.",
    "Aerial drone shot over a high-voltage power corridor, transmission towers stretching to the horizon, the lines converging at a substation, the aging grid infrastructure carrying the load of a demand profile it was never designed to serve")
act6_clips.append(c)

c = clip(257,
    "The second force: electrification of transportation. A conventional internal combustion vehicle contains roughly twenty kilograms of copper. A comparable electric vehicle contains eighty to one hundred kilograms — four to five times as much. Global EV production was on track to exceed thirty million vehicles in 2026. The copper demand from that transition alone was creating a structural deficit in the mining pipeline.",
    "Close-up of a copper wire harness in an electric vehicle during assembly, the bundled cables being carefully routed through the vehicle frame, the visual density of copper in the electrified economy")
act6_clips.append(c)

c = clip(193,
    "The third force: grid modernization. Every grid in every developed nation was built in the mid-twentieth century. Its physical infrastructure — the wires, the transformers, the substations, the distribution systems — was approaching end of life simultaneously with the requirement to carry dramatically more electricity. Grid investment requirements measured in the trillions of dollars lay ahead, and copper was in every meter of it.",
    "Aerial drone shot over a substation modernization project, construction equipment visible, new transformer equipment being installed beside old infrastructure, the scale of the transition from the industrial-era grid to the digital-era grid")
act6_clips.append(c)

c = clip(257,
    "The geopolitical complexity of the copper story added another dimension of risk. The largest copper reserves in the world are concentrated in three politically complex areas: the Andes of South America — primarily Chile and Peru — the Democratic Republic of Congo, and Zambia. Each of these regions had experienced political disruption in recent years. The supply chains for the twenty-first century's essential metal were not secure.",
    "Cinematic aerial shot of the Andes mountains, snow-capped peaks above cloud level, the landscape vast and ancient, the geology of copper deposits underlying the peaks that now defined the geopolitics of the energy transition")
act6_clips.append(c)

c = clip(193,
    "China had been systematically addressing its copper supply security for two decades. Through its Belt and Road Initiative and through direct commercial acquisitions, China had built significant ownership stakes and long-term supply agreements in copper mines across Africa and South America. The strategic competition for copper was already deeply underway before March 2026.",
    "Cinematic slow drone shot over a major port, mountains of copper cathodes stacked in the open air, the distinctive red-orange metal visible from altitude, cranes loading ships, the organized flow of copper from mine to processing plant to manufacturing")
act6_clips.append(c)

c = clip(257,
    "The broader commodity supercycle thesis — that we were entering a decade or more of elevated commodity prices driven by years of underinvestment in supply and by surging structural demand — was being vindicated in real time. Gold, silver, copper, uranium, the rare earths. Each had its specific demand story. All shared a common supply story: years of capital starvation had created deficits.",
    "Aerial drone shot over a mining operation in a remote landscape, the scale of earth movement visible, trucks as small as ants on the roads between excavation levels, the industrial muscle applied to the resource extraction that civilization requires")
act6_clips.append(c)

c = clip(193,
    "The global liquidity cycle, as Michael Howell had noted, had peaked in fall 2025. But previous liquidity cycles offered a template: when the next expansion cycle begins — when central banks ultimately respond to the economic slowdown with accommodation — the commodities and hard assets that had been building structural deficits would benefit disproportionately.",
    "Slow motion shot of a wave cresting at the shoreline, the peak perfectly captured at maximum height before it begins to fall forward, the moment of pause at the apex of the cycle, the implied return encoded in the physics of the wave")
act6_clips.append(c)

c = clip(257,
    "The sophisticated investor framework for March 2026 was not that the world was ending. It was that the rules of the old world had ended. The old rules: predictable supply chains, stable energy prices, freely functioning credit markets, a Federal Reserve with room to maneuver. The new rules had not yet been written. But they required a different portfolio construction.",
    "Wide shot of a crossroads in a remote landscape, two roads stretching in different directions to different horizons, late afternoon light throwing long shadows from the road markers, the geography of choice")
act6_clips.append(c)

c = clip(193,
    "The Wealthion interview with Brett Rentmeester captured the emerging consensus with precision: hard assets matter when geopolitics and markets turn chaotic. Not as a bet on disaster, but as a recognition that the range of bad outcomes had permanently and irreversibly widened. Insurance is not purchased because you expect the house to burn. It is purchased because you acknowledge it might.",
    "Wide aerial drone shot over a landscape showing both cultivated farmland and wild terrain in the same frame, the boundary between order and chaos the relevant question, the two states coexisting in visible proximity")
act6_clips.append(c)

c = clip(257,
    "The US-China dynamic on energy extended beyond oil and copper. The Rare Earth dimension of the conflict had received insufficient attention. China controls approximately eighty percent of global rare earth processing capacity — the refining of the minerals used in EV motors, wind turbines, defense electronics, and precision weapons. The Iran war had accelerated discussion of supply chain vulnerabilities that had been noted for years but not addressed.",
    "Aerial drone shot over a rare earth processing facility, the distinctive red-orange tailings ponds visible from altitude, the chemical infrastructure of mineral separation, the strategic material at the center of the technological competition")
act6_clips.append(c)

# ============================================================
# ACT VII: THE RECKONING
# Target: ~60 clips, ~2,500 words
# ============================================================
act7_clips = []

c = clip(257,
    "Act Seven. The Reckoning. What we have learned. What it means. And what comes next in a world that has permanently changed.",
    "Extreme wide aerial drone shot at dusk over a great city, the lights beginning to come on as the sun descends on the horizon, the city stretching to every edge of the frame, the scale of human civilization visible from above")
act7_clips.append(c)

c = clip(257,
    "Let us return to where we began. March first, 2026. The dawn strikes on Tehran. In the days that followed, four compound crises became visible simultaneously — crises that had been developing for years but were crystallized by the war into a single, coherent, and terrifying economic picture that financial markets were only beginning to price.",
    "Slow cinematic push into a globe on a library desk, the lamp light illuminating the Middle East region, the camera moving slowly toward the Persian Gulf, the geography of the crisis at the center of the frame")
act7_clips.append(c)

c = clip(193,
    "Crisis one: the energy shock. The Strait of Hormuz had gone from one hundred and thirty-eight ships per day to four. Brent crude had hit one hundred and fifteen dollars, up thirty-five percent in under a week. LNG prices had surged one hundred and thirty-seven percent in five days. QatarEnergy had declared force majeure on long-term contracts worth billions.",
    "Cinematic overhead drone shot slowly moving over the Strait of Hormuz, the narrow passage of water between two landmasses, the extraordinary geopolitical weight of twenty-one miles of ordinary ocean")
act7_clips.append(c)

c = clip(257,
    "Crisis two: the private credit implosion. Three hundred billion dollars in assets frozen or restricted across BlackRock, Blackstone, Morgan Stanley's North Haven, Cliffwater, and Blue Owl. British firms collapsing. The shadow banking system — built in the decade of zero interest rates, unregulated and opaque — discovering that it had no mechanism for large-scale simultaneous redemptions.",
    "Slow dolly shot down a long hallway of a financial institution after hours, offices dark on both sides, a cleaning crew visible at the far end, the institutional machinery idle for the night, the scale of the system dwarfing the people who serve it")
act7_clips.append(c)

c = clip(193,
    "Crisis three: the Federal Reserve's bind. Stagflation — the simultaneous presence of rising inflation and economic contraction — presented the central bank with a choice between bad options. Cut rates and fuel the inflation that was already threatening to become entrenched. Raise rates and deepen a recession that was already underway for the bottom ninety percent. Hold steady and allow both problems to compound.",
    "Close-up of a doctor's hands holding a stethoscope to a patient's chest, the listening posture of diagnosis, the weight of a difficult assessment, the professional responsible for finding a path where there may not be one")
act7_clips.append(c)

c = clip(257,
    "Crisis four: the K-shaped fracture. The bottom ninety percent of the American economy — already experiencing the lived reality of recession through credit card delinquencies, auto loan defaults, food bank usage, and wage stagnation — were about to face higher gasoline prices, higher heating costs, higher food prices, and tighter credit conditions simultaneously. This was not a theoretical risk. It was already happening.",
    "Aerial drone shot over an American highway at rush hour, four lanes of traffic moving slowly in both directions, the everyday reality of commuting, the fuel cost of the daily grind, the working economy in motion and increasingly under pressure")
act7_clips.append(c)

c = clip(193,
    "Connecting these four crises was a single common thread: the debt. The United States federal government owed forty trillion dollars. American households owed a combined twenty-six trillion. American corporations owed another fifteen trillion. Every dollar of that debt had been issued under assumptions of growth, stability, and affordable energy that no longer held.",
    "Aerial drone shot slowly pulling back from a single residential house to reveal an entire neighborhood, then a city, then a metropolitan area, the debt of each household aggregating to the unimaginable total that defines the American economic condition")
act7_clips.append(c)

c = clip(257,
    "When assumptions fail at this scale, the reckoning does not arrive all at once. It arrives in stages — each crisis revealing the next vulnerability, each patch revealing the next leak. The financial system is not a machine that fails completely. It is a network that degrades, that finds partial solutions, that creates new fragilities while resolving old ones.",
    "Cinematic wide shot of an old stone wall being carefully inspected by a mason, hands probing the mortar between stones, finding a crack, pressing a finger in, discovering the depth of deterioration, the expert diagnosis of structural weakness")
act7_clips.append(c)

c = clip(193,
    "For investors — the audience that financial channels serve — the practical question was not academic. What do you do? Every analyst we have featured in this documentary, from Martin Wolf at the Financial Times to Luke Gromen to Arthur Hayes, converges on a single broad principle: diversification away from paper assets and toward real assets is no longer a fringe view.",
    "Wide shot of a museum vault or storage facility where different types of physical assets are stored, the visual of real value in physical form, paintings, sculptures, metals, the things that human civilization has always valued in extremis")
act7_clips.append(c)

c = clip(257,
    "Gold has a five-thousand-year track record as a store of value. In every monetary crisis in history — every debasement, every hyperinflation, every default, every empire's end — gold has preserved purchasing power over long time horizons. The Incrementum forecast of five thousand two hundred dollars per ounce is not wild speculation. It is a historically grounded estimate of where gold goes when fiat credibility erodes.",
    "Cinematic extreme close-up of ancient gold artifacts in a museum display, coins and jewelry from ancient civilizations laid in careful rows, the metal unchanged after millennia, the same material that ancient merchants, kings, and emperors trusted")
act7_clips.append(c)

c = clip(193,
    "Silver, at ninety dollars with both a monetary and industrial demand story that the energy transition only strengthens, had a multi-decade runway that gold's purely monetary story did not. First Majestic Silver's CEO speaking of one hundred and fifty to one hundred and seventy-five dollar silver was not a fantasy. It was an extrapolation from structural deficits already visible in the supply data.",
    "Aerial drone shot over a solar farm in operation at sunset, panels angled toward the diminishing light, the silver conductors in every panel invisible but essential, the industrial demand for the metal embodied in landscape-scale installations")
act7_clips.append(c)

c = clip(257,
    "Bitcoin remained the most contested of the safe havens. With fixed supply — twenty-one million coins, of which approximately nineteen point eight million had already been mined — and growing institutional adoption evidenced by the spot ETF flows, Bitcoin's long-term supply-demand equation was compelling. The short-term path through the current liquidity cycle downturn was uncertain.",
    "Dramatic aerial shot over a fork in a river, the water splitting around a large island, the two channels moving in parallel before one curves away, the visual of divergent paths from a single origin, both channels flowing toward the same eventual sea")
act7_clips.append(c)

c = clip(193,
    "Luke Gromen's smoke alarm metaphor for Bitcoin was not endorsement of any particular price target. It was a framework for understanding what Bitcoin's price movements reveal about the underlying health of the global financial system. When the alarm sounds — when Bitcoin moves dramatically — it is worth paying attention to what it is detecting.",
    "Close-up of a smoke detector on a ceiling, the device small and unassuming, a single indicator light blinking in its slow rhythm, the quiet vigilance of a warning system that most people ignore until the moment they need it")
act7_clips.append(c)

c = clip(257,
    "Copper, as we have documented, was the commodity story of the decade that the current crises were obscuring rather than reversing. The AI energy demand, the electrification of transportation, the grid modernization — none of these structural demand drivers were reversed by the Iran war. If anything, the war's demonstration of energy supply vulnerability accelerated the political will to invest in domestic energy infrastructure.",
    "Wide aerial shot of a major infrastructure construction project, cranes and concrete forms visible, workers in hard hats at multiple levels, the physical scale of the investment in energy and transportation infrastructure, the copper in every wall")
act7_clips.append(c)

c = clip(193,
    "The Ben Rhodes phrase — the Great Lie of War — was not about any single administration's decision. It was about the institutional tendency to oversell certainty when presenting military options and to undersell the economic consequences. Wars are easy to start. They are nearly impossible to stop on the schedule that those who start them promise.",
    "Wide shot of an empty war memorial at dusk, stone walls bearing names, the late light catching the carved letters, a single flower placed at the base of the wall, the human cost made intimate and individual against the institutional scale of the decision that created it")
act7_clips.append(c)

c = clip(257,
    "The Iran war, in its second week as of March thirteenth, had already produced economic consequences that would take years to fully absorb. The Hormuz closure, even if lifted tomorrow, had permanently demonstrated the vulnerability. That vulnerability would now be repriced into insurance rates, investment in alternative routing infrastructure, and the energy security budgets of every affected nation.",
    "Aerial drone shot over a reconstruction site where infrastructure is being rebuilt after disruption, construction equipment active, new structures rising from cleared ground, the process of adaptation underway, the future being built on the ruins of assumptions")
act7_clips.append(c)

c = clip(193,
    "Azul, the financial advisor with over twenty years of experience, had warned his audience about the classic warning signs of market downturns: 'What always happens before a market crash' is not a single alarming event. It is the accumulation of small risks — individually explained away, collectively catastrophic. By March 2026, those small risks had ceased to be small.",
    "Cinematic wide shot of stormclouds building over a wide landscape, individual clouds merging into a larger formation, the sky darkening incrementally, each new cloud adding to the mass, the threshold between weather and storm approaching")
act7_clips.append(c)

c = clip(257,
    "The oil shock was real. The private credit freeze was real. The Fed's bind was real. The K-shaped fracture in the economy was real. The question was no longer whether these things were happening. The question was how they would interact — how they would amplify each other, dampen each other, or resolve into some new equilibrium that none of the models had yet described.",
    "Slow wide aerial drone shot over an ocean surface at sunset, the water moving in deep swells, the wind visible in the wave patterns, the vast system of forces operating beneath and above the surface, the depth and power of the dynamic equilibrium")
act7_clips.append(c)

c = clip(193,
    "Every economist cited in this documentary, from Martin Wolf to Joseph Stiglitz to Charles Gave, agreed on one principle: the uncertainty range had permanently widened. The probability-weighted outcomes of the global economy in 2026 and 2027 were now far more dispersed than they were in February. The tails were fat, and both of them were threatening.",
    "Wide shot of a weather forecasting center, meteorologists studying multiple screens showing different model outputs, the visual of expert uncertainty — not ignorance, but the honest acknowledgment of a wide probability range, the integrity of knowing what you don't know")
act7_clips.append(c)

c = clip(257,
    "For the retail investors — the millions of subscribers to Thoughtful Money, ITM Trading, David Lin, Soar Financially, Bankless, and the dozen other channels whose collective audience measured the scope of financial anxiety in March 2026 — the message from every credible voice was the same: this is not a moment for heroic single bets. It is a moment for diversification, humility, and preparation for outcomes that standard models do not include.",
    "Cinematic wide shot of a family around a dining room table in the evening, books and papers spread out, a conversation happening that is clearly important, the domestic scale of financial decision-making in a moment of macroeconomic crisis, warm lamp light on the family")
act7_clips.append(c)

c = clip(193,
    "The comment that stayed with us through this analysis came from the ITM Trading live stream on Day Six of the Iran war. 'When fiat goes to zero, they take you to war.' Whether precise or not, it captured a sentiment — a bone-deep mistrust of institutional finance, of political promises, of official narratives — that millions share and that has been building for decades.",
    "Close-up of hands opening a leather-bound journal to a page with handwritten notes, the personal record of someone trying to make sense of the world, the pen resting across the open pages, the individual effort to understand a system that seems designed to resist understanding")
act7_clips.append(c)

c = clip(257,
    "That sentiment had driven the precious metals community for decades. It drove the Bitcoin community from its inception. It was increasingly driving the mainstream investor who looked at forty trillion dollars in federal debt, a private credit market with no pricing transparency, a Federal Reserve between a rock and a hard place, and decided: I need to own something real.",
    "Wide aerial drone shot over a city at night transitioning to reveal farmland in the dawn light beyond its edges, the city on one side of the frame with its paper wealth, fields on the other with their real production, the two economies of physical and financial reality")
act7_clips.append(c)

c = clip(193,
    "The Eurodollar University comment thread captured the retail experience of the recession debate with devastating economy: 'Economist finally admit to a recession. That's how you know the depression started.' Another: 'We've been in recession since 2022.' A third: 'No jobs out there. 1929 moment is here.' These were not analyses. They were human experiences.",
    "Wide shot of ordinary people on a city street going about their lives — commuters, a street vendor, a construction worker, a mother with a stroller — the everyday economy captured in its human texture, the lived reality beneath the macroeconomic statistics")
act7_clips.append(c)

c = clip(257,
    "The Steve Hanke comment thread produced what may be the most precisely cynical observation of the entire crisis: 'IT WILL BE THE 70s AGAIN WITHOUT ALL THE GOOD MUSIC.' The humor was dark, the history was correct. The 1970s were economically devastating — two oil shocks, double-digit inflation, a lost decade of equity returns. The music, of course, was extraordinary.",
    "Cinematic slow-motion shot of a vintage vinyl record turning on a turntable, the needle in the groove, the warm analog sound implied in the physical contact of stylus and disc, the 1970s made material and present")
act7_clips.append(c)

c = clip(193,
    "What the 1970s analogy ultimately tells us is this: the crisis will resolve. Not quickly, not painlessly, not without creating permanent structural changes in the economy and the political landscape. But it will resolve. The world continued after 1973. It continued after 1979. It continued after 2008. It will continue after 2026.",
    "Aerial drone shot over a forest recovering from a wildfire, new green growth visible pushing through the ash-grey landscape, the resilience of biological regeneration, the evidence that destruction and renewal are always the same process")
act7_clips.append(c)

c = clip(257,
    "Martin Wolf's counsel, drawn from a lifetime of studying economic crises, was ultimately a counsel of informed hope: the world economy has absorbed massive shocks before. The 1970s oil crises. 2008. COVID. Each time, the recovery took longer and cost more than the initial optimists predicted. Each time, the world emerged changed but continuing. That pattern is the most powerful argument against despair.",
    "Wide aerial drone shot over a harbor as the sun rises, fishing boats heading out on the morning tide, the ordinary heroism of people who continue working whatever the macroeconomic environment, the morning economy going about its essential business")
act7_clips.append(c)

c = clip(193,
    "The investors who will navigate this period best are not those who predicted it precisely. They are those who built portfolios resilient enough to survive a wide range of outcomes — who own real assets alongside financial ones, who are not leveraged to a single outcome, who have accepted that uncertainty is not a temporary condition but the permanent nature of the world.",
    "Slow wide aerial shot of a mountaineer on a ridge line at first light, the sun rising behind distant peaks, the climber steady and balanced on the narrow path, the vast landscape stretching in every direction below, the hard-won vantage")
act7_clips.append(c)

c = clip(257,
    "The global economic crisis of March 2026 will be studied in business schools and economic history courses for generations. Not as a case study in inevitable catastrophe — it was not inevitable, until it was. But as a case study in compound risk: in how individual vulnerabilities that seem manageable in isolation can become catastrophic when they converge simultaneously.",
    "Slow pan through a university library, tall shelves of books receding in both directions, a single student at a table studying, the institution of accumulated knowledge, the long project of understanding through the records of those who came before")
act7_clips.append(c)

c = clip(193,
    "The oil shock and the private credit crisis and the Federal Reserve's bind and the K-shaped economy are not four separate stories. They are four faces of a single underlying reality: an economic system that had been running on borrowed time, borrowed money, and borrowed assumptions about the stability of the world.",
    "Dramatic aerial wide shot of a major suspension bridge at dusk, the cables tensioned and holding, traffic crossing from both directions, the engineered elegance of a structure bearing enormous loads through distributed tension and precise balance")
act7_clips.append(c)

c = clip(257,
    "The question that every investor, every government, every central bank faces as they watch March 2026 unfold is the same question that every generation faces when the world changes faster than the models: What do you do when the map is no longer the territory? When the assumptions embedded in every price and every model are revealed to have been wrong?",
    "Close-up of an old paper map laid flat on a table, a compass rose in one corner, the lines and symbols of how someone once understood the world, the edges worn from use, the territories named in a language of certainty about a world that no longer exists")
act7_clips.append(c)

c = clip(193,
    "You relearn the territory. You acknowledge the map's failure. You venture out with greater humility and greater care, relying less on models and more on principles, less on recent history and more on long history, less on what has worked in the last decade and more on what has worked across centuries.",
    "Cinematic slow shot of a compass needle finding north in a hand, the needle oscillating before settling on its orientation, the analog technology of navigation, the basic tool of orienting yourself when the familiar landmarks have shifted")
act7_clips.append(c)

c = clip(257,
    "The principles that have worked across centuries are the ones being rediscovered in March 2026. Diversification. Hard assets. Avoidance of excess leverage. A time horizon long enough to survive the inevitable corrections. Skepticism of institutions that benefit from your confidence in them. And the knowledge that every previous generation faced its own version of the world on fire — and found its way through.",
    "Wide aerial drone shot at dawn over a coastal city, the sun rising over the ocean, the first light catching the towers of the financial district, the harbor with its boats, the airport with its early departures, the city beginning a new day")
act7_clips.append(c)

c = clip(193,
    "The war in Iran will end — as all wars end. The Strait of Hormuz will reopen — or the world will build infrastructure to route around it. The private credit crisis will resolve — painfully, with winners and losers and some degree of regulatory reckoning. The Federal Reserve will find its path — probably through the inflation that the debt load ultimately requires.",
    "Cinematic slow zoom out from a single candle flame to reveal a room full of candles, each light separate but the collective illumination transforming the space, the visual of distributed resilience, individual lights adding to a collective brightness")
act7_clips.append(c)

c = clip(257,
    "But the landscape on the other side will look different. Energy security will be a central concern of every national budget for a generation. The private credit market will operate under far more scrutiny than it has ever faced. The Federal Reserve's independence will be tested in ways that will require institutional courage. And the commodities that the world needs to build the future — copper, silver, the rare earths of the green transition — will be priced to reflect their strategic importance.",
    "Sweeping aerial drone shot at dawn over an industrial-natural landscape, a river winding through a valley, a city on one bank, farmland on the other, the sun rising and casting new light on the entire scene, the suggestion of a world reordering itself in the morning")
act7_clips.append(c)

c = clip(193,
    "We have reported on this crisis through the voices of those living through it: the analysts, the economists, the channel hosts, and the commenters who represent the voice of retail investors trying to protect their families. The data is incomplete. The outcome is uncertain. That is the honest condition of real-time financial journalism in a moment of genuine crisis.",
    "Close-up of a journalist's notepad with handwritten notes being actively written, a pen moving across the page, words forming under the pen, the recording of events in progress, the act of documentation as a form of witness")
act7_clips.append(c)

c = clip(257,
    "What is not uncertain is this: the events of March 2026 have changed the assumptions under which global finance operates. The era of cheap energy, easy credit, stable geopolitics, and a Federal Reserve with room to maneuver — that era did not end with a single dramatic moment. It ended in stages, beginning long before March 2026, completing itself in thirteen days of war, oil shock, and credit freeze.",
    "Final dramatic wide aerial shot of a burning flare stack on an oil platform at night, the flame brilliant against the dark ocean and sky, the platform small and the darkness enormous, the energy and its cost made visible in the single flame against the night")
act7_clips.append(c)

c = clip(257,
    "The world is on fire. Literally, in the Persian Gulf, where the war's second week showed no signs of ending. Figuratively, in the offices of private credit managers calculating their exposure. In the corridors of the Federal Reserve, where every option leads somewhere painful. In the kitchens of families watching their energy bills climb. And in the data centers where algorithms are pricing risks that standard models have no category for.",
    "Wide aerial drone shot over the Persian Gulf at dusk, the water turning from blue to gold to orange as the sun descends, oil platforms visible on the horizon, the geography of the crisis, the beauty and the danger in the same frame")
act7_clips.append(c)

c = clip(193,
    "What happens next depends on choices that have not yet been made. By Iran's new leadership. By the Federal Reserve. By the governments of Europe. By BlackRock and Morgan Stanley and Cliffwater. And by millions of individual investors deciding, right now, whether to trust the system that got them here, or to seek the real assets that exist outside it.",
    "Wide cinematic shot of a road stretching straight to a distant horizon under a sky of storm clouds and breaks of sunlight simultaneously, multiple possible futures present in a single frame, the road ahead real and passable but uncertain")
act7_clips.append(c)

c = clip(257,
    "We will be watching. We will continue to report what we find. Because in financial crises — as in all crises — the most dangerous thing is not the risk you can see and name and hedge against. It is the risk you have convinced yourself doesn't exist. The oil is flowing. The credit is locked. The smoke alarms are sounding. What you do next is up to you.",
    "Final wide aerial drone shot slowly pulling back from a city at night, the lights glowing in grid patterns, the human civilization lit up against the darkness, the planet in its orbit with its seven billion lives navigating the same storm, a long slow fade to black")
act7_clips.append(c)

# ============================================================
# ASSEMBLE SEGMENTS
# ============================================================
segments = [
    {
        "act": "Act I: Cold Open — The Iran War Begins",
        "theme": "iran_war",
        "clips": act1_clips
    },
    {
        "act": "Act II: Oil Shock — Hormuz Dark, Brent at $115",
        "theme": "oil_shock",
        "clips": act2_clips
    },
    {
        "act": "Act III: Private Credit Crisis — $300 Billion Contagion",
        "theme": "private_credit_crisis",
        "clips": act3_clips
    },
    {
        "act": "Act IV: The Fed's Impossible Bind — Stagflation Returns",
        "theme": "fed_stagflation_bind",
        "clips": act4_clips
    },
    {
        "act": "Act V: Safe Havens — Gold $5,200, Silver $90, Bitcoin as Smoke Alarm",
        "theme": "gold_silver_surge",
        "clips": act5_clips
    },
    {
        "act": "Act VI: Geopolitical Chess — Russia Wins, China Adapts, Europe Fractures",
        "theme": "us_china_geopolitics",
        "clips": act6_clips
    },
    {
        "act": "Act VII: The Reckoning — Synthesis and What Lies Ahead",
        "theme": "k_shaped_economy_recession",
        "clips": act7_clips
    }
]

# Build full structure
script = {
    "title": title,
    "description": description,
    "negative_prompt": negative_prompt,
    "segments": segments
}

# Write to file
with open('/home/user/workspace/v5_script.json', 'w', encoding='utf-8') as f:
    json.dump(script, f, indent=2, ensure_ascii=False)

# Statistics
total_clips = sum(len(seg['clips']) for seg in segments)
total_narration_words = sum(
    len(clip_item['narration'].split())
    for seg in segments
    for clip_item in seg['clips']
)
duration_minutes = total_narration_words / 135

print(f"\n{'='*60}")
print(f"DOCUMENTARY SCRIPT SUMMARY")
print(f"{'='*60}")
print(f"Title: {title}")
print(f"\nTotal clips: {total_clips}")
print(f"Total narration words: {total_narration_words:,}")
print(f"Estimated duration at 135 WPM: {duration_minutes:.1f} minutes ({duration_minutes*60:.0f} seconds)")
print(f"\nClips per act:")
for seg in segments:
    act_words = sum(len(c['narration'].split()) for c in seg['clips'])
    act_duration = act_words / 135
    print(f"  {seg['act']}: {len(seg['clips'])} clips | {act_words:,} words | {act_duration:.1f} min")

print(f"\nFrame distribution:")
frame_counts = {}
for seg in segments:
    for c in seg['clips']:
        f = c['frames']
        frame_counts[f] = frame_counts.get(f, 0) + 1
for frames, count in sorted(frame_counts.items()):
    duration_s = frames / 24
    print(f"  {frames} frames ({duration_s:.1f}s at 24fps): {count} clips")

print(f"\nTotal video duration from frames: {sum(c['frames'] for seg in segments for c in seg['clips'])/24/60:.1f} minutes")
print(f"\nFile saved: /home/user/workspace/v5_script.json")
print(f"File size check...")
import os
size = os.path.getsize('/home/user/workspace/v5_script.json')
print(f"File size: {size/1024:.1f} KB")
