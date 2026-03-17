#!/usr/bin/env python3
"""
Build the v5 documentary script JSON
"""
import json

title = "The World on Fire: The Global Economic Crisis of March 2026"
description = """In thirteen days, the world changed. On March 1, 2026, US and Israeli strikes on Iran ignited a chain reaction that no economic model had fully priced in. The Strait of Hormuz — through which 20% of the world's oil flows — went dark. Ship traffic collapsed 94%. Brent crude spiked to $115. LNG prices surged 137% in five days. And in the shadows of the energy shock, a $300 billion private credit crisis began to crack open.

This is the story of March 2026: how a war triggered an oil shock, how an oil shock threatened a debt crisis, how a debt crisis paralyzed the Federal Reserve, and how ordinary investors were left searching for shelter in gold, silver, and Bitcoin.

Featuring analysis from Martin Wolf (Financial Times), Joseph Stiglitz (Nobel laureate), Charles Gave (Gavekal Research), Ronald Stoeferle (Incrementum), Luke Gromen, Arthur Hayes, and leading voices from across the financial world.

A Bloomberg Originals-style documentary examining the intersecting crises reshaping the global economy in real time."""

negative_prompt = "blurry, low quality, distorted, text, watermark, logo, subtitles, words, letters, numbers, UI overlay, cartoon, anime, illustration, painting, drawing, screen with text, monitor with data"

clips = []
clip_counter = [0]

def clip(frames, narration, prompt):
    clip_counter[0] += 1
    cid = f"clip{clip_counter[0]:03d}"
    return {
        "id": cid,
        "frames": frames,
        "narration": narration,
        "prompt": prompt
    }

# ============================================================
# ACT I: COLD OPEN — THE IRAN WAR BEGINS
# ============================================================
act1_clips = []

act1_clips.append(clip(257,
    "March first, 2026. In the pre-dawn darkness over the Persian Gulf, the sky over Tehran flashed white.",
    "Extreme wide aerial shot over a vast dark desert at night, distant horizon erupting in massive orange and white explosions, shockwave rings rippling outward, stars still visible above the violence below, cinematic slow motion"
))

act1_clips.append(clip(257,
    "American and Israeli aircraft had crossed Iranian airspace. The target: the Islamic Republic's nuclear and military command infrastructure.",
    "Tight cinematic shot of afterburners blazing on military jets banking hard over black ocean water at night, orange flame trails cutting through total darkness, motion blur on wingtips, dramatic low angle"
))

act1_clips.append(clip(193,
    "Within hours, reports confirmed the unthinkable. Ayatollah Ali Khamenei, Iran's Supreme Leader for thirty-four years, had been killed in the strikes.",
    "Slow push-in on a massive crowd gathered in a public square at night, lit only by candles and phone screens, hundreds of thousands of people, overhead wide drone shot pulling back to reveal the full scale of the gathering"
))

act1_clips.append(clip(257,
    "Senior commanders of the Islamic Revolutionary Guard Corps — the IRGC — were eliminated alongside him. The Iranian command structure, built over decades, had been decapitated in a single night.",
    "Cinematic close-up of military medals and insignia laid out on a dark surface, single overhead light illuminating them, a slow dolly shot moving across the objects, empty chairs visible in background"
))

act1_clips.append(clip(193,
    "Within forty-eight hours, Iran's new leadership had issued one response. A single order. Close the Strait of Hormuz.",
    "Aerial drone shot looking straight down on the narrow blue-green waters of a strait between two rocky coastlines, a lone military patrol vessel cutting a white wake through calm water, ominous and still"
))

act1_clips.append(clip(257,
    "Ben Rhodes, former Deputy National Security Advisor, called it, in a phrase that would echo across every financial news terminal on Earth: The Great Lie of War. A conflict sold on certainty, delivered into chaos.",
    "Slow dolly shot through an empty government briefing room, leather chairs pushed back from a long conference table, American flags drooping in stillness, late afternoon light slanting through venetian blinds across the floor"
))

act1_clips.append(clip(193,
    "In Washington, the administration insisted the operation was proceeding ahead of schedule. The markets heard something different.",
    "Wide shot of a massive trading floor at the opening bell, hundreds of traders at screens, red indicators flashing across digital boards, a blur of urgent movement, overhead crane shot pulling back and up"
))

act1_clips.append(clip(257,
    "S&P futures dropped two and a half percent in overnight trading. The Dow opened down six hundred points. And the oil price — which had already been climbing for weeks — began its vertical ascent.",
    "Extreme close-up of hands gripping the edge of a trading desk, knuckles white, blurred screens reflecting red light on the face of the unseen trader, cinematic depth of field"
))

act1_clips.append(clip(193,
    "This is the story of March 2026. Thirteen days that shook the financial world. A war, an oil shock, a private credit collapse, and a Federal Reserve with no good options.",
    "Slow aerial drone push forward over a massive oil refinery lit up at dusk, towers of steel and pipe and flame glowing orange against a purple-pink sky, scale overwhelming, industrial cathedral"
))

act1_clips.append(clip(257,
    "Martin Wolf of the Financial Times had been warning about this scenario for years. Asked directly whether the Iran war could cause a new oil crisis, his answer was unambiguous.",
    "Close-up of a broadsheet newspaper on a dark mahogany desk, hands smoothing the pages open, headline area obscured by camera angle, reading glasses resting beside it, a cup of tea steaming in soft morning light"
))

act1_clips.append(clip(257,
    "As Wolf told The Monetary Matters Network: 'If one wanted to think of a nightmare disruption scenario for the world economy, it would be a war in the Gulf. If the straits were closed for three months or more, we would be looking at a major jolt to the world economy.'",
    "Wide establishing shot of a centuries-old stone library interior, tall shelves receding into shadow, a single reading lamp illuminating an open desk, heavy silence, dust motes drifting in a beam of afternoon light through a high window"
))

act1_clips.append(clip(193,
    "Wolf added something that investors needed to hear: 'Persia was the first great empire in human history. They've been around a long time. The Persians fought the Roman Empire to a standstill over centuries. Not to be underestimated.'",
    "Tight shot of ancient stone relief carvings on a weathered wall, Achaemenid Persian figures in procession, deep shadows in the carved grooves, a hand slowly tracing the detail without touching, cinematic shallow focus"
))

act1_clips.append(clip(257,
    "Nobel laureate economist Joseph Stiglitz, speaking to The Monetary Matters Network, was equally stark. The Middle East war, he said, threatened economic chaos — not as a distant possibility, but as an imminent reality.",
    "Wide shot of a stone amphitheater at dusk, empty stone seats stretching up in a semicircle, a single lit podium at the base illuminating the surrounding darkness, dramatic single-source light"
))

act1_clips.append(clip(193,
    "Stiglitz pointed to what economists call the transmission mechanism: war disrupts energy supply, energy supply drives input costs across every industry, input costs drive inflation, and inflation destroys purchasing power.",
    "Cinematic tracking shot moving slowly through an industrial warehouse, shelves stacked floor to ceiling with goods, a forklift frozen mid-aisle, overhead fluorescent lights casting clinical shadows, sense of stillness and abundance both"
))

act1_clips.append(clip(257,
    "We have seen this movie before. The oil embargo of 1973. The Iranian revolution of 1979. Each time, the developed world was unprepared. Each time, the economic consequences lasted not months but years.",
    "Archival-style sepia tone footage aesthetic: a long line of vehicles at a gas station in twilight, attendants in overalls, drivers standing outside their cars, the queue stretching around the block and out of frame"
))

act1_clips.append(clip(193,
    "But 2026 is different from 1973 in one critical respect. The global economy now carries forty trillion dollars in US federal debt alone. The shock absorbers are gone.",
    "Extreme close-up of a rusted shock absorber spring on industrial equipment, corroded and compressed, held together by fraying metal threads, dark workshop light, depth-of-field focus on the stress fracture"
))

act1_clips.append(clip(257,
    "ITM Trading framed the stakes in stark historical terms: the 1973-74 oil embargo led to nationwide fuel shortages. Oil prices doubled, then quadrupled. It fed an inflation crisis that was already building, crushing purchasing power and raising interest rates to levels the country could barely survive.",
    "Cinematic slow push into a rusted industrial oil pump in a field at golden hour, the pump arm rising and falling in hypnotic rhythm, long dry grass swaying around the base, vast empty sky above"
))

act1_clips.append(clip(193,
    "Today, the starting conditions are worse. In 1973, the United States was at peak industrial production. In 2026, it is carrying the weight of decades of financialization, outsourcing, and debt accumulation.",
    "Wide shot of a shuttered factory exterior, windows boarded, overgrown parking lot with cracked asphalt, a lone security light still burning at the entrance, long shadows at dusk"
))

act1_clips.append(clip(257,
    "The Iran war was now two weeks old. Iranian proxies in Yemen, Iraq, and Lebanon had activated. US forces in the region were under pressure. And the financial system was beginning to fracture along fault lines that had been building for years.",
    "Aerial shot looking down on a naval carrier strike group in formation on dark ocean water, ships arranged in protective pattern around the central carrier, white wakes cutting the blue-black surface, ominous precision"
))

act1_clips.append(clip(193,
    "Retail investors watching financial channels on YouTube were already sounding the alarm. As one commenter put it on ITM Trading's live stream on Day Six: 'This month America has to sell three hundred billion dollars in treasuries, just to help pay one trillion in interest on its thirty-nine trillion dollar debt.'",
    "Close-up of a smartphone screen glowing in a dark room, the light reflecting on a face just out of frame, comments scrolling upward in a live feed, the blue-white glow the only light source"
))

act1_clips.append(clip(257,
    "That retail anxiety was the signal. When ordinary savers start talking about treasury auctions, something fundamental has shifted. The war had not merely disrupted energy markets. It had ripped open a conversation about the sustainability of American debt — a conversation that Wall Street had been carefully avoiding for years.",
    "Slow overhead drone shot over a residential suburb at night, houses lit from within in warm amber, the grid of streets stretching to the horizon, ordinary life visible in every window, a profound stillness"
))

act1_clips.append(clip(193,
    "And then, on March third, the data arrived that would define the crisis. From the Strait of Hormuz: ship traffic had collapsed.",
    "Dramatic aerial shot directly over the narrow strait between two landmasses, the water perfectly still and empty where dozens of vessels should be moving, eerie absence, deep blue-green water stretching to haze"
))

act1_clips.append(clip(257,
    "According to Projekt 100X, citing maritime tracking data, the Strait of Hormuz had gone from one hundred and thirty-eight ships per day to just four. A ninety-four percent reduction. Nearly overnight.",
    "Extreme wide drone shot over a vast empty harbor at dawn, enormous container ship berths sitting completely empty, cranes still, water flat and glassy, the scale of the emptiness only visible from altitude"
))

act1_clips.append(clip(193,
    "Through that narrow waterway, twenty percent of the world's oil flows every single day. Twenty percent of global LNG. The fertilizer that feeds a billion people. The fuel that powers Asian manufacturing.",
    "Cinematic slow-motion shot of a massive LNG tanker underway at full speed through blue open ocean, the enormous vessel gleaming white in strong sunlight, bow wave spreading wide, scale conveyed by comparison to escort vessel"
))

act1_clips.append(clip(257,
    "The strait is twenty-one miles wide at its narrowest point. For decades, it has been the jugular vein of the global economy. Now, for the first time in history, that vein had been cut.",
    "Tight cinematic shot of water rushing through a narrow channel between two rocky outcroppings, current powerful and fast, white foam against dark rock, the water disappearing into shadow around a bend"
))

# ============================================================
# ACT II: OIL SHOCK — BRENT AT $115, HORMUZ DARK
# ============================================================
act2_clips = []

act2_clips.append(clip(257,
    "Act Two. The Oil Shock.",
    "Sweeping aerial drone shot over an oil refinery complex at dusk, flames from flare stacks burning bright orange against a darkening purple sky, vast industrial structures stretching to the horizon, steam clouds drifting slowly"
))

act2_clips.append(clip(257,
    "The moment the Hormuz closure was confirmed by satellite imagery on March third, the oil market moved in ways traders had not seen since 2008. Brent crude, which had been trading around eighty-five dollars per barrel, gapped up through ninety, through one hundred, through one ten.",
    "Extreme close-up of a pressure gauge on a massive steel pipe, the needle climbing rapidly in the red zone, industrial grease and grime on the metal surface, ambient steam blurring the background"
))

act2_clips.append(clip(193,
    "Within seventy-two hours of the closure confirmation, Brent had reached one hundred and fifteen dollars. At peak intraday trading, it touched one hundred and twenty. A thirty-five percent surge in less than a week.",
    "Cinematic shot of crude oil pouring from a large industrial pipe into a dark holding pool below, the black viscous liquid catching harsh industrial light, iridescent rainbow swirls forming on the surface, slow motion"
))

act2_clips.append(clip(257,
    "Maggie Lake, anchoring Talking Markets, posed the question that had become inescapable: Is one hundred dollars oil the new normal? The answer, according to every analyst she interviewed, was not just yes — but that one hundred might be the floor.",
    "Wide cinematic shot looking up at a towering oil drilling platform from deck level, steel lattice disappearing into a grey overcast sky, workers in hard hats small against the industrial scale, seabirds circling far above"
))

act2_clips.append(clip(193,
    "The physics of the crisis were simple. You cannot reroute twenty percent of global oil supply through alternative passages without adding weeks and significant cost to every journey. The Cape of Good Hope route from the Persian Gulf to Europe adds eleven to fourteen days of sailing time.",
    "Dramatic wide shot of a massive oil tanker navigating around a rocky cape in rough seas, waves crashing against the hull, spray exploding upward, the ship straining visibly against the ocean swell"
))

act2_clips.append(clip(257,
    "As Market Insider reported, tanker charter rates had already exploded. Rates that were one hundred and thirty thousand dollars per day just two weeks earlier were now four hundred thousand dollars a day. And rising.",
    "Close-up of a ship captain's hands navigating chart tables, thick fingers tracing a course across nautical maps, the charts weighted down at corners, compass and parallel rulers visible, amber desk lamp"
))

act2_clips.append(clip(193,
    "Those costs — every dollar of every extra day of every extra mile — feed directly into the price at the pump. This was not a market abstraction. It was an energy tax on every driver, every farmer, every factory in the world.",
    "Slow motion close-up of a fuel pump nozzle entering a vehicle tank, the digital display numbers blurring upward, a hand gripping the handle firmly, a gas station canopy reflected in the car's metallic surface"
))

act2_clips.append(clip(257,
    "But oil was only the first shock. The second was liquefied natural gas — LNG. And here, the numbers were even more alarming.",
    "Wide aerial shot of an LNG export terminal at night, massive spherical storage tanks glowing under industrial floodlights, loading arms extended over a docked tanker, steam clouds rising into the dark sky"
))

act2_clips.append(clip(193,
    "Twenty percent of global LNG supply — the fuel that heats European homes, powers Asian industry, and underpins the energy security of a dozen countries — was now offline.",
    "Cinematic close-up of industrial natural gas flames burning at a processing facility, the blue-orange fire in perfect focus against a blurred refinery background, the raw power of energy made visible"
))

act2_clips.append(clip(257,
    "LNG spot prices surged one hundred and thirty-seven percent in five days. Not five months. Five days. The speed of the move shattered every historical precedent for energy price adjustment.",
    "Wide shot of a European city street at night in winter, apartment windows glowing warmly, a gas utility worker in high-visibility vest examining a meter connection on the side of a building, quiet urgency"
))

act2_clips.append(clip(193,
    "And then QatarEnergy — the world's largest LNG exporter — declared force majeure. Contracts that companies had relied upon for years, worth billions of dollars, were suddenly void.",
    "Dramatic aerial shot of the Qatar coastline at dusk, vast industrial LNG facilities stretching along the shore, massive storage tanks in rows, the Persian Gulf shimmering orange-gold in the setting sun"
))

act2_clips.append(clip(257,
    "Force majeure is the legal escape hatch of last resort. It means: circumstances beyond our control have made performance impossible. When QatarEnergy invoked it, the message to global energy markets was clear. The old supply chains were finished.",
    "Close-up of formal legal documents on a conference table, a fountain pen resting beside them, hands visible at the edge of frame, the text obscured by angle, heavy signet ring glinting under boardroom lights"
))

act2_clips.append(clip(193,
    "The food price dimension of the Hormuz closure received less attention — but it may prove the most consequential for the world's most vulnerable populations.",
    "Cinematic wide shot of a vast wheat field in golden afternoon light, stalks bending gently in a warm breeze, the field stretching to a low horizon under a pale blue sky, solitary grain elevator visible in far distance"
))

act2_clips.append(clip(257,
    "As Market Insider's analysis made clear: the strait carries not just oil and gas, but the petrochemical feedstocks from which most of the world's fertilizer is made. No fertilizer, no yield. No yield, no food. The 2026 growing season was already planted. The 2027 season was now in jeopardy.",
    "Aerial drone shot slowly moving over a patchwork of agricultural fields from high altitude, geometric shapes of different crops in greens and yellows, irrigation channels glinting silver, the scale of global agriculture made visible"
))

act2_clips.append(clip(193,
    "The geopolitical logic behind the closure was not irrational. Iran could not match US military firepower. But it had always held one card: the Hormuz stranglehold. With its leadership killed, its successor government had nothing left to lose.",
    "Wide low-angle shot looking along the deck of a navy destroyer at sea, grey hull cutting through choppy dark water, radar array spinning on the superstructure, an overcast sky pressing down on the scene"
))

act2_clips.append(clip(257,
    "As Joe Blogs' analysis noted for his audience: the United States had now taken the extraordinary step of allowing additional Russian oil to be sold — an emergency waiver from its own sanctions regime — in order to stabilize global energy markets.",
    "Cinematic aerial wide shot of a trans-Siberian oil pipeline corridor cutting through dense boreal forest, the pipeline a silver line vanishing to a point in both directions, frost on the ground, late winter light"
))

act2_clips.append(clip(193,
    "The irony was brutal. A war ostensibly fought to constrain Iran had, within two weeks, forced the United States to loosen sanctions on Russia. As one Eurodollar University commenter noted: 'Oil to $200 a barrel, mark my words.'",
    "Close-up of oil rig machinery in full operation, heavy steel drill components turning and grinding, grease glistening under work lights, the weight and power of extraction visible in every bolt and weld"
))

act2_clips.append(clip(257,
    "Ed Yardeni, recording his analysis late on a Sunday evening in his now-iconic session titled 'Between Iran and a Hard Place,' captured the dilemma facing the American economy with characteristic precision.",
    "Wide shot of a home study at night, bookshelves lining the walls behind an empty desk chair, a single lamp creating a pool of warm light, framed degrees and certificates on the walls, a cup of coffee steaming"
))

act2_clips.append(clip(193,
    "The American economy had a structural advantage that the 1973 crisis lacked: the shale revolution had made the United States a net oil exporter. Higher prices were a windfall for domestic producers, not purely a burden.",
    "Aerial drone shot over a shale oil field in the Texas Permian Basin, dozens of pump jacks operating across flat scrubland, each one nodding in rhythmic motion, late afternoon light casting long shadows across the dust"
))

act2_clips.append(clip(257,
    "But the distribution of that windfall was profoundly uneven. Energy company shareholders — concentrated in the top ten percent of the wealth distribution — would profit. Everyone who drives, farms, heats a home, or operates any business dependent on transportation would pay.",
    "Cinematic slow tracking shot through an upscale residential neighborhood at dusk, large houses lit from within, luxury vehicles in driveways, contrasted immediately with a wide shot of a working-class strip mall, neon signs, modest cars"
))

act2_clips.append(clip(193,
    "Steve Hanke, economist and Johns Hopkins professor, appeared on David Lin's show warning of a fifty-year crisis breaking a market bubble. His message: oil at one hundred and eight dollars at time of recording. If we hit one hundred and forty, 'two thousand and eight will look like a picnic.'",
    "Dramatic close-up of a glass thermometer in extreme close-up, the red mercury column rising steadily past a critical mark, the glass tube vibrating slightly, clinical white background"
))

act2_clips.append(clip(257,
    "Polymarket, the prediction market platform, was pricing in a four percent chance of two hundred dollar oil by end of March. But that number tells a more interesting story than it seems. Twenty-five million dollars in volume had been bet on that contract. Institutional money was hedging the tail risk.",
    "Overhead close-up of a green felt gaming table with cards and chips arranged in a complex pattern, a hand hovering over the arrangement, calculating odds, dramatic overhead single-source light"
))

act2_clips.append(clip(193,
    "The retail investment community was already making its moves. As one commenter on Adam Taggart's Thoughtful Money channel wrote: 'Most people are buying oil stock. TPET stock has doubled in a week.' The crisis was creating its first wave of winners alongside its victims.",
    "Close-up of a hand scrolling through a brokerage app on a smartphone, green numbers and portfolio values visible in the glow, the face of the viewer reflected faintly in the screen glass, focused and intent"
))

act2_clips.append(clip(257,
    "The longer-term question haunting energy economists was duration. Tanker route rerouting can patch short-term supply gaps. But if the strait remained closed for three months — the threshold Martin Wolf had identified — the rerouting math would not save the global economy.",
    "Sweeping aerial shot of a busy shipping lane in calm open ocean, a dozen tankers visible at different distances stretching to the horizon, their wakes intersecting, the immense organized flow of global trade"
))

act2_clips.append(clip(193,
    "And the ships that had been transiting the Hormuz before the closure — one hundred and thirty-eight per day — were not simply redirected. Many were anchored. Waiting. Owners watching the conflict, calculating insurance costs, waiting for a ceasefire that had not come.",
    "Wide aerial shot of a massive anchorage area where dozens of large vessels sit motionless in calm turquoise water, anchors down, cargo not moving, the scene appearing almost pastoral despite its economic gravity"
))

act2_clips.append(clip(257,
    "The oil shock of March 2026 had one dimension that made it qualitatively different from all prior oil shocks: it arrived simultaneously with a financial crisis that had nothing to do with oil. A crisis that had been quietly building in the shadows of the private credit market.",
    "Slow zoom into the dark glass facade of a massive financial headquarters tower in a city at night, reflections of the city skyline distorted in the glass, a single window lit on an upper floor, the rest dark"
))

# ============================================================
# ACT III: PRIVATE CREDIT CRISIS
# ============================================================
act3_clips = []

act3_clips.append(clip(257,
    "Act Three. The Private Credit Crisis. Three hundred billion dollars in contagion risk. And almost nobody saw it coming.",
    "Slow dolly shot through a darkened bank vault corridor, polished steel safe deposit boxes receding into shadow on both sides, security lights creating a dim amber glow, silence and stillness suggesting enormous hidden value"
))

act3_clips.append(clip(257,
    "To understand what happened in private credit, you need to understand what private credit actually is. Unlike public markets — stocks, bonds, exchange-traded securities — private credit is money lent directly from large institutional investors to private companies, bypassing traditional banks.",
    "Cinematic wide shot looking down a long corridor of a modern office building, glass-walled meeting rooms visible on both sides, people in formal business attire visible through glass moving silently, shot from above"
))

act3_clips.append(clip(193,
    "The private credit market grew explosively after the 2008 financial crisis, when banking regulations forced traditional lenders to reduce risk. Private credit funds stepped into the gap. By 2026, the market had grown to well over two trillion dollars globally.",
    "Aerial drone shot over a gleaming financial district at golden hour, towers of glass and steel casting long shadows, the geometry of modern capitalism visible from above, the city humming with unseen transactions"
))

act3_clips.append(clip(257,
    "What made private credit attractive to investors was yield. When interest rates were near zero, private credit funds offered six, seven, eight percent returns. Pension funds, endowments, sovereign wealth funds poured money in.",
    "Close-up of a thick printed investment prospectus on a conference table, performance numbers in columns visible but not legible, hands turning pages, a fountain pen held in one hand, a glass of water catching window light"
))

act3_clips.append(clip(193,
    "What made private credit dangerous was opacity. Unlike public bonds, private credit loans are not marked to market daily. Fund managers have significant discretion in how they value the loans on their books. This, as Stoic Finance documented in devastating detail, created systematic incentives to deceive.",
    "Close-up of a magnifying glass hovering over printed fine-print contract language, the text in soft focus beneath the lens, the glass catching overhead light, the act of scrutiny itself made visual"
))

act3_clips.append(clip(257,
    "The first warning shot came in February. BlackRock's twenty-six billion dollar private credit fund began receiving an unusual volume of redemption requests. In private funds, investors typically must give advance notice — sometimes thirty, sixty, or ninety days — before withdrawing capital.",
    "Cinematic wide shot of the BlackRock headquarters building in Manhattan from the street, the imposing stone and glass facade, pedestrians rushing past below, American flags above the entrance, overcast sky"
))

act3_clips.append(clip(193,
    "BlackRock honored some redemptions. And then, in early March, it began denying the rest. 'Redemptions limited.' Four words that sent a chill through every institutional investor that had money in private credit anywhere in the world.",
    "Dramatic close-up of an imposing wooden door with a polished brass nameplate, the door closed and locked, a brass knocker unmoving, hard shadows from overhead light, the weight of institutional refusal"
))

act3_clips.append(clip(257,
    "As Eurodollar University's Jeffrey Snider reported it: 'Now it's Morgan Stanley's turn. Yesterday it was Cliffwater. Before that it was BlackRock and Blackstone and of course Blue Owl.' Morgan Stanley's eight billion dollar North Haven private income fund was the latest to get hit with massive withdrawals — and the latest to deny most of them.",
    "Slow zoom into the Morgan Stanley logo on a glass building facade, the reflection of the city street in the glass, pedestrians crossing in the reflection, the institution's scale implied by the perspective"
))

act3_clips.append(clip(193,
    "Cliffwater. Blue Owl. Blackstone's twenty-one billion dollar fund. Then Morgan Stanley's North Haven. In the space of two weeks, four of the largest private credit funds in the world had restricted or denied redemptions. The total assets under restriction: approaching three hundred billion dollars.",
    "Wide shot of a Wall Street street scene at opening time, suited workers moving urgently through the financial district, yellow taxis, steam from subway grates, the physical infrastructure of global finance"
))

act3_clips.append(clip(257,
    "Stoic Finance identified the contagion risk with precision: three hundred billion dollars is not the exposure itself. It is the exposure that can cause further failures. Each denied redemption means an investor somewhere cannot meet their own obligations — their own redemptions, their own margin calls, their own debt service.",
    "Dramatic cinematic shot of a line of dominoes arranged in a long curve on a dark table, the first falling in slow motion, the cascade beginning, each piece toppling the next, extreme close-up"
))

act3_clips.append(clip(193,
    "This is how financial contagion spreads. Not through direct loss, but through the chain of obligations that can no longer be met. In 2008, it was mortgage-backed securities. In 2026, it was private credit.",
    "Slow motion water drip falling into a still pool, the perfect concentric rings spreading outward from the impact point, each ring generating the next, the pattern of contagion made beautiful and visible"
))

act3_clips.append(clip(257,
    "As Eurodollar University noted, the parallel to 2008 was unmistakable — and haunting. In 2007 and 2008, there was an oil price shock even bigger than the current one. It has been largely forgotten, overshadowed by the deflationary calamity that overtook it. Before it did, while oil was soaring, what almost every central bank missed was the credit implosion building beneath the surface.",
    "Wide shot of a major urban intersection during rush hour, streams of vehicles flowing in four directions, the intersection working normally from above, but a cinematic pull-back reveals one lane completely blocked and backing up"
))

act3_clips.append(clip(193,
    "The mechanism was also similar. Private credit loans made during years of low interest rates were now struggling under higher debt service costs. Borrowers who had been managing their debt at four percent were now refinancing at seven, eight, nine percent. Some could not.",
    "Close-up of a heavy iron chain under tension, the links stretching and straining, slight corrosion visible at the stress points, the implied weight enormous, a single link beginning to deform"
))

act3_clips.append(clip(257,
    "The British dimension of the crisis deserves particular attention. As Stoic Finance reported under the headline 'Private Credit Collapses British Economy as Contagion Spreads Globally': two British firms had collapsed in the space of a week, for precisely the same reason as their American counterparts.",
    "Wide aerial shot of the City of London financial district, the Gherkin and Walkie-Talkie towers visible, the Thames winding through the frame, grey overcast sky, the ancient-new collision of British finance"
))

act3_clips.append(clip(193,
    "The UK private credit market had expanded faster, relative to GDP, than almost anywhere else. British pension funds — which manage the retirement savings of millions of workers — had been the most aggressive allocators to the sector, drawn by the yield premium.",
    "Cinematic wide shot of a traditional British high street in a small town, an old bank branch building prominent, red double-decker bus passing, people walking with purpose, the quiet financial dependency of ordinary lives"
))

act3_clips.append(clip(257,
    "When those funds began to restrict redemptions, the ripple effects reached quickly into the real economy. British companies that had been planning to raise capital through private credit refinancing found their access suddenly cut off. Investment plans were cancelled. Jobs that would have been created were not.",
    "Slow dolly shot through a half-built construction site, scaffolding up but work halted, tools laid down, hard hats on a table but no workers, late afternoon light catching the dust suspended in the still air"
))

act3_clips.append(clip(193,
    "Commenter sentiment on the Stoic Finance channel was raw and furious. 'These institutions have ZERO oversight, regulation, or fiduciary obligations,' one viewer wrote. 'Private profits but public losses. Disgusting.' Another: 'They're operating like a bank without the regulations that banks must follow.'",
    "Wide shot of ordinary people in a bank branch queue, waiting patiently at roped barriers, a teller window in the background, the timeless image of small depositors trusting large institutions with their savings"
))

act3_clips.append(clip(257,
    "The YouTube commenter who wrote 'OPM Inc. — Other People's Money' may have been sarcastic. But the analysis was correct. Private credit funds exist to profit from the deployment of other people's money, in markets with minimal transparency and effectively no public accountability.",
    "Overhead cinematic shot looking straight down into an atrium of a major financial institution, escalators moving people between floors, a lobby fountain in the center, the geometry of institutional scale"
))

act3_clips.append(clip(193,
    "Chris Irons, speaking to Adam Taggart on Thoughtful Money, described the private credit meltdown threat in terms that connected the oil shock and the credit crisis into a single compound catastrophe.",
    "Close-up of two hands over a conference table, one pointing to invisible data, the other making a joining gesture, the physicality of financial argument, blurred background of a professional office environment"
))

act3_clips.append(clip(257,
    "The compound nature of the crisis was precisely what made it so dangerous. An oil shock alone, the economy can absorb — painful, yes, but finite. A private credit crisis alone, regulators can manage — painful, but containable. Both together, simultaneously, while the Federal Reserve has its hands tied? That is where systemic risk lives.",
    "Dramatic wide aerial shot of a storm system over an ocean, two separate storm cells visible from altitude, both rotating, moving toward each other on a collision course, the vast scale of the atmospheric violence"
))

act3_clips.append(clip(193,
    "By the second week of March, the compound crisis had acquired a name on financial channels: Warflation. A portmanteau coined in the title of Soar Financially's analysis: 'WARFLATION: Oil Shock Plus Debt Crisis Could Break the Economy.'",
    "Close-up of a weathered brick wall with layers of paint showing different eras, the latest layer peeling to reveal older layers beneath, the metaphor of compounding crises written in material decay, natural light"
))

act3_clips.append(clip(257,
    "Dr. Steve Keen, speaking to Soar Financially, described warflation as the convergence of three simultaneous pressures: the supply-side cost shock from energy, the demand destruction from rising rates, and the debt deflation threatening from the private credit market.",
    "Cinematic wide shot of a pressure cooker on a stove, steam escaping from three different release valves simultaneously, the pot shaking slightly, the kitchen blurred behind it, the physics of compounded pressure"
))

act3_clips.append(clip(193,
    "This was not a theoretical exercise. By mid-March 2026, Joe Blogs reported from the UK that gas prices were already soaring, the energy shock was hitting household budgets in real time, and the private credit crisis was beginning to register in corporate credit spreads.",
    "Slow tracking shot through a petrol station forecourt at night, puddles reflecting neon price sign lights in the wet tarmac, a lone car refueling under the canopy, the driver visible as a silhouette inside the vehicle"
))

act3_clips.append(clip(257,
    "The financial system had entered what Bankless's hosts called, citing Ethereum founder Vitalik Buterin, 'the chaotic era.' We had moved, they said, from the stable era to the chaotic era. Wars, AI disruption, overall market jitters — and underneath it all, a credit system under siege.",
    "Sweeping aerial drone shot over a major city at dusk in a light rain, streets reflecting red and white light trails of traffic, the city humming with life but an atmospheric heaviness pressing from above"
))

# ============================================================
# ACT IV: THE FED'S IMPOSSIBLE BIND
# ============================================================
act4_clips = []

act4_clips.append(clip(257,
    "Act Four. The Federal Reserve. The Impossible Bind.",
    "Wide establishing shot of the Federal Reserve building in Washington DC at dusk, classical columns illuminated by ground lighting, the American flag visible on the roof, cars streaming past with light trails"
))

act4_clips.append(clip(257,
    "The Federal Reserve has two mandates: price stability and maximum employment. In March 2026, it was failing both — and the tools available to fix one would worsen the other. This is stagflation. And it is the nightmare scenario that every central bank economist studies, and hopes never to face.",
    "Close-up of a traditional scale balance in perfect equilibrium, both pans identical in weight, then a single gold coin placed on one side, the balance tilting decisively, the mechanism swinging to a new resting point"
))

act4_clips.append(clip(193,
    "The arithmetic was simple and brutal. Oil at one hundred and fifteen dollars per barrel adds approximately one and a half percentage points to core inflation within ninety days. The Fed's target: two percent. Pre-crisis CPI: already running at three point four percent.",
    "Cinematic close-up of a precision measuring instrument with a needle moving steadily into a red zone, the dial calibrated in fine gradations, industrial setting, a hand hovering near but not touching a warning switch"
))

act4_clips.append(clip(257,
    "Martin Wolf put it plainly: 'Being completely dependent on fuels imported through very dangerous places like the Strait of Hormuz is very problematic and this has really underlined it.' The inflation transmission from energy to all other goods is not a question of whether. Only of when and how much.",
    "Wide shot of a vast logistics hub, hundreds of trucks lined up at loading docks, workers moving freight with electric pallet jacks, the entire system visible as an interconnected web of movement and cost"
))

act4_clips.append(clip(193,
    "The Fed's response options were paralyzed. Raise rates to fight inflation? The economy was already slowing. Corporate debt service costs were rising. The private credit system was cracking. Rate hikes would pour gasoline on a credit fire.",
    "Slow motion close-up of a hand turning a combination lock, clicking through numbers, the mechanism of control and constraint made physical, the click of each position deliberate and irreversible"
))

act4_clips.append(clip(257,
    "Cut rates to stimulate growth? Oil-driven inflation was already threatening to spiral. Cutting rates with inflation rising would destroy the Fed's credibility, trigger a dollar selloff, and potentially unleash a currency crisis on top of an energy crisis on top of a credit crisis.",
    "Dramatic wide shot of a dam with water spilling over the top, controlled overflow through multiple channels, engineers visible on the walkway above, the tension between containment and release made viscerally physical"
))

act4_clips.append(clip(193,
    "The Monetary Matters Network's discussions with both Martin Wolf and Joseph Stiglitz centered on this bind. Hold rates, and the recession deepens. Move rates, and you risk making one crisis worse while trying to fix another.",
    "Cinematic overhead shot of a chess game mid-match, pieces arranged in a complex position, one player's king in a difficult position with no clear escape, the board geometry communicating constraint"
))

act4_clips.append(clip(257,
    "Joseph Stiglitz, whose Nobel Prize was partly for the economics of information asymmetry, pointed to the K-shaped economy as the deeper structural problem. The Federal Reserve's models assume a relatively homogeneous economy. The actual American economy of 2026 was anything but.",
    "Wide aerial shot of a neighborhood where large luxury homes on one side of a boulevard directly face modest older houses on the other, the economic divide rendered as stark visual geography"
))

act4_clips.append(clip(193,
    "The K-shape: two groups, moving in opposite directions. The top ten percent — asset owners, stockholders, property owners, private credit investors — riding an endless wave of capital appreciation. The bottom ninety percent — workers, renters, debtors — experiencing something that felt, in their daily lives, indistinguishable from a recession.",
    "Cinematic wide shot of a busy luxury hotel entrance, valets parking expensive cars, guests with designer luggage, and immediately cutting to a slow shot of a Walmart parking lot at dawn, working families with shopping carts"
))

act4_clips.append(clip(257,
    "Rosenberg Research had been documenting this divergence for months. Their analysis was unambiguous: for the bottom ninety percent of American households by income, a recession was not approaching. It was already underway. Credit card delinquencies at multi-decade highs. Auto loan defaults climbing. Food bank usage at record levels.",
    "Close-up of hands sorting through a handful of credit cards on a kitchen table, a bill visible in the background, the quiet economic anxiety of a household working through its options, natural window light"
))

act4_clips.append(clip(193,
    "The payroll data had already been flashing warning signs before the Iran war began. Negative revisions to prior months' job numbers had been quietly appearing in the data — a pattern that historically precedes official recession declarations.",
    "Slow zoom into an open laptop on a desk, a spreadsheet of numbers visible but not legible, a coffee mug steaming beside it, the ambient hum of a home office, late evening"
))

act4_clips.append(clip(257,
    "Now, with the oil shock adding to household budgets and the private credit restriction threatening corporate investment, the leading indicators were deteriorating in real time. And the Fed was watching, and waiting, and doing nothing — because there was nothing it could do.",
    "Wide shot of the Federal Reserve building from across the street, the building imposing and static, traffic flowing past, a maintenance worker hosing down the steps, the paradox of institutional power and powerlessness"
))

act4_clips.append(clip(193,
    "Dr. Komal Sri-Kumar, speaking to Soar Financially, described the stagflation scenario of 2026 as in some ways worse than the 1970s. In the seventies, there was no twenty-six trillion dollar household debt overhang. There was no forty trillion dollar federal debt. The system then had slack. Today, every circuit breaker has been used.",
    "Cinematic close-up of an electrical fuse box with every circuit breaker tripped to the off position, red indicators showing failure across the board, an electrician's hand moving across the panel, the problem everywhere"
))

act4_clips.append(clip(257,
    "Econ Lessons, breaking down the mechanics for retail investors, explained why the inflation-recession trap was different this time: 'When energy prices rise, firms face higher production costs. If they pass those costs on, you get cost-push inflation. If they absorb them, you get profit compression. Either way, investment falls.'",
    "Wide cinematic shot of a small manufacturing plant floor, workers at stations, machinery running, but the camera slowly tracking past idle equipment and unused workstations as it moves through the facility"
))

act4_clips.append(clip(193,
    "This investment collapse was already beginning to show up in the data. The ISM Manufacturing Index had been below fifty — indicating contraction — for seventeen of the past twenty months. The oil shock would not help.",
    "Close-up of industrial machinery sitting idle, conveyor belt stopped, a thin layer of dust beginning to settle on the belt surface, overhead industrial lights creating stark shadows on the still equipment"
))

act4_clips.append(clip(257,
    "The Federal Reserve's Jerome Powell had spent years building credibility on inflation. He had earned that credibility by hiking rates aggressively in 2022 and 2023, inducing pain to bring inflation down. Now, that credibility was the only thing preventing an inflation expectations spiral.",
    "Dramatic slow zoom into the Federal Reserve seal on a polished wood surface, the eagle and shield in bas-relief, the symbolism of institutional authority, warm light catching the carved detail"
))

act4_clips.append(clip(193,
    "But credibility is not infinite. If oil-driven inflation persisted for six months, the public's inflation expectations — anchored with such difficulty — could become unanchored again. And unanchored inflation expectations are, in central banking, the nightmare from which there is no easy waking.",
    "Slow motion close-up of a tightly wound spring held under compression by two metal plates, the energy stored in the metal visible in its geometry, the potential for release enormous and contained only by the slenderest margin"
))

act4_clips.append(clip(257,
    "The Ken McElroy Podcast, focusing on the real estate dimension, captured the liquidity crisis in practical terms. When money stops flowing — when private credit gates, when banks tighten — real estate transactions slow, values fall, and the wealth that hundreds of millions of Americans hold in their homes begins to erode.",
    "Aerial drone shot over a suburban development where half-finished houses sit in various stages of completion, lumber stacked but unused, concrete foundations poured but nothing built above them, work halted"
))

act4_clips.append(clip(193,
    "As one Ken McElroy commenter noted: 'If the government does nothing and liquidity is tightening, we can have another situation where people can't borrow, people can't buy anything.' The circular logic of credit collapse: when credit tightens, demand falls; when demand falls, credit quality worsens; when credit quality worsens, lending tightens further.",
    "Close-up of a single drop of water falling in extreme slow motion into a still pool, the impact creating a perfect crown of water, then the crown collapsing back, the cycle of impact and withdrawal"
))

act4_clips.append(clip(257,
    "The Federal Reserve was not the only central bank caught in this bind. The European Central Bank, the Bank of England, the Bank of Japan — all faced some version of the same impossible tradeoff. But none faced it with the same debt load. And none faced it while also managing the world's reserve currency.",
    "Wide aerial drone shot over the Frankfurt financial district at dusk, the glass towers of the ECB headquarters visible, the River Main curving around the city, the architecture of European monetary union"
))

act4_clips.append(clip(193,
    "Arthur Hayes, BitMEX founder and arguably the most-watched macro commentator in the digital asset space, had a blunt prediction: the Fed will always print money. Interviewed on Wealthion, his thesis was simple. The debt is too large to service at any meaningful interest rate. Eventually, the Fed will inflate it away. The question is not whether, but when.",
    "Cinematic wide shot of an industrial printing facility at night through large windows, enormous machines operating, the rhythm of mechanical production continuous, warm amber light inside, dark outside"
))

act4_clips.append(clip(257,
    "This view — that money printing is the path of least political resistance — was also the foundation of the bull case for gold, silver, and Bitcoin. If the Fed must ultimately choose between inflation and default, the history of every fiat currency in history suggests which choice they will make.",
    "Slow dolly shot through a museum display of ancient coins from different civilizations, gold and silver coins from Rome, Persia, Byzantine, Athens, each civilization's monetary history visible in metal, dramatic museum lighting"
))

# ============================================================
# ACT V: SAFE HAVENS — GOLD, SILVER, BITCOIN
# ============================================================
act5_clips = []

act5_clips.append(clip(257,
    "Act Five. The Safe Havens. Gold at five thousand. Silver at ninety. Bitcoin as smoke alarm.",
    "Cinematic extreme close-up of a single gold coin on a dark velvet surface, a beam of light catching the surface relief in perfect detail, the precious metal warm and luminous against the surrounding darkness"
))

act5_clips.append(clip(257,
    "Gold had crossed four thousand dollars per ounce in late 2025. By the first week of March 2026, it was trading above five thousand. The move that almost nobody on Wall Street had endorsed had become impossible to ignore.",
    "Wide aerial shot of a gold mine open pit operation, the massive terraced walls of earth spiraling downward, heavy equipment visible as tiny specks on the benches, the scale of the extraction only comprehensible from altitude"
))

act5_clips.append(clip(193,
    "Ronald Stoeferle of Incrementum, whose annual In Gold We Trust report is the most comprehensive institutional analysis of the gold market, had forecast five thousand two hundred dollar gold in his 2025 report. Speaking to Soar Financially, he stood by that target. In fact, he suggested it might be conservative.",
    "Close-up of weathered hands carefully examining a gold coin through a jeweler's loupe, the magnified detail of the coin visible in the lens, the patient scrutiny of a precious metals expert, natural desk light"
))

act5_clips.append(clip(257,
    "Stoeferle's thesis was multidimensional. Central bank gold buying — led by China, India, Russia, and a dozen emerging market economies — had created a structural floor under the price. These buyers were not speculating. They were diversifying away from dollar reserves, deliberately.",
    "Cinematic wide shot of an enormous bank vault interior, rows of gold bars stacked on shelves, a security guard visible in the far background, the sheer weight and density of stored wealth, warm vault lighting"
))

act5_clips.append(clip(193,
    "Stoeferle noted that gold was no longer contrarian. This was arguably his most important observation. For decades, owning gold was an eccentric, contrarian bet — the investment of conspiracy theorists and doomsayers. By 2026, it had entered mainstream institutional portfolios.",
    "Wide shot of a formal investment conference with hundreds of suits seated in rows facing a stage, a speaker at a podium, the visual of institutional consensus, serious faces absorbing serious analysis"
))

act5_clips.append(clip(257,
    "This matters because of what it implies for the bull run's duration. Contrarian moves end when they become consensus. But gold was still being under-allocated by most institutional investors. The average pension fund globally held less than two percent in gold. The recommended allocation, by many macro analysts, was ten to fifteen percent.",
    "Close-up of a pie chart drawn by hand on paper with pen, the gold slice very small, a larger allocation being sketched in with deliberate strokes, the visual of reallocation in progress, warm desk light"
))

act5_clips.append(clip(193,
    "Mark Thornton of the Mises Institute, speaking to ITM Trading's Daniela Cambone, made the Austrian economic case with characteristic directness: gold is not rising. The dollar is falling. The distinction matters enormously for how you think about allocation.",
    "Slow motion extreme close-up of gold being poured in molten form from a crucible into a mold, the glowing liquid metal flowing in a thin stream, the heat visible in the shimmer around it, sparks floating upward"
))

act5_clips.append(clip(257,
    "Gold and silver are not assets that generate income. They do not pay dividends. They are money. Or more precisely, they are the memory of money — the form in which wealth was stored for five thousand years before the invention of central banks and fiat currency.",
    "Cinematic wide shot of an ancient marketplace scene suggested through architecture, stone arches and columns, a single merchant's stall visible, the weight of historical commerce in the weathered stone surfaces"
))

act5_clips.append(clip(193,
    "When fiat currencies debase — which every fiat currency in history eventually has — gold and silver do not rise. They are revealed. The price in dollars goes up because the dollar is going down, not because gold has changed.",
    "Time-lapse style sequence of a candle burning down in close-up, the flame steady, the wax diminishing, the light constant but the material being consumed, the metaphor of fiat erosion"
))

act5_clips.append(clip(257,
    "Now consider silver. At ninety dollars per ounce as of March thirteenth, silver had already accomplished what silver bugs had predicted for two decades. But First Majestic Silver's CEO, speaking to Soar Financially, suggested ninety was not the ceiling. His range: one hundred and fifty to one hundred and seventy-five dollars.",
    "Aerial drone shot over a silver mine in mountainous terrain, terraced excavation visible against grey rock, late afternoon light catching the exposed earth, a processing facility visible at the base of the hill"
))

act5_clips.append(clip(193,
    "The case for silver outperforming gold is structural. Silver is both a monetary metal and an industrial metal. The electrification of the global economy — electric vehicles, solar panels, data center cooling — requires silver in quantities that the existing supply chain cannot currently provide.",
    "Close-up of the internal circuitry of a solar panel in the manufacturing process, thin silver conductor lines visible across the silicon surface, a technician's gloved hands adjusting the alignment, clinical white light"
))

act5_clips.append(clip(257,
    "The gold-to-silver ratio — which measures how many ounces of silver it takes to buy one ounce of gold — had been running at over eighty to one for years. Historically, the ratio averages closer to fifty to one. A reversion to the historical mean, with gold at five thousand, implies silver at one hundred dollars. A reversion to its historical extremes implies silver far higher.",
    "Slow tracking shot moving along a museum display case holding both gold and silver artifacts side by side, the visual contrast of the two metals under glass, spotlights creating highlights on both surfaces"
))

act5_clips.append(clip(193,
    "YouTube commenters on the precious metals channels were experiencing the classic retail investor tension — wanting confirmation of their thesis, but nervous about short-term volatility. 'PM's are falling off a cliff again at noon EST,' one wrote. 'Silver heading below eighty and Gold below five thousand. Nothing is making sense. The manipulations are in control.'",
    "Cinematic close-up of a rollercoaster track from the perspective of a rider, the rails curving dramatically downward against a bright sky, the visual of vertiginous movement, the blur of speed"
))

act5_clips.append(clip(257,
    "Short-term price manipulation in precious metals markets is a legitimate and documented phenomenon. The COMEX futures market allows participants to sell claims to far more gold and silver than physically exists. When large players need to cover positions, prices can be driven down temporarily regardless of underlying fundamentals.",
    "Overhead shot of a massive commodity exchange trading floor, pits of traders gesturing to each other, the chaos of open-outcry trading, paper flying, the apparent disorder that is actually a highly structured market"
))

act5_clips.append(clip(193,
    "But as one commenter noted with simple clarity: 'Gold and silver aren't the threat. They just reveal the real threat, which is the devaluing fiat currency. The currency is going down no matter what. Don't worry about short-term fluctuations and manipulations. Fiat will go to zero. They always do.'",
    "Wide aerial shot of a ghost town in the American Southwest, abandoned buildings, dust streets, skeletal remains of a once-thriving settlement, the long arc of economic history written in desertion"
))

act5_clips.append(clip(257,
    "David Lin, whose Gold About to Double Again interview captured the prevailing sentiment among precious metals analysts, framed the financial crisis as inevitable rather than possible. The question is not whether a financial crisis occurs. The question is what form it takes and how long it lasts.",
    "Slow zoom into the face of a clock on a public building, the hands moving, the weight of time and inevitability, late afternoon light on the clock face, the city moving in the background"
))

act5_clips.append(clip(193,
    "Rob Bruggeman, speaking on David Lin's channel, articulated the endgame logic: inflation is ultimately a choice. 'The nice thing about inflation — you're paying off legacy debts with dollars that aren't worth as much.' This is the quiet default. The way governments have always resolved unrepayable debt.",
    "Cinematic wide shot of a central bank printing facility through a window, machines in operation, the endless production of currency, the worker visible only as a silhouette against the industrial light"
))

act5_clips.append(clip(257,
    "Now Bitcoin. At around seventy thousand dollars as of mid-March, Bitcoin had pulled back significantly from its all-time high of one hundred and twenty-six thousand — a peak reached in late 2025. The bears were calling it a bubble.",
    "Dramatic aerial shot of Las Vegas at night from altitude, the strip glowing in a grid of lights against the desert darkness, neon and LED and the human impulse to bet on the future written in light"
))

act5_clips.append(clip(193,
    "But Luke Gromen — macro analyst, founder of Forest for the Trees, and one of the most prescient voices on the intersection of fiscal policy and monetary policy — had a different view. He had described Bitcoin, in multiple interviews, as the last functioning smoke alarm for the global financial system.",
    "Extreme close-up of a smoke detector mounted on a white ceiling, a thin trail of smoke drifting toward it, the detector's LED indicator glowing steady red, the quiet vigilance of the warning system"
))

act5_clips.append(clip(257,
    "The smoke alarm metaphor is precise. A smoke alarm does not cause the fire. It detects it. When Bitcoin prices move in unusual ways, Gromen argued, it is signaling something about the health of the global financial system — specifically, the degree to which institutional actors believe the dollar-denominated system is under stress.",
    "Wide shot of a residential street at night, a house fire visible far down the block, fire engines arriving, neighbors watching from their porches, the emergency lights casting red pulses across the facades, the alarm already sounded"
))

act5_clips.append(clip(193,
    "At seventy thousand dollars, Bitcoin was down forty-four percent from peak. But Benjamin Cowen, presenting detailed on-chain analysis, argued that the four-year cycle — driven by the Bitcoin halving events — remained intact. The bear case required dismissing a decade of cycle data.",
    "Cinematic aerial shot over ocean waves in a long cyclical pattern, the regular swells approaching a rocky coastline in rhythmic succession, the predictable cycle of wave energy, each wave following the last"
))

act5_clips.append(clip(257,
    "The Bankless analysis was particularly sharp on the relationship between Bitcoin and the macro environment. Three things were driving markets in March 2026: oil, jobs, and private credit. All three affected Bitcoin not through their own logic, but through their effect on global liquidity.",
    "Wide aerial drone shot over a city financial district at golden hour, the towers of banking and finance casting long shadows eastward, the light itself seeming to flow between buildings like a liquid"
))

act5_clips.append(clip(193,
    "Michael Howell of CrossBorder Capital had identified global liquidity as peaking in fall 2025 and now turning downward. Liquidity cycle downturns are historically the biggest risk factor for speculative assets, Bitcoin included. But Howell also noted that liquidity downturns are never permanent.",
    "Slow motion shot of a tide pulling back from a rocky beach, exposing the rocks and pools beneath, the water receding, the familiar and cyclical nature of the movement, the implied return"
))

act5_clips.append(clip(257,
    "Arthur Hayes, in his characteristically blunt style on Wealthion, offered the ultimate bullish thesis for Bitcoin: 'The Fed will always print money.' If you believe that central banks will ultimately inflate their way out of debt burdens — as they have done every time in history — then hard assets with fixed or declining supply are the rational allocation.",
    "Cinematic wide shot of a river flowing endlessly through a landscape, the water constant and moving, a bridge spanning it, the river indifferent to any single moment in its continuous flow"
))

act5_clips.append(clip(193,
    "EllioTrades, analyzing the Iran Oil Crisis impact on Bitcoin specifically, noted that the volatility itself was the signal. When geopolitical shocks cause rapid price movements in Bitcoin, it demonstrates the asset's role as a global financial barometer.",
    "Extreme close-up of a barometer instrument, the needle oscillating slightly around a reading, the precision of the measurement, old polished brass and glass, the instrument of atmospheric pressure translated to financial pressure"
))

act5_clips.append(clip(257,
    "The ITM Trading community — deeply invested in gold and silver — was watching the precious metals with growing frustration at short-term price suppression, but the long-term thesis had only strengthened. One commenter articulated the position of a generation of retail precious metals holders: 'When fiat goes to zero, they take you to war.'",
    "Slow zoom into the surface of a gold bar, the reflective surface showing a warped reflection of the room, the purity marks visible in close-up, the weight and permanence of the metal implied"
))

act5_clips.append(clip(193,
    "Safe haven assets in March 2026 were not all performing the same way, and the divergences were informative. Jeremy Schwartz of Wisdom Tree, speaking to Wealthion, noted the paradox: traditional risk models were failing. 'We're in war and precious metals are underperforming. The dollar was supposed to be debasing, but yet it's rallying.'",
    "Wide shot of a radar screen in a control room, the sweep arm rotating, blips appearing at different distances from center, an operator studying the screen intently, the task of making sense of competing signals"
))

act5_clips.append(clip(257,
    "The dollar rally was itself a signal. In acute crisis, the dollar strengthens as global investors flee to the reserve currency — even if the long-term trajectory of the dollar is downward. This is the dollar smile theory in action: the currency that benefits from both good news and bad news, at least in the short term.",
    "Aerial drone shot over the New York Federal Reserve building in lower Manhattan, the stolid limestone exterior unchanged since 1924, the financial district streets around it teeming with movement, the gold in the vault below"
))

# ============================================================
# ACT VI: GEOPOLITICAL CHESS
# ============================================================
act6_clips = []

act6_clips.append(clip(257,
    "Act Six. The Geopolitical Chess. Who wins. Who loses. And what happens to the map of global energy.",
    "Sweeping aerial drone shot over a geopolitical landscape at dusk, mountains and plains visible, the abstract natural geography that underlies all political boundaries, the world as physical fact"
))

act6_clips.append(clip(257,
    "Charles Gave of Gavekal Research, speaking to Soar Financially, offered the most incisive geopolitical analysis of the oil shock. His central thesis: the Iran war was reshaping the US-China relationship in ways that could prove as consequential as the war itself.",
    "Wide shot of a large conference room with an empty oval table, flags of major nations behind the chairs, the visual of high-stakes diplomacy in the absence of the diplomats, ambient corporate lighting"
))

act6_clips.append(clip(193,
    "China receives a significant portion of its oil from the Gulf states through the Strait of Hormuz. But Gave's crucial insight: China is less vulnerable than it appears. Pipeline access from Russia and Central Asia provides an alternative supply route that bypasses the strait entirely.",
    "Aerial drone shot over a massive oil pipeline corridor crossing a Central Asian steppe, the silver pipeline a straight line to the horizon in both directions, the immense scale of energy infrastructure"
))

act6_clips.append(clip(257,
    "This asymmetry matters enormously. If the United States has closed the Hormuz to punish Iran, and in doing so has disrupted the energy supply to Europe and Japan and Southeast Asia — but less so China — then the unintended consequence of the war is a relative strategic advantage for Beijing.",
    "Cinematic wide shot of a massive container port with enormous cranes loading ships, goods flowing in and out, the physical infrastructure of global trade, the port as economic artery"
))

act6_clips.append(clip(193,
    "Gave described this as the oil shock reshaping the US-China relationship. China, as a net oil importer that is somewhat insulated from Hormuz disruption, watches the war with a different calculus than Europe, which is far more exposed.",
    "Wide aerial shot of a sprawling industrial city in China at dusk, factory chimneys and power plants visible, the urban-industrial scale of Chinese manufacturing, yellow haze catching the last light"
))

act6_clips.append(clip(257,
    "And then there is Russia. The Coin Bureau's analysis was direct: 'The Only Winner in the Iran War is Unexpectedly Russia.' At one hundred and fifteen dollar oil, Russia's war-era budget math transforms completely. Every barrel sold above eighty dollars is essentially free money for Moscow.",
    "Cinematic aerial drone shot slowly moving over a Siberian oil field in winter, the machinery and wells visible in snow-covered landscape, pipes and infrastructure stark against the white, a grey overcast sky above"
))

act6_clips.append(clip(193,
    "Joe Blogs documented the extraordinary irony: the United States had been forced to loosen its own sanctions on Russian oil to prevent the global energy market from completely seizing up. In fighting Iran, America had enriched Russia.",
    "Wide shot of a pipeline terminal where multiple lines converge into a manifold, valves and meters, steam in cold air, the complex interdependency of global energy infrastructure made visible in metal"
))

act6_clips.append(clip(257,
    "This geopolitical reversal had an additional dimension in the European context. Europe had spent three years since the Ukraine war urgently diversifying away from Russian energy. LNG from Qatar and the United States had been the cornerstone of that diversification. Now, QatarEnergy had declared force majeure. American LNG production was maxed out. And Europe was staring at an energy security crisis.",
    "Wide aerial drone shot over a European industrial port in winter, LNG tankers visible at dock, steam from regasification equipment, the critical infrastructure of European energy import dependency"
))

act6_clips.append(clip(193,
    "The European energy crisis was not merely economic. It was political. Governments that had made promises about energy security were now unable to keep them. The political consequences — in a continent already under strain from immigration pressures, far-right movements, and economic divergence — could reshape European politics.",
    "Dramatic wide shot of a European parliament chamber, the semicircular rows of seats visible, flags of member states arrayed behind the podium, an empty podium suggesting the absence of easy answers"
))

act6_clips.append(clip(257,
    "Potential deindustrialization of Germany — already in recession and reliant on energy-intensive manufacturing — had moved from theoretical risk to plausible scenario. The Mittelstand, the small and medium industrial companies that form the backbone of German manufacturing, was already struggling before the war. Now it faced another energy cost surge.",
    "Cinematic wide shot of a German automotive factory floor, advanced robotic arms moving along an assembly line, human workers visible at quality control stations, the precision engineering of industrial excellence"
))

act6_clips.append(clip(193,
    "The geopolitical chess extended to food security. The Hormuz closure had disrupted not just oil and LNG, but the petrochemical feedstocks for fertilizer. The Middle East also accounts for a significant share of global phosphate and potassium exports. Agricultural nations from Pakistan to Egypt to Bangladesh faced both higher energy costs and fertilizer shortages simultaneously.",
    "Wide aerial drone shot over agricultural fields in a developing nation, small-scale farming visible, irrigation channels, the essential vulnerability of food production systems that depend on global supply chains"
))

act6_clips.append(clip(257,
    "Now, copper. In the midst of all these crises, Commodity Culture's Jesse Day reported on March eleventh that copper was 'next up to shock the market.' The copper deficit, he said, was serious — driven not by war, but by structural demand that no one could easily turn off.",
    "Cinematic aerial shot over a massive open-pit copper mine, the terraced red-brown walls spiraling down hundreds of meters, heavy mining equipment visible on each bench, the scale of extraction staggering"
))

act6_clips.append(clip(193,
    "The copper demand supercycle is driven by three forces that are, individually, enormous. And together, they represent a demand story that the current copper supply chain cannot satisfy. First: AI. The buildout of artificial intelligence data centers requires enormous quantities of copper — for power distribution, cooling systems, and the electrical infrastructure of computing at scale.",
    "Wide shot of a massive data center interior under construction, rows of server rack frames being installed, extensive copper wiring being laid, the physical infrastructure of digital intelligence"
))

act6_clips.append(clip(257,
    "Second: electrification. Every electric vehicle contains approximately four times as much copper as a comparable internal combustion vehicle. The global transition to electric mobility — mandated by regulation in Europe, driven by economics in China, accelerating in North America — represents a step change in copper demand that the mining industry cannot satisfy on a five-year planning horizon.",
    "Close-up of a copper wire harness in an electric vehicle during assembly, the bundled cables being routed through the vehicle frame, the visual density of copper in the new economy"
))

act6_clips.append(clip(193,
    "Third: grid upgrade. The electrical grid infrastructure of every developed nation was built in the mid-twentieth century and is approaching end of life simultaneously with the requirement to carry dramatically more electricity — for EV charging, heat pump heating, and industrial electrification.",
    "Aerial drone shot over high-voltage transmission towers stretching across a landscape, the lines converging at a substation, the ancient and modern grid infrastructure coexisting, clouds behind the towers"
))

act6_clips.append(clip(257,
    "Jeremy Schwartz of Wisdom Tree, speaking to Wealthion, connected the copper thesis directly to the AI energy super cycle: 'The energy demand from AI data centers alone could require grid investments measured in the trillions of dollars over the next decade.' Copper is the essential material in every meter of that investment.",
    "Wide shot of a solar farm under construction in a desert landscape, solar panels being installed in long rows by workers in hard hats, the red desert stretching to mountains behind, the future energy economy taking shape"
))

act6_clips.append(clip(193,
    "The irony — and the geopolitical complexity — of the copper story is that the largest copper reserves in the world are concentrated in the Andes of South America and the Congo. The nations that control those reserves are increasingly not aligned with US strategic interests. Another commodity, another supply chain vulnerability.",
    "Cinematic aerial shot of the Andes mountains, snow-capped peaks above cloud level, the landscape vast and ancient, somewhere beneath the rock the copper reserves that the future depends upon"
))

act6_clips.append(clip(257,
    "The US-China dynamic on copper added another dimension. China is the world's largest copper consumer and has been systematically acquiring long-term supply contracts and ownership stakes in copper mines globally. The strategic competition for copper was already underway before March 2026. The Iran war and its economic consequences only accelerated it.",
    "Cinematic slow drone shot over a major shipping port, mountains of copper cathodes stacked in the open air, the distinctive red-orange metal visible from altitude, cranes loading ships bound for China"
))

act6_clips.append(clip(193,
    "The broader commodity supercycle thesis — that we are entering a decade or more of elevated commodity prices driven by underinvestment in supply during the 2010s and surging structural demand in the 2020s — was being vindicated in real time by the events of March 2026.",
    "Aerial drone shot over a mining operation in a remote landscape, the scale of earth movement visible, trucks as small as ants on the roads between excavation levels, the industrial muscle applied to resource extraction"
))

act6_clips.append(clip(257,
    "The global liquidity cycle, as Michael Howell of CrossBorder Capital had noted, had peaked in fall 2025. But global liquidity cycles do not peak and then stay depressed forever. They turn. And when the next liquidity cycle begins — when central banks ultimately respond to the slowdown with accommodation — the hard assets will have been waiting.",
    "Slow motion shot of a wave cresting at the shoreline, the peak of the wave perfectly captured at maximum height before it begins to fall forward, the moment of pause at the apex of the cycle"
))

act6_clips.append(clip(193,
    "This was the sophisticated investor's thesis for March 2026: not that the world was ending, but that the rules of the old world — predictable supply chains, stable energy prices, freely functioning credit markets, a Fed with room to maneuver — had ended. And the new rules required a different portfolio.",
    "Wide shot of a crossroads in a remote landscape, two roads stretching in different directions to different horizons, the choice point, late afternoon light throwing long shadows from the road markers"
))

act6_clips.append(clip(257,
    "The Wealthion session with Brett Rentmeester on 'War, Oil, and the Debt Spiral: How to Invest Through the Chaos' captured the emerging consensus: hard assets matter when geopolitics and markets turn chaotic. Not as speculation, but as insurance — the recognition that the range of bad outcomes had permanently widened.",
    "Wide aerial drone shot over a landscape showing both cultivated farmland and wild terrain in the same frame, order and chaos side by side, the boundary between them the relevant question"
))

# ============================================================
# ACT VII: CLOSING — SYNTHESIS AND WHAT LIES AHEAD
# ============================================================
act7_clips = []

act7_clips.append(clip(257,
    "Act Seven. The Closing. What this means. Where we go from here. And whether the worst is still ahead.",
    "Extreme wide aerial drone shot at dusk over a great city, the lights beginning to come on as the sun descends, the city stretching to every horizon, the scale of human civilization and its financial dependencies"
))

act7_clips.append(clip(257,
    "Let us return to where we began. March first, 2026. The dawn strikes on Tehran. In the days that followed, four compound crises became visible simultaneously — crises that had been developing for years but were crystallized by the war into a single, coherent, terrifying picture.",
    "Slow cinematic push into a globe on a desk, the lamp light illuminating the Middle East region, the camera moving slowly toward the Persian Gulf area, political lines invisible on the physical terrain"
))

act7_clips.append(clip(193,
    "Crisis one: the energy shock. The Strait of Hormuz — the twenty-one-mile chokepoint through which twenty percent of global oil and twenty percent of global LNG flows every day — had been effectively closed. Brent crude hit one hundred and fifteen dollars. LNG prices surged one hundred and thirty-seven percent in five days.",
    "Cinematic overhead drone shot slowly moving over the Strait of Hormuz, the narrow passage of water between two landmasses, the extraordinary geopolitical weight of ordinary ocean geography"
))

act7_clips.append(clip(257,
    "Crisis two: the private credit implosion. Three hundred billion dollars in assets frozen or restricted across BlackRock, Blackstone, Morgan Stanley, Cliffwater, Blue Owl. British firms collapsing. Contagion spreading. The shadow banking system — built in the decade of zero interest rates — discovering that it had no mechanism to handle large-scale simultaneous redemptions.",
    "Slow dolly shot down a long hallway of a financial institution after hours, offices dark, a cleaning crew visible at the far end, the institutional machinery idle for the night, the human scale of financial power"
))

act7_clips.append(clip(193,
    "Crisis three: the Federal Reserve's bind. Stagflation — the simultaneous presence of inflation and recession — was the one scenario for which the standard monetary policy toolkit has no good answer. Cut rates, you fuel inflation. Raise rates, you deepen recession. Hold steady, you do neither.",
    "Close-up of a doctor's hands holding a stethoscope to a patient's chest, the listening posture of diagnosis, the careful attention to signals, the weight of a difficult assessment, warm clinical light"
))

act7_clips.append(clip(257,
    "Crisis four: the K-shaped fracture. The bottom ninety percent of the American economy — already experiencing recession in their lived reality — were about to face higher gasoline prices, higher heating costs, higher food prices, and tighter credit conditions simultaneously. The wealth effect that had been supporting top-tier consumption was already showing signs of fatigue.",
    "Aerial drone shot over an American highway at rush hour, lanes of traffic moving slowly, the everyday reality of commuting, the fuel cost of the daily grind, the working economy in motion"
))

act7_clips.append(clip(193,
    "Connecting these four crises was a common thread: the debt. The United States federal government owed forty trillion dollars. American households owed a combined twenty-six trillion. American corporations owed another fifteen trillion. Every dollar of that debt had been issued under the assumption of some combination of growth, stability, and affordable energy.",
    "Slow zoom into a digital clock display — not showing numbers, but showing a counting mechanism that suggests accumulation, the visual metaphor of debt as time, the weight of compounding obligation"
))

act7_clips.append(clip(257,
    "None of those assumptions held as of March thirteenth, 2026. And when assumptions fail at this scale, the reckoning does not arrive all at once. It arrives in stages, over months and years — each crisis revealing the next vulnerability, each patch revealing the next leak.",
    "Cinematic wide shot of an old stone wall being carefully inspected by a mason, his hands probing the mortar between stones, finding a crack, pressing a finger in, discovering the depth of deterioration"
))

act7_clips.append(clip(193,
    "What does this mean for investors? Every analyst we have referenced in this documentary converges on a single broad principle: diversification away from paper assets and toward real assets is no longer a fringe view. It is the emerging consensus of institutional finance.",
    "Wide shot of a museum vault or storage facility where different types of assets are stored — paintings, sculptures, gold bars, physical goods — the visual of real value in physical form"
))

act7_clips.append(clip(257,
    "Gold has a five-thousand-year track record as a store of value. In every monetary crisis in history — every debasement, every hyperinflation, every default — gold has preserved purchasing power over long time horizons. The Incrementum forecast of five thousand two hundred dollars per ounce is not a wild speculation. It is a historically grounded estimate of where gold goes when fiat credibility erodes.",
    "Cinematic extreme close-up of ancient gold artifacts in a museum display, coins and jewelry from ancient civilizations, the metal unchanged after millennia, the same material that ancient merchants traded"
))

act7_clips.append(clip(193,
    "Silver, at ninety dollars with a structural industrial demand story that the energy transition only strengthens, has a multi-decade runway that gold does not. The argument for silver overperforming gold over the next decade is compelling: it is both monetary and industrial, currently under-owned, and facing a supply deficit.",
    "Aerial drone shot over a solar farm in operation at sunset, the panels angled toward the diminishing light, the silver conductors invisible but present in every panel, the industrial demand for the metal embodied in landscape"
))

act7_clips.append(clip(257,
    "Bitcoin remains the most contested asset in this analysis. It has the most compelling long-term thesis — fixed supply, global liquidity, institutional adoption now underway — and the most uncertain short-term path. Arthur Hayes is right that the Fed will ultimately print money. The question is timing. Bitcoin in a liquidity contraction is not the same as Bitcoin in a liquidity expansion.",
    "Dramatic aerial shot over a fork in a river, the water splitting around a large island, the two channels moving in parallel for a while before one curves away, the visual of divergent paths from a single origin"
))

act7_clips.append(clip(193,
    "Copper — less discussed, less glamorous than gold or Bitcoin, but arguably the most consequential commodity story of the decade — is the one asset that both the green energy transition and the AI revolution require in quantities that current supply cannot provide. The price discovery process for copper has only begun.",
    "Close-up of copper pipes being fitted in an industrial installation, the warm orange-red metal gleaming under work lights, a plumber's hands applying sealant at a joint, the fundamental infrastructure of everything"
))

act7_clips.append(clip(257,
    "The geopolitical realignment being accelerated by the Iran war will take years to fully manifest. But the direction is clear: a world that was globalizing is now fragmenting into competing blocs. Energy security is being repriced. Supply chain resilience is being repriced. And the dollar's role as the sole reserve currency is being questioned more seriously than at any time since Bretton Woods.",
    "Sweeping aerial drone shot over a major international port at night, containers stacked in enormous grids, cranes lit up, ships arriving and departing, the global trading system that looks permanent but has always been contingent"
))

act7_clips.append(clip(193,
    "Ben Rhodes' phrase — the Great Lie of War — was not about any single decision. It was about the institutional tendency to oversell the certainty of military outcomes while underselling the certainty of economic consequences. Wars are easy to start and nearly impossible to stop on schedule.",
    "Wide shot of an empty war memorial at dusk, stone walls bearing names, the late light catching the carved letters, a single flower placed at the base of the wall, the human cost made intimate by the individual names"
))

act7_clips.append(clip(257,
    "The Iran war, now in its second week as of March thirteenth, had already produced economic consequences that would take years to fully absorb. The Hormuz closure, even if lifted tomorrow, had demonstrated the vulnerability. That vulnerability would now be permanently repriced into insurance rates, routing decisions, investment in alternative infrastructure, and the national security budgets of a dozen nations.",
    "Aerial drone shot over a reconstruction site where infrastructure is being rebuilt, construction equipment active, new structures rising, the process of adaptation and repair underway"
))

act7_clips.append(clip(193,
    "Azul, the financial advisor with over twenty years of experience whose videos documented the classic warning signs of market downturns, noted that what always precedes a crash is not a single alarming event. It is the accumulation of small risks that have been individually explained away — each one manageable, but together, catastrophic.",
    "Cinematic wide shot of stormclouds building over a landscape, individual clouds merging into a larger formation, the sky darkening from the west, the incremental arrival of a larger weather system"
))

act7_clips.append(clip(257,
    "As of March thirteenth, those small risks had ceased to be small. The war was real. The oil shock was real. The private credit freeze was real. The Fed's bind was real. The K-shaped fracture in the economy was real. The question was no longer whether these things were happening. The question was how they would interact over the months ahead.",
    "Slow wide aerial drone shot over an ocean surface at sunset, the water moving in deep swells, the wind visible in the wave patterns, the vast system of forces operating beneath the surface, the depth and power of the sea"
))

act7_clips.append(clip(193,
    "Every economist we have cited, from Martin Wolf at the Financial Times to Joseph Stiglitz to Charles Gave to Ronald Stoeferle, agrees on one point: the uncertainty range has permanently widened. The probability-weighted outcomes of the global economy in 2026 and 2027 are now far more dispersed than they were in February.",
    "Wide shot of a weather forecasting center, meteorologists studying multiple screens showing different model outputs, the visual of expert uncertainty — not ignorance, but the honest acknowledgment of a wide probability range"
))

act7_clips.append(clip(257,
    "For retail investors — the YouTube community that Thoughtful Money, ITM Trading, David Lin, and Soar Financially serve — the message from every credible voice was consistent: this is not a moment for heroic single bets. It is a moment for diversification, humility, and preparation for outcomes that standard models do not include.",
    "Cinematic wide shot of a family around a dining room table in the evening, books and papers spread out, a conversation happening that is clearly important, the domestic scale of financial decision-making, warm lamp light"
))

act7_clips.append(clip(193,
    "The retail investor comment that has stayed with us through this analysis came from the ITM Trading live stream on Day Six of the Iran war. 'When fiat goes to zero, they take you to war.' Whether that comment is precisely accurate or not, it captures a sentiment — a bone-deep mistrust of institutional finance, of political promises, of official narratives — that millions of people share.",
    "Close-up of hands opening a leather-bound journal to a page with handwritten notes, the personal record of someone trying to make sense of the world, the pen resting across the open pages"
))

act7_clips.append(clip(257,
    "That sentiment has driven the precious metals community for decades. It drove the Bitcoin community from its inception. It is increasingly driving the mainstream investor who looks at forty trillion dollars in federal debt, a private credit market with no pricing transparency, a Federal Reserve between a rock and a hard place, and decides: I need to own something real.",
    "Wide aerial drone shot over a city at night transitioning to reveal farmland in the dawn light, the city on one side of the frame, fields on the other, the two worlds of paper and real economy side by side"
))

act7_clips.append(clip(257,
    "The global economic crisis of March 2026 will be studied in business schools for generations. Not as a case study in inevitable catastrophe, but as a case study in compound risk — in how individual vulnerabilities that seem manageable in isolation can become catastrophic when they arrive together.",
    "Slow pan through a university library, tall shelves of books receding in both directions, a student at a table studying, the institution of accumulated knowledge, the long project of learning from history"
))

act7_clips.append(clip(193,
    "The oil shock and the private credit crisis and the Federal Reserve's bind and the K-shaped economy are not four separate stories. They are four faces of a single underlying reality: an economic system that has been running on borrowed time, borrowed money, and borrowed assumptions about the stability of the world.",
    "Dramatic aerial wide shot of a suspension bridge at dusk, the cables tensioned and holding, traffic crossing, the engineered elegance of a structure bearing enormous loads through distributed tension"
))

act7_clips.append(clip(257,
    "The question that every investor, every government, every central bank faces as they watch March 2026 unfold is the same question that every generation faces when the world changes faster than the models: What do you do when the map is no longer the territory?",
    "Close-up of an old paper map laid flat on a table, a compass rose in one corner, the lines and symbols of how someone once understood the world, the edges worn from use, the territories named in a language of certainty that no longer applies"
))

act7_clips.append(clip(193,
    "Martin Wolf's final counsel, drawn from a lifetime of studying economic crises, was not pessimism. It was realism. The world economy has absorbed massive shocks before — the 1970s, 2008, COVID. It will absorb this one too. But the absorption takes time, causes pain, and permanently changes the landscape that emerges on the other side.",
    "Aerial drone shot over a forest recovering from a wildfire, new green growth visible emerging from the ash-grey landscape, the resilience of regeneration, the evidence that destruction and renewal are the same process"
))

act7_clips.append(clip(257,
    "The investors who will navigate this period best are not the ones who predicted it perfectly. They are the ones who have built portfolios resilient enough to survive a wide range of outcomes — who own real assets alongside financial ones, who are not leveraged to a single outcome, who have accepted that uncertainty is not a temporary condition but the permanent state of a complex world.",
    "Slow wide aerial shot of a mountaineer on a ridge line at dawn, the sun rising behind distant peaks, the climber steady and balanced, the vast landscape stretching in every direction, the hard-won vantage of difficult ascent"
))

act7_clips.append(clip(193,
    "The commenters who wrote, on every financial channel from ITM Trading to Bankless: 'We've been in recession since 2022.' 'The 1929 moment is here.' 'Oil to two hundred dollars, mark my words.' They are not economists. They may not be right in every detail. But they are sensing something real — the accumulated stress of a system running at its limits.",
    "Wide shot of ordinary people on a city street going about their lives — commuters, shoppers, a street vendor, a construction worker — the everyday economy captured in its human texture, people sensing but not yet naming the change"
))

act7_clips.append(clip(257,
    "March 2026 may not be remembered as the month the global economy broke. It may be remembered as the month the global economy began the long process of honest reckoning with the debts — financial, energy, geopolitical — that had been deferred for decades.",
    "Wide aerial drone shot slowly rising over a major city until the individual streets and buildings give way to the full geometry of the metropolis, the city as system, vast and complex and in motion"
))

act7_clips.append(clip(193,
    "The war in Iran will end — as all wars end. The Strait of Hormuz will reopen — or the world will build infrastructure to route around it. The private credit crisis will resolve — painfully, with winners and losers and some degree of bailout. The Federal Reserve will find its path — probably through inflation.",
    "Cinematic slow zoom out from a single candle flame to reveal a room full of candles, each light separate but the collective illumination transforming the space, the visual of distributed resilience"
))

act7_clips.append(clip(257,
    "But the landscape on the other side will look different. Energy security will be a central concern of every national budget. The private credit market will operate under far more scrutiny. The Federal Reserve's independence — already under political pressure — will be tested further. And the commodities that the world needs to build the future — copper, silver, the rare earths of the green transition — will be priced accordingly.",
    "Sweeping aerial drone shot at dawn over an industrial-natural landscape, a river winding through a valley, a city on one bank, farmland on the other, the sun rising and casting new light on everything, the suggestion of a new day"
))

act7_clips.append(clip(193,
    "We have reported on this crisis in real time, through the voices of analysts and economists and ordinary investors who are living through it, watching it, trying to understand it. The data is incomplete. The outcome is uncertain. The range of possibilities is wide.",
    "Close-up of a journalist's notepad with handwritten notes, a pen resting across the open page, words visible but indistinct, the recording of events in progress, the act of documentation as witness"
))

act7_clips.append(clip(257,
    "What is not uncertain is this: the events of March 2026 have changed the assumptions under which global finance operates. The era of cheap energy, easy credit, and stable geopolitics that defined the post-Cold War world — that era ended not with a single dramatic moment, but with a war in the Gulf, a private credit freeze, and a Federal Reserve with no good options.",
    "Final dramatic slow aerial pull-back from a burning flare stack on an oil platform at night, the flame brilliant against the dark ocean and sky, the platform small and the darkness enormous, the energy and its cost made visible in the single flame"
))

act7_clips.append(clip(257,
    "The world is on fire. Literally, in the Persian Gulf. And figuratively, in the offices of private credit managers, in the corridors of the Federal Reserve, in the kitchens of families watching their energy bills climb, and in the data centers where algorithms are pricing in risks that standard models don't even have a category for.",
    "Wide aerial drone shot over the Persian Gulf at dusk, the water turning from blue to gold to orange as the sun descends, oil platforms visible on the horizon, the geography of the crisis, the beauty and the danger"
))

act7_clips.append(clip(193,
    "What happens next depends on choices that have not yet been made. By the Iranian successor government. By the Federal Reserve. By BlackRock and Morgan Stanley and Cliffwater. By the governments of Europe. By central banks everywhere. And by millions of individual investors deciding, right now, how much of this they want to absorb.",
    "Wide cinematic shot of a road stretching straight to a distant horizon under a dramatic sky with storm clouds and breaks of sunlight simultaneously, the visual of an uncertain path forward, multiple outcomes possible"
))

act7_clips.append(clip(257,
    "We will be watching. And we will report what we find. Because in financial crises — as in all crises — the most dangerous thing is not the risk you can see. It is the risk you have convinced yourself doesn't exist.",
    "Final wide aerial drone shot pulling slowly back from a city at night, the lights glowing in grid patterns, the human civilization lit up against the darkness, the planet rotating in its orbit indifferent to the financial crises below, a long slow fade"
))

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
    len(clip['narration'].split())
    for seg in segments
    for clip in seg['clips']
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
    duration = frames / 24
    print(f"  {frames} frames ({duration:.1f}s at 24fps): {count} clips")

print(f"\nFile saved: /home/user/workspace/v5_script.json")
