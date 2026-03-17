#!/usr/bin/env python3
"""
Add supplemental clips to each act to reach 330+ clips / 13,500 words target
Loads existing script and appends additional clips to each segment
"""
import json

with open('/home/user/workspace/v5_script.json', 'r') as f:
    script = json.load(f)

# Find current max clip number
segments = script['segments']
all_existing = [c for seg in segments for c in seg['clips']]
max_clip = len(all_existing)
print(f"Current clips: {max_clip}")

counter = [max_clip]

def clip(frames, narration, prompt):
    counter[0] += 1
    return {
        "id": f"clip{counter[0]:03d}",
        "frames": frames,
        "narration": narration.strip(),
        "prompt": prompt.strip()
    }

# ============================================================
# ACT I ADDITIONS (~15 clips)
# ============================================================
act1_add = []

act1_add.append(clip(257,
    "The war had been building for months, if not years. The United States and Israel had been conducting an extensive intelligence operation focused on Iran's nuclear program and its IRGC command structure. The strikes on March first were the culmination of that preparation, but the trigger was a specific intelligence report about imminent Iranian nuclear capability that the two governments deemed unacceptable.",
    "Cinematic aerial drone shot over the Mediterranean Sea at night, a fleet of ships visible as pinpoints of light on the dark water, the logistics of a military operation captured from altitude, the scale of the deployment invisible from any single point"))

act1_add.append(clip(193,
    "Ed Yardeni's 'Between Iran and a Hard Place' session noted the domestic political dimension with characteristic dryness. The administration had repeatedly promised a short, decisive engagement. Markets were beginning to price in a scenario where short and decisive was not the actual outcome.",
    "Wide shot of a television news studio in operation, multiple screens displaying different feeds, anchors at the desk, the live processing of breaking events, the media infrastructure of crisis coverage"))

act1_add.append(clip(257,
    "The first economic impact was immediate and personal for hundreds of millions of Americans: the price of gasoline. Retail gasoline prices — which had already been elevated — jumped thirty cents per gallon in the first week of the war. At thirty cents per gallon, the average American household filling up twice a week was paying an additional twenty-four dollars a month. Annually, that was nearly three hundred dollars.",
    "Slow motion close-up of a gasoline price sign outside a service station, the changeable numbers showing a higher price than the day before, the mechanism of adjustment visible in the sign display, the direct transmission from Persian Gulf to American pocket"))

act1_add.append(clip(193,
    "That three-hundred-dollar annual figure sounds modest in isolation. But it arrived on top of three years of above-target inflation that had already reduced real wages for the median American household. The marginal dollar was already stretched. The oil shock was the straw added to an already burdened load.",
    "Close-up of a household budget spreadsheet open on a kitchen table, numbers in ink, a calculator beside it, the domestic arithmetic of stretched finances, a coffee ring on the corner of the paper, morning light on the table"))

act1_add.append(clip(257,
    "The concept of energy poverty — households spending more than ten percent of their income on energy costs — was already at elevated levels in the United States before the war. The oil shock threatened to push a further cohort of working-class households into energy poverty, forcing the impossible choices that the economic statistics only partially capture.",
    "Wide shot of a utility company office with a customer service queue, people waiting with bills in hand, the human interface of the energy cost crisis, ordinary faces navigating an extraordinary situation"))

act1_add.append(clip(193,
    "The Mark Thompson Show documented the domestic political backlash beginning to form. 'This is not about America First. It is not about the welfare of our country.' The political framing of the war's economic consequences was hardening in real time, as the gap between official optimism and market pricing widened.",
    "Wide aerial shot of a main street in a mid-size American city, storefronts and vehicles and people, the quotidian reality of an economy watching a war unfold on its phones while going about daily business, the detachment and engagement simultaneously"))

act1_add.append(clip(257,
    "The markets were simultaneously processing two separate but related probabilities: the probability that the military campaign achieved its stated objectives on the promised timeline, and the probability that the economic disruption caused by the Hormuz closure would resolve before causing structural damage. Both probabilities, as of March thirteenth, were well below fifty percent.",
    "Wide aerial drone shot over a financial district at golden hour, the towers casting shadows, the market participants visible below as purposeful dots, the aggregate of individual probability assessments creating the price"))

act1_add.append(clip(193,
    "Joe Blogs documented the cascading second-order effects that the financial press was underreporting: rising shipping insurance premiums, cancelled voyages, deferred maintenance at refineries whose managers were uncertain about the revenue environment, and the chilling effect on new energy investment as project economics became impossible to calculate.",
    "Cinematic wide shot of a shipyard with vessels in various stages of construction, some actively worked on, others seemingly paused, the uncertainty of the energy environment translating into investment hesitation"))

act1_add.append(clip(257,
    "Soar Financially's Milton Berg offered a note of analytical caution that the bears needed to hear: 'Don't Fear the Headlines Yet.' Berg's proprietary analysis suggested that the market's initial reaction had potentially overpriced the immediate disruption. The situation was serious. But serious situations resolve. Investors who panicked at the headline had historically underperformed those who held their framework.",
    "Wide shot of a natural harbor in early morning calm, fishing boats tied at their moorings, the water absolutely still, the absence of storm suggesting the perspective that the storm is real but not all-consuming"))

act1_add.append(clip(193,
    "Berg's caution was disciplined and historically grounded. But it sat in tension with the structural analysis from other quarters. The question was not whether markets had overreacted in the short term — they often do. The question was whether the structural damage from the Hormuz closure was as temporary as the price action suggested. The answer, increasingly, was no.",
    "Close-up of a geologist's rock sample under examination, a hand turning the core sample in the light, the mineral layers visible in section, the reading of structural evidence that does not change based on observer sentiment"))

act1_add.append(clip(257,
    "The Bankless Weekly Rollup of the second week of March framed the situation for a crypto-native audience that was accustomed to volatility but less accustomed to war: 'We moved from the stable era to the chaotic era. Wars, AI, overall market jitters. There's three things we're paying attention to: oil, jobs, private credit.' The synthesis was correct and complete.",
    "Aerial drone shot over a modern city at dusk, the transition from day to night visible across the skyline, some districts in full light, others beginning to darken, the uneven transition from stability to chaos rendered in the urban geography of light"))

act1_add.append(clip(193,
    "The Wealthion interview with Brett Rentmeester captured what the war meant for portfolio construction: when the stable era ends, the portfolio that was built for stability becomes a liability. The portfolio that was built for resilience — that anticipated a range of bad outcomes — becomes the asset. March 2026 was the test.",
    "Wide shot of a testing laboratory, equipment being examined under stress conditions, instruments measuring the response to applied forces, the scientific method applied to portfolio resilience through systematic analysis"))

act1_add.append(clip(257,
    "Within the first week of the war, the vocabulary of the global financial conversation had changed. Terms that had been academic — warflation, Hormuz premium, private credit freeze — became mainstream financial media usage. The analysts who had been warning about these scenarios for years found their audiences multiplying. The crisis had its own accelerant in the attention economy.",
    "Cinematic wide shot of a broadcast studio with multiple news channels visible on monitors, the simultaneous processing of crisis across different media platforms, the multiplication of attention around a single set of events"))

act1_add.append(clip(193,
    "The Meb Faber Show offered a long-term perspective that the daily news cycle inevitably compressed: US equity valuations — already at elevated Shiller CAPE ratios approaching the levels last seen in 1999 — had been the single biggest unpriced risk in the portfolio of the average American investor. The war had not created that vulnerability. It had merely created the conditions under which it would be tested.",
    "Aerial drone shot over a classic suspension bridge at dusk, the structure holding an enormous weight while appearing graceful, the engineering of load-bearing elegance, the test of the structure only visible under maximum load"))

act1_add.append(clip(257,
    "The data from Azul's demographic analysis of the Buffett Indicator — market capitalization to GDP — and the Shiller CAPE ratio created a sobering backdrop. Both measures were at or near their historical extremes. Both suggested that the equity market had been pricing in a perfection that the real world had already decided not to provide.",
    "Cinematic slow push into the reflection of an overcast sky in a glass building facade, the clouds moving slowly in the reflection, the building's glass surface capturing and distorting the world outside, the market as mirror and as distortion"))

# ============================================================
# ACT II ADDITIONS (~15 clips)
# ============================================================
act2_add = []

act2_add.append(clip(257,
    "The tanker insurance dimension of the oil shock received insufficient analytical attention despite being critically important. Lloyds of London and the broader marine insurance market had repriced war risk premiums for Gulf voyages to levels that made many standard voyages uneconomic. The physical availability of oil mattered less than the cost of getting it to where it was needed.",
    "Close-up of the underwriting floor of a major insurance exchange, brokers discussing terms across desks, the physical market for risk pricing in operation, the invisible financial infrastructure of global shipping"))

act2_add.append(clip(193,
    "The sovereign wealth fund dynamics added another layer to the oil price story. Middle Eastern sovereign wealth funds — which held trillions of dollars in American and European financial assets — were operating in a novel environment. Their governments needed the oil revenue. But some of their investment committees were also pulling back from Western assets in response to the war.",
    "Wide aerial shot of a gleaming financial district in the Middle East, the towers of wealth management and sovereign investment, the capital flows that connected the Gulf to global markets, architecture as geopolitical relationship"))

act2_add.append(clip(257,
    "The Saudi dimension was carefully watched but underreported. Saudi Arabia, which had spent years managing its relationship with both the United States and the broader Arab world, was navigating the Iran war with extreme caution. Saudi oil production decisions — and Saudi decisions about where to price its oil and in which currency — carried enormous geopolitical weight.",
    "Aerial drone shot over the Saudi Aramco facilities in Abqaiq, the vast processing infrastructure of the world's largest oil producer, steam and smoke visible, the physical scale of production that the global economy depended upon"))

act2_add.append(clip(193,
    "The petrodollar system — the arrangement by which Gulf oil has been priced and settled in US dollars since the 1970s — was under discussion in ways that it had not been for decades. The Iran war had forced several Gulf states to consider their exposure to dollar-denominated systems controlled by a country conducting military operations in their neighborhood.",
    "Cinematic close-up of various currency notes on a table, the dollar prominent among them alongside other major currencies, the visual of the dollar's dominance and the questioning of that dominance in a changing world"))

act2_add.append(clip(257,
    "The shipping rerouting problem was not merely a cost problem. It was a time problem. European natural gas storage — which had been drawn down through the winter — needed to be replenished through spring and summer LNG imports. Every week of Hormuz closure was a week of replenishment that was not happening. The risk of European natural gas shortages by winter 2026-27 was already being priced into energy futures markets.",
    "Aerial drone shot over a natural gas storage facility in Europe, the large underground storage field visible in the surface infrastructure, the critical buffer of stored energy that protected against supply disruption, now being drawn down"))

act2_add.append(clip(193,
    "The United States' domestic energy production advantage was genuine but partial. American shale oil was light and sweet — ideal for producing gasoline. But the US was a net importer of medium and heavy crude grades used for diesel production. The Hormuz disruption was creating a specific diesel supply stress that the American production advantage could not fully offset.",
    "Wide shot of a US refinery complex at dawn, the processing towers lit by the rising sun, the American energy industry at full operation, the domestic production that provided relative insulation from the external shock"))

act2_add.append(clip(257,
    "The agricultural commodity cascade from the Hormuz closure was beginning to show up in futures markets by the end of the first week of March. Wheat futures — already elevated from years of climate disruption and the Ukraine conflict — gained another eight percent. Corn futures followed. The oil shock was transmitting into food commodity inflation through the fertilizer channel exactly as the economic models predicted.",
    "Wide aerial shot of agricultural commodity storage facilities, massive grain silos visible from altitude, the buffer stocks of the food system, the strategic reserve of calories that moderated but could not prevent food price transmission"))

act2_add.append(clip(193,
    "The ITM Trading live stream on Day Six captured retail investor anxiety about the oil price with particular vividness. 'Oil will be $100 plus next week,' one viewer wrote during the live session. Another: 'Going to war as an economy collapses. Third time?' The pattern recognition of people who had studied economic history was activating.",
    "Close-up of hands on a keyboard in a dark room, the glow of a live stream visible on a monitor, the active participation in a financial community discussing the crisis in real time, the democratized anxious wisdom of the internet age"))

act2_add.append(clip(257,
    "The oil price volatility itself was a problem independent of the level. Businesses that needed to plan capital expenditures, hire staff, or commit to long-term contracts were unable to do so when the price of their primary input cost could swing twenty percent in a week. Uncertainty was its own form of economic damage, freezing decisions that would otherwise be made.",
    "Wide shot of a business planning meeting in progress, a whiteboard with numbers visible but not legible, participants in discussion, the organized attempt to plan around information that keeps changing, the management of uncertainty"))

act2_add.append(clip(193,
    "The Soar Financially analysis with Dr. Komal Sri-Kumar on 'WAR ECONOMY: Stagflation Hits in 2026' provided a framework for what came next: oil shock followed by demand destruction, demand destruction followed by recession, recession coinciding with persistent inflation creating the stagflationary trap that the Fed had no clean tool to escape.",
    "Cinematic wide shot of a traffic intersection during morning rush hour, vehicles gridlocked, horns audible in the scene, the system at maximum load and unable to move, the visual metaphor of stagflation as systemic gridlock"))

act2_add.append(clip(257,
    "The Eurodollar University analysis connected the oil shock of 2026 to the forgotten oil shock of 2007-2008: 'It's been largely forgotten after being understandably overshadowed by the deflationary calamity which overtook it. Before it did, while oil was soaring, what almost every central bank missed was the credit implosion building beneath the surface.' In 2026, the credit implosion was not hidden. It was in plain sight.",
    "Slow dolly shot through a financial archive, rows of thick binders and bound reports, the accumulated documentation of the 2008 crisis visible in the physical records, the paper trail of how the world learned what it had missed"))

act2_add.append(clip(193,
    "The David Lin interview with Kevin Steuer on market panic signals identified three specific indicators that professional traders were watching: the spread between investment-grade and high-yield corporate bonds, the behavior of the dollar index in currency markets, and the volatility of the VIX options market. All three were signaling elevated stress as of mid-March.",
    "Close-up of three separate measurement instruments in a row, each with a different scale and needle, each showing an elevated reading, the panel of warning signals captured in the visual language of instrumentation"))

act2_add.append(clip(257,
    "The offshore dollar — the Eurodollar market that Jeffrey Snider of Eurodollar University had spent years analyzing — was showing the specific stress patterns that precede credit events. The cross-currency basis swap was widening. Short-term dollar funding was becoming more expensive. These were not mainstream financial indicators. They were the early seismic signals in the financial plumbing.",
    "Dramatic close-up of water pressure gauges in an industrial pipe system, multiple gauges showing increasing pressure, steam beginning to appear at a joint, the infrastructure under stress that is not visible to those using the tap above"))

act2_add.append(clip(193,
    "The Market Insider's 'Investment in Today's US Market' analysis offered a contrarian angle: money was flowing from Wall Street to the real economy. Main Street's turn. But that narrative — compelling in a stable environment — seemed to assume that the real economy was not itself under siege from energy costs and credit tightening. The timing was unfortunate.",
    "Wide aerial shot of a busy commercial district with both large chain stores and small independent businesses visible, the mixed economy of corporate and local commerce, the real economy as it actually exists beyond the Wall Street abstraction"))

act2_add.append(clip(257,
    "By the end of the first week of the oil shock, a consensus had formed among the most credible financial analysts: this was not a spike that would quickly revert. The Hormuz closure was a structural disruption of uncertain duration. The market needed to price in not just the current oil price but the option value of further escalation. That option value was substantial.",
    "Wide aerial shot of an options trading floor, specialists at their stations, the organized complexity of derivative pricing, the market mechanism for expressing uncertainty about future prices in the language of options contracts"))

# ============================================================
# ACT III ADDITIONS (~15 clips)
# ============================================================
act3_add = []

act3_add.append(clip(257,
    "The private credit crisis had a specific vocabulary that needed translation for retail investors. A 'gated fund' does not mean the fund has failed. It means the fund manager has suspended redemptions — temporarily, in theory — to prevent a run on the fund's assets. In practice, once a gate goes up, the question of when it comes down depends entirely on whether the underlying assets can be liquidated at the prices on the fund's books.",
    "Cinematic close-up of a heavy gate swinging closed on a financial institution entrance, the mechanism of the closing visible, the security personnel stepping forward, the physical instantiation of redemption restriction"))

act3_add.append(clip(193,
    "The fundamental problem with private credit fund valuation was the mark-to-model issue. Unlike a public bond, which trades on an exchange and has a real-time market price, a private credit loan has no market. It has a model. And the model is run by the fund manager who has an incentive to show the highest possible valuation to retain investor capital.",
    "Close-up of a computer screen displaying a financial model spreadsheet, a cursor hovering over an input cell, the single point of control that determines a billion dollars of reported value, the power of the assumption behind the model"))

act3_add.append(clip(257,
    "The regulatory architecture around private credit funds was built on an assumption that these were sophisticated institutional investors who could protect themselves. The assumption was not entirely wrong. But 'sophisticated' in this context meant sophisticated enough to buy the product, not sophisticated enough to independently value it. When every fund's valuation model said everything was fine, there was no independent signal to the contrary.",
    "Wide shot of a regulatory office with stacks of applications and filings in trays, a regulator at a desk examining documents, the human scale of financial oversight, the gap between the volume of activity and the resources available to monitor it"))

act3_add.append(clip(193,
    "The Adam Taggart Thoughtful Money interview with Chris Irons was the most detailed examination of the private credit crisis mechanism available on YouTube. Irons documented how the cycle of borrowing, reinvesting, and leveraging within private credit funds had created a system that could only function smoothly if redemption requests remained below a threshold. Above that threshold, the structure collapsed.",
    "Cinematic wide shot of a Jenga tower mid-game, many pieces removed and the tower still standing, a hand reaching carefully for another piece, the instability visible in the lean of the remaining structure, the moment before"))

act3_add.append(clip(257,
    "The Blackstone story deserved particular attention because of the fund involved: BREIT, the Blackstone Real Estate Income Trust, with over sixty billion dollars in assets at its peak. BREIT had been the flagship product of the democratization of alternative investments — the vehicle through which smaller institutional investors could access real estate returns previously available only to the very largest.",
    "Aerial drone shot over a commercial real estate portfolio from altitude, office towers, retail centers, and multifamily residential buildings visible in a single frame, the diversified real estate assets that underpinned the private credit promise"))

act3_add.append(clip(193,
    "When BREIT began gating redemptions in late 2022, it was a warning shot that the financial press underweighted. The mechanism that had begun with real estate funds had now spread to the broader private credit ecosystem. The gating was not confined to one asset class or one manager. It was systematic.",
    "Slow zoom into a tree with multiple branches, each branch representing a different fund manager or asset class, the shared root system of the private credit market, the structural connection that made contagion inevitable"))

act3_add.append(clip(257,
    "The Cliffwater thirty-three billion dollar fund deserved specific attention. Cliffwater was not a household name — it was a fund-of-funds operator serving institutional investors. Its size reflected not a single large investor but hundreds of pension funds, endowments, and institutional allocators who had individually made reasonable-seeming decisions to diversify into private credit.",
    "Wide cinematic shot of a pension fund investment committee meeting room, empty chairs around a table, projected charts visible on a screen at the front, the institutional decision-making environment that allocated capital into private credit"))

act3_add.append(clip(193,
    "Each of those pension funds had its own beneficiaries — teachers, firefighters, municipal workers — whose retirement security depended on investment returns. The private credit allocation had improved returns for years. Now it was the source of the problem. The workers who had been promised a secure retirement were discovering that 'secure' had been conditional on assumptions that were no longer valid.",
    "Close-up of a union contract document on a table, a handshake gesture visible above it, the representation of the promised security of institutional benefit plans, the social contract between employers and workers about retirement"))

act3_add.append(clip(257,
    "The Blue Owl situation was particularly complex. Blue Owl was among the most institutional of the private credit managers — a firm whose marketing emphasized its conservative approach and risk management. When Blue Owl experienced redemption pressure, the market read it as confirmation that the problem was not concentrated in the riskiest operators but was systemic across the industry.",
    "Cinematic wide shot of a financial conference room where multiple institutional partners are seated, the formality of investment committee governance, the architecture of fiduciary responsibility in a moment when that responsibility was failing"))

act3_add.append(clip(193,
    "The YouTube comment that read 'private or private fraud is fraud' captured the retail investor view of the private credit collapse with harsh efficiency. The distinction between legal and ethical had, in the viewer's judgment, been collapsed by the behavior of the institutions involved. The mechanisms were legal. The outcomes were not ethical.",
    "Wide shot of a courthouse exterior, the steps visible, pedestrians walking past, the institution of legal adjudication and its relationship to economic justice, the gap between what is legal and what is right made architectural"))

act3_add.append(clip(257,
    "The parallel to 2008 that Eurodollar University drew was not merely rhetorical. The specific mechanism — a market where prices are self-reported, where liquidity is assumed to be available until it suddenly isn't, where the opacity of the underlying assets prevents outside assessment — was the same mechanism that had allowed the subprime mortgage crisis to develop invisible to regulators for years.",
    "Slow dolly shot through a museum exhibit on the 2008 financial crisis, news clippings and timelines on display boards, the documented record of how the crisis unfolded, the historical narrative that was now rhyming with the present"))

act3_add.append(clip(193,
    "The Bank of England was watching the UK private credit situation with particular concern. The Prudential Regulation Authority had been flagging the growth of the UK private credit market as a potential systemic risk for eighteen months. The two British firm collapses in a single week had validated those concerns in the most public and costly way possible.",
    "Wide aerial shot of the Bank of England building in the City of London, the historic stone building at the center of the financial district, the institution that had managed British monetary affairs since 1694 now facing a crisis its regulatory framework had not anticipated"))

act3_add.append(clip(257,
    "The contagion from UK private credit to US institutions was the specific development that elevated the crisis from national to global. As Stoic Finance reported: 'Contagion from these British collapsing firms has already started to spread to American private credit firms and American and even global banking organizations.' The financial system was discovering its interconnections.",
    "Aerial shot of the Atlantic Ocean from above, the vast water stretching to both horizons, the cables and connections that run beneath the surface connecting the financial systems of two continents, the invisible infrastructure of global finance"))

act3_add.append(clip(193,
    "The Federal Reserve's financial stability team — which produces the twice-yearly Financial Stability Report — had identified private credit as a potential vulnerability in its most recent report. The identification of a vulnerability and the ability to prevent its materialization are, in financial regulation, very different capabilities. The vulnerability had been identified. The prevention had not occurred.",
    "Close-up of a thick bound report labeled 'Financial Stability' on a government desk, a hand opening to a flagged page, the formal warning that preceded the actual event, the gap between forecast and preparation"))

act3_add.append(clip(257,
    "The net result of the private credit crisis for the real economy was a sudden and severe contraction of available credit for mid-market companies — businesses too large for community banks and too small for public bond markets. This was precisely the segment of the economy that drives employment growth, innovation, and the majority of net job creation. Cutting off their access to capital was cutting off the fuel supply to the engine of employment.",
    "Wide aerial shot of a mid-size American city's commercial district, a mix of business types visible, the economic ecosystem of the mid-market, the diverse employment base that the private credit contraction would put under stress"))

# ============================================================
# ACT IV ADDITIONS (~12 clips)
# ============================================================
act4_add = []

act4_add.append(clip(257,
    "The Federal Reserve's November 2025 decision to pause its rate hiking cycle — citing evidence of economic cooling — had created a specific vulnerability. The pause had been intended to provide relief to an economy showing signs of stress. Instead, with inflation now re-accelerating through the oil channel, the pause had left the Fed in the worst possible position: not tight enough to prevent inflation, not loose enough to prevent recession.",
    "Aerial drone shot over a river with a large dam, the dam gates partially open, the controlled release of water that is both too much and not enough, the engineering of an imperfect solution to a natural force"))

act4_add.append(clip(193,
    "The bond market's response to the Fed's dilemma was telling. Ten-year Treasury yields were rising — not because the Fed was hiking, but because the bond market was demanding a higher inflation premium. Real yields, adjusted for inflation expectations, were actually falling. This combination — rising nominal yields, falling real yields — was the bond market's way of saying: we expect the Fed to fall behind the curve.",
    "Close-up of a ship's navigation console showing multiple instrument readouts, speed and heading and depth all in view simultaneously, the challenge of managing multiple variables under stress, the navigation metaphor for monetary policy"))

act4_add.append(clip(257,
    "The political dimension of the Fed's bind was particularly acute. The administration, which had spent months pressuring the Fed to cut rates to support the economy, was now watching oil-driven inflation threaten to politically discredit both the war and the economic management of the past four years. The pressure on the Fed to do something — anything — was increasing with every weekly CPI release.",
    "Wide shot of the Capitol building and the Federal Reserve building from opposite sides, the two institutions of American economic governance, their relationship captured in the geography of Washington DC's power architecture"))

act4_add.append(clip(193,
    "Rosenberg Research's analysis of the K-shaped economy included a particularly sobering data point: real consumer spending for the bottom fifty percent of income earners had been negative for six consecutive quarters before the war began. The consumer economy that accounted for seventy percent of GDP was being carried almost entirely by the spending of the top thirty percent.",
    "Cinematic wide shot of a high-end shopping district, luxury boutiques and restaurants busy with well-dressed customers, a visual of consumption that was real but not representative, the thirty percent carrying the consumer economy"))

act4_add.append(clip(257,
    "The payroll revision story was quietly devastating. The Bureau of Labor Statistics had revised downward the initial jobs reports for eleven of the previous twelve months — in aggregate, by over three hundred thousand jobs. The economy had created significantly fewer jobs than reported. The labor market was significantly weaker than the headlines had suggested. This revision pattern was the dog that hadn't barked.",
    "Close-up of official government statistics documents on a desk, data tables visible but not legible, a yellow highlighter marking a specific row, the forensic reading of official data for the signal buried in the revision footnotes"))

act4_add.append(clip(193,
    "The student debt dimension of the K-shaped economy received examination from Coin Bureau's analysis of the US student loan crisis. One point eight four trillion dollars in student debt — owed primarily by adults in their thirties who had been financially hobbled for life by graduate degrees that did not generate the expected returns — was a structural drag on consumer spending that no monetary policy could easily address.",
    "Wide shot of a university graduation ceremony from the back of the auditorium, hundreds of graduates in robes and caps, the moment of celebration that preceded the decade of debt service, the education investment and its complex returns"))

act4_add.append(clip(257,
    "The Ben Graham principle that informed investors like Meb Faber applied was stark: when the S&P 500 CAPE ratio approaches the levels of 1999, the forward ten-year returns are typically negative in real terms. The equity market, in other words, was not a refuge. It was, for the majority of investors, an overpriced claim on future earnings that might themselves be under pressure.",
    "Aerial drone shot over a crowded stock exchange building from above, the street below busy, the building that housed the price discovery mechanism for American corporate ownership, the implicit promise and its current fragility"))

act4_add.append(clip(193,
    "The circular economy problem that Ken McElroy described was becoming visible in commercial real estate. Values falling, loans underwater, owners unable to refinance, banks unwilling to extend credit, values falling further. The loop was beginning. Private credit had been one of the financing channels for this commercial real estate cycle. Its freeze was accelerating the pressure.",
    "Aerial drone shot over a downtown commercial district, office tower vacancy visible in the darkened windows, a mix of occupied and empty floors, the slow-motion deflation of commercial real estate values made visible in the building facades"))

act4_add.append(clip(257,
    "Joseph Stiglitz's Nobel Prize framework on information asymmetry was directly applicable to the private credit crisis. The fund managers had information that investors did not. The opacity of private credit pricing was a textbook example of the information asymmetry that Stiglitz's work demonstrated led to market failure. The crisis was not just a financial event. It was a vindication of economic theory.",
    "Close-up of a complex contract document being examined with a magnifying glass, the fine print in focus through the glass, the asymmetry of information between those who draft contracts and those who sign them"))

act4_add.append(clip(193,
    "The Federal Reserve's financial stability monitoring did not cover private credit funds directly. They were outside the regulatory perimeter — one of the explicit design features of the shadow banking system that had grown up after 2008. The regulators who had been tasked with preventing a repeat of 2008 had regulated the institutions directly involved in 2008 while the risk migrated to unregulated alternatives.",
    "Wide shot of a city with a clearly defined boundary, the regulated financial district on one side, the shadow banking ecosystem on the other, the boundary between oversight and its absence"))

act4_add.append(clip(257,
    "The Fed's ultimate dilemma was crystallized in a single number: forty trillion dollars. At the average interest rate on the US national debt — approximately three point seven percent in March 2026 — the annual interest cost was one point five trillion dollars. That was larger than the entire defense budget. Every percentage point of rate increase added four hundred billion dollars to the annual interest burden. The math of debt made the Fed a prisoner of its own balance sheet.",
    "Cinematic slow zoom into a US government bond certificate, the formal document with its seals and signatures, the legal claim on future American taxpayer income, the physical form of the debt that constrained every policy option"))

act4_add.append(clip(193,
    "The only escape route from the debt trap that both Martin Wolf and Arthur Hayes agreed upon — from opposite ends of the political spectrum — was inflation. Inflate away the real value of the debt. Pay back forty trillion dollars with dollars that each buy less than the original loan. This was not a plan any government would announce. It was the plan that revealed itself in the absence of any other viable option.",
    "Slow motion close-up of ice melting in a glass, the solid cube losing its definition, becoming liquid, the process of solid form dissolving into something that takes the shape of its container, the metaphor of fixed debt being liquefied by inflation"))

# ============================================================
# ACT V ADDITIONS (~12 clips)
# ============================================================
act5_add = []

act5_add.append(clip(257,
    "The central bank gold buying data that underpinned Stoeferle's thesis was remarkable in its consistency. In 2022, 2023, and 2024, central banks had collectively purchased more gold than in any three-year period since the Bretton Woods system ended in 1971. The buyers included the People's Bank of China, the Reserve Bank of India, the National Bank of Poland, and the central banks of Hungary, Singapore, and Turkey.",
    "Cinematic aerial shot over a central bank headquarters building, the formal architecture of monetary authority, the gold vault below the street level, the institution that was systematically converting dollar reserves into something older and more permanent"))

act5_add.append(clip(193,
    "The Chinese central bank gold buying was the most strategically significant. China's foreign exchange reserves — over three trillion dollars at their peak — were predominantly held in US Treasury bonds. The systematic conversion of a portion of those reserves into gold was China's way of reducing its dependence on an instrument that the US had demonstrated it was willing to weaponize through sanctions.",
    "Wide aerial shot of the People's Bank of China headquarters in Beijing, the building visible in the financial district, the institution managing the world's second largest economy and its reserve diversification strategy"))

act5_add.append(clip(257,
    "The Russia experience was the cautionary tale that motivated every emerging market central bank's gold buying. In February 2022, the US had frozen three hundred billion dollars of Russian central bank reserves held in Western financial institutions overnight, without trial, without due process, without precedent in international financial law. The message to every other central bank: dollar reserves are conditionally safe. Gold is unconditionally safe.",
    "Cinematic wide shot of a central bank vault interior, massive steel doors open, gold bars stacked in rows on shelves, security lighting on the metal, the physical permanence of a store of value that cannot be frozen by a foreign government"))

act5_add.append(clip(193,
    "ITM Trading's Daniela Cambone asked Mark Thornton the direct question about where gold prices were going. His Austrian economics framework produced a directional answer without a price target: 'Gold will go much higher because the monetary policy environment has not changed. The spending has not changed. The debt has not changed. The fundamentals that drive gold prices have not changed. Only the speed of the recognition has changed.'",
    "Slow motion close-up of gold coins being counted by practiced hands, each coin placed in a neat stack, the physical weight and consistency of the metal, the practiced expertise of someone who has held the metal for years"))

act5_add.append(clip(257,
    "The silver supply deficit story was one of the most underappreciated financial narratives in the marketplace. The Silver Institute's data showed a structural deficit of over two hundred million ounces in 2024 — meaning that industrial and investment demand for silver exceeded mine supply by that amount. The deficit had been filled by above-ground inventory. But that inventory had limits.",
    "Aerial drone shot over a silver mine and processing facility from altitude, the industrial infrastructure of silver production, the mine itself a small feature in a large landscape, the supply constraint made visible in the geography of extraction"))

act5_add.append(clip(193,
    "The four CEOs interviewed by Soar Financially — from First Majestic, Pan-American Silver, Endeavor Silver, and Hecla Mining — were unanimous on one point: their businesses had been transformed by the price move to ninety dollars. Free cash flow was 'booming' in the words of one CEO. The mining industry was investing in expansion, in exploration, in the infrastructure of future supply. But mine development takes five to ten years. Short-term supply was fixed.",
    "Wide shot of a silver mine operations facility, the processing plant in full operation, material moving on conveyor belts, workers in hard hats managing the automated systems, the industrial reality of precious metal production"))

act5_add.append(clip(257,
    "The gold-silver ratio history offered a sobering context for the current price levels. In 1980, at the Hunt Brothers' silver price peak, the ratio had briefly reached fourteen to one. In 2020, at the COVID financial panic, it had reached one hundred and twenty-four to one — the most extreme overvaluation of gold relative to silver in modern history. From one hundred and twenty-four, the mean reversion still had a long way to run.",
    "Slow tracking shot along a row of display cases showing the gold and silver prices through history, the numbers and dates creating a visual timeline of the two metals' relationship, the context for the current moment in the longer story"))

act5_add.append(clip(193,
    "Bitcoin's correlation to risk assets during the first two weeks of the war was being closely watched by institutional allocators. In previous geopolitical crises, Bitcoin had sometimes acted as a risk-off asset — rising when uncertainty drove investors away from equities. In March 2026, it had initially fallen with equities, then decoupled, suggesting a market still working out its role.",
    "Wide shot of a trading algorithm visualization, the patterns of automated trading visible in the movements, the non-human intelligence that now conducted the majority of financial market transactions, the technology that was learning what Bitcoin was"))

act5_add.append(clip(257,
    "The ETF flows data for gold in the first week of March 2026 was extraordinary. The SPDR Gold Trust — the world's largest gold ETF — recorded its highest weekly inflow since 2020. Institutional investors who had been underweight gold for years were finally increasing their allocations. The move was not driven by retail enthusiasm. It was driven by institutional recognition of regime change.",
    "Aerial drone shot over a precious metals storage facility, the physical form of gold ETF backing, large bars in climate-controlled storage, the connection between the digital ETF share and the physical metal it represented"))

act5_add.append(clip(193,
    "The Bankless analysis of Bitcoin in the context of the Iran crisis identified a specific pattern that had appeared in prior geopolitical crises: Bitcoin initially falls with all risk assets as liquidity contracts, then recovers and frequently outperforms as the liquidity implications of the crisis become clear. The pattern in March 2026 was tracking that template.",
    "Cinematic aerial shot over a harbor during a storm that is passing, the last rain visible on one side of the frame, sunlight breaking through on the other, the transition from disruption to recovery, the aftermath pattern of the crisis cycle"))

act5_add.append(clip(257,
    "Arthur Hayes' framework for why the Fed would ultimately print money was compelling precisely because it was not based on ideology. It was based on mathematics. At current deficit trajectories, the US government would spend more on interest than on defense by 2026. The political will to reduce spending or raise taxes to the level required to close that gap did not exist in any political party in either chamber of Congress. The math was sovereign.",
    "Close-up of a calculator on a desk, a hand entering numbers, the display showing a large result, the calculation of compound mathematics that no political preference could override, the sovereignty of arithmetic over ideology"))

act5_add.append(clip(193,
    "The cryptocurrency market's reaction to the private credit crisis was itself informative. When Morgan Stanley's North Haven fund gated redemptions, Bitcoin dropped three percent in the immediate aftermath — then recovered the next day. The market was learning that private credit gates were bad for liquidity in general but not specifically bad for Bitcoin. The learning process itself was generating data.",
    "Wide aerial shot of a financial district at dawn, the first light of morning on the buildings, traders visible through windows beginning their day, the market preparing to process another day of information and form new prices"))

# ============================================================
# ACT VI ADDITIONS (~12 clips)
# ============================================================
act6_add = []

act6_add.append(clip(257,
    "The Israel-Iran dimension of the geopolitical chess required separate analysis from the US-Iran dimension. For Israel, the military campaign represented an existential strategic calculation — the elimination of a threat to its existence that had been building for decades. For the United States, it was a more complex calculation involving alliance obligations, regional stability, and economic interests that were now in direct conflict.",
    "Aerial drone shot over the Eastern Mediterranean at dusk, the coastlines of multiple nations visible at altitude, the geography of the conflict's originating parties, the ancient lands that modern geopolitics had made permanently contentious"))

act6_add.append(clip(193,
    "Turkey's position was among the most complex in the regional chess game. A NATO member, Turkey had refrained from joining Western sanctions against Russia. It maintained warm relations with Iran's trading partners. And it had been quietly positioning itself as a potential energy transit corridor that could route oil and gas from multiple sources to European markets outside the Hormuz bottleneck.",
    "Aerial drone shot over the Bosphorus strait in Istanbul, ships passing through the narrow waterway that connects the Black Sea to the Mediterranean, the geography of Turkey's strategic position made visible in the transit channel"))

act6_add.append(clip(257,
    "India's response to the oil shock was revealing. The world's third largest oil consumer, India had been quietly negotiating discounted Russian crude since 2022 through a mechanism that allowed it to pay in rupees or through third-country financial intermediaries. The Iran war increased India's motivation to expand those alternative arrangements and to accelerate its development of domestic refining capacity.",
    "Aerial drone shot over an Indian oil refinery complex, the vast processing facilities visible from altitude, the industrial scale of India's domestic energy processing, the strategic investment in independence from the global price benchmark"))

act6_add.append(clip(193,
    "The Japan and South Korea dimension was also significant. Both nations were more exposed to Hormuz disruption than any other G7 economy — both were almost entirely dependent on imported oil and LNG, and both had limited alternative supply routes. Their governments were simultaneously managing the diplomatic challenge of maintaining alliance relationships with the United States while facing serious energy security emergencies.",
    "Wide aerial shot of a Japanese coastal city at night, the urban industrial complex visible, a power plant on the waterfront, the dependency of modern civilization on the reliable supply of energy that was now under threat"))

act6_add.append(clip(257,
    "The Belt and Road Infrastructure investment that China had made across Central Asia — pipelines, railways, ports — was revealing its strategic value in the 2026 crisis. China's energy security did not depend on a single chokepoint. It depended on a diversified network of overland and maritime routes that no single military action could simultaneously disrupt. This was infrastructure as geopolitical resilience.",
    "Aerial drone shot over a major infrastructure corridor in Central Asia, roads and rail lines and pipelines running parallel through mountain terrain, the physical backbone of Chinese energy security strategy"))

act6_add.append(clip(193,
    "The cobalt story deserved mention alongside the copper narrative. Cobalt — essential for EV batteries and increasingly for the defense technology that the Iran war was consuming — was ninety percent processed in China from mines primarily in the Democratic Republic of Congo. The US had no significant cobalt processing capacity. Another critical mineral, another strategic vulnerability.",
    "Close-up of cobalt-blue mineral ore in a worker's hand, the deep blue of the raw material, the connection between this chemical element and the batteries that powered the electrified economy and the weapons that fought the war"))

act6_add.append(clip(257,
    "The European response to the energy crisis was moving along two parallel tracks. The short-term track: emergency energy sharing agreements between member states, accelerated permitting for LNG regasification terminals, and emergency drawdowns of strategic petroleum reserves. The medium-term track: a forced acceleration of renewable energy investment that the energy price crisis made economically compelling where political commitment alone had not.",
    "Aerial drone shot over a European offshore wind farm, the towers standing in grey water, blades turning, the renewable energy transition made visible in the sea, the forced march to energy independence that crisis had accelerated"))

act6_add.append(clip(193,
    "The rare earth dimension of the US-China geopolitical competition received new urgency from the Iran war. US precision weapons systems — the kind deployed in the Iran strikes — require rare earth elements in their guidance, targeting, and communication systems. China's dominance in rare earth processing was not merely an economic fact. It was a defense industrial vulnerability that the war had made visible.",
    "Close-up of the components of a precision electronic device, the circuit board with its miniature components, the rare earth elements invisible but present in every piece of technology that the modern military depended upon"))

act6_add.append(clip(257,
    "Martin Wolf's ultimate geopolitical assessment was sobering and historically grounded: 'The Persians fought the Roman Empire to a standstill over centuries.' The implication was not that Iran would win the military conflict. It was that the Middle East had been the graveyard of imperial ambitions for three thousand years, and there was no reason to believe that the twenty-first century would be different.",
    "Aerial drone shot over ancient archaeological ruins in the Middle East, the remains of civilizations that had each believed themselves permanent, the deep historical time visible in the eroded stone structures, the humility of the long view"))

act6_add.append(clip(193,
    "The commodity supercycle thesis — articulated by analysts from Commodities Culture to Wealthion to Soar Financially — found its most powerful validation in the copper deficit data. Mine development lead times of seven to fifteen years meant that no amount of investment decided in 2026 could solve the copper deficit before 2033 at the earliest. The price would have to clear the market through demand destruction or substitution, neither of which was quickly achievable.",
    "Wide aerial drone shot over a copper mine development site where new infrastructure is being built, the early stages of a project that would take a decade to produce material, the long planning horizon of the mining industry"))

act6_add.append(clip(257,
    "The AI energy demand story, as Jeremy Schwartz elaborated, was genuinely exponential. Each generation of AI model required dramatically more compute than the last. Each unit of compute required energy. Each unit of energy required infrastructure. And all of that infrastructure required copper. The multiplier effect of AI on copper demand was not a linear projection — it was a geometric one.",
    "Cinematic aerial shot of a massive AI data center campus under construction, buildings in various stages of completion, transformer substations visible on the perimeter, the power infrastructure of artificial intelligence made physical"))

act6_add.append(clip(193,
    "The great irony of the March 2026 crisis was that the very technologies intended to provide long-term energy security — solar panels, electric vehicles, battery storage, wind turbines — all required the commodities that the current crisis was making more expensive and less accessible. The path to energy independence ran through a temporary worsening of the energy dependency that the path was designed to escape.",
    "Wide aerial shot of a solar panel manufacturing facility, the production line visible through skylights, the industrial scale of renewable energy equipment production, the paradox of building independence through the very globalized supply chains that created vulnerability"))

# ============================================================
# ACT VII ADDITIONS (~12 clips)
# ============================================================
act7_add = []

act7_add.append(clip(257,
    "The aggregate economic modeling of the four compound crises — oil shock, private credit freeze, Fed bind, K-shaped recession — produced forecasts that varied widely but clustered around a common central tendency: a US recession by Q3 2026 was more likely than not. Goldman Sachs had revised its recession probability to sixty-two percent. Morgan Stanley was at fifty-eight percent.",
    "Wide shot of an economic research department, analysts at workstations with multiple screens, the modeling and forecasting infrastructure of institutional economics, the quantification of uncertainty in pursuit of actionable probability"))

act7_add.append(clip(193,
    "But recession probabilities are not policy prescriptions. They are assessments of what is likely absent intervention. And in March 2026, the nature of potential interventions — fiscal stimulus, Fed accommodation, sanctions relief, diplomatic resolution of the Iran conflict — each carried its own costs and risks. There was no clean path to a soft landing.",
    "Cinematic wide shot of a ship in dense fog, navigating by instruments only, the bow barely visible from the bridge, the careful management of uncertainty when the normal reference points have disappeared"))

act7_add.append(clip(257,
    "The historical parallel that offered the most practical guidance was not 1973 but 1980. The 1980 crisis combined the second oil shock, the hostage crisis, high inflation, and a Federal Reserve under Paul Volcker that was determined to break inflation expectations regardless of the economic cost. The result was a severe double-dip recession. But it cleared the inflation and set the stage for the longest peacetime expansion in American history.",
    "Wide shot of a controlled demolition of an old building, the building beginning to collapse in an orderly way, the planned destruction that creates the cleared ground for what comes next, the deliberate sacrifice of the old structure"))

act7_add.append(clip(193,
    "The Volcker parallel was both instructive and terrifying. Volcker's Federal Reserve raised rates to twenty percent to break inflation expectations. The mortgage market shut down. Unemployment reached nearly eleven percent. The political cost was enormous. But the credibility gained was worth it in long-term economic terms. In 2026, with forty trillion in debt, rates at twenty percent were arithmetically impossible.",
    "Close-up of an old door being opened with a key, the lock mechanism visible, the key turning, the metaphor of the policy tool available to open the lock of inflation, the question of whether the key still fit the lock"))

act7_add.append(clip(257,
    "The most important thing that the March 2026 crisis revealed was not a new vulnerability. It revealed existing vulnerabilities that had been present for years but were obscured by the stability that cheap energy, easy credit, and geopolitical calm had provided. Remove those three conditions simultaneously, and the fragilities that had been accumulating become visible.",
    "Cinematic wide shot of a river at low tide, the rocks and debris on the riverbed now visible that the water had hidden, the exposure of what the flow had been concealing, the revelation that comes with the receding of the covering layer"))

act7_add.append(clip(193,
    "For the community of financial channel viewers — the millions of people who had found their financial education not in business schools but in YouTube channels, Substack newsletters, and podcast communities — March 2026 was a validation of years of heterodox analysis. The mainstream institutions had said debt didn't matter. The energy supply was resilient. The private credit market was well-managed. All three propositions had been tested and found wanting.",
    "Wide shot of a diverse group of people watching a financial presentation on their various devices — phones, tablets, laptops — each in their own environment, the distributed education of the self-taught investor in the digital age"))

act7_add.append(clip(257,
    "Wealthion's War, Oil, and Debt Spiral analysis identified the inescapable conclusion: the debt spiral was not a future risk. It was a present reality. Every dollar of new debt issued to fight the war, to stabilize the energy market, to bail out private credit funds, was added to a pile that was already beyond the capacity of normal economic growth to service. The spiral accelerated with each new intervention.",
    "Cinematic aerial shot of a whirlpool forming at the mouth of a river, the water moving in an accelerating circular pattern, the physics of the spiral visible from above, the increasing speed of rotation as the center deepens"))

act7_add.append(clip(193,
    "The IMF, in its emergency March briefing, raised its global growth forecast downgrade to one and a half percentage points from its February projections. In absolute terms, one and a half percentage points of global GDP represents approximately one point eight trillion dollars of economic output that would not be produced. The human cost of that figure — in jobs not created, incomes not earned, investments not made — was incalculable.",
    "Wide aerial shot of a global logistics hub, goods moving in every direction, the physical representation of the global economic activity that generates the GDP figures, the human activity behind the abstract number"))

act7_add.append(clip(257,
    "The long-term investors who were best positioned for the March 2026 environment had one thing in common: they had not constructed their portfolios on the assumption of any single future. They held gold because gold performed well in both inflationary and deflationary crises. They held commodity equities because the structural deficit in supply was independent of any single geopolitical event. They held cash because cash preserved optionality.",
    "Wide shot of a well-organized investment office, multiple monitors showing different asset classes, the discipline of diversification visible in the allocation of attention across different markets and instruments"))

act7_add.append(clip(193,
    "The ITM Trading community's accumulated wisdom on portfolio construction — accumulated over decades of watching monetary systems degrade — was reaching its moment. 'Get it in your hands and put it in a safe place.' The people who had been called eccentric for following this advice were now the ones whose portfolios were holding up while the paper systems were under stress.",
    "Close-up of a safe door being opened to reveal gold coins inside, the hand on the handle, the satisfying weight of the door, the physical reality of wealth that exists outside the financial system that was under siege"))

act7_add.append(clip(257,
    "And yet, we should resist the narrative of vindication. The people who had been warning about these risks for years had also been warning about them in years when the risks did not materialize. The stopped clock is right twice a day. The genuine insight is not that these risks were real — they were always real. The insight is that they converged in March 2026 in a way that compressed decades of building vulnerability into two weeks of acute crisis.",
    "Wide cinematic shot of a clock tower in a city square, the clock keeping its steady time, the city around it in motion, the permanence of the timekeeping mechanism against the contingent flow of events it measures"))

act7_add.append(clip(193,
    "The final word belongs to the commenters — the retail investors who had been watching, learning, arguing, and feeling the economy in their bones while the professional class was discussing it in conference rooms. 'The hardest part of a twelve-day war are the first twelve years.' The commenter who wrote that on ITM Trading understood something fundamental: what begins in thirteen days takes years to end.",
    "Slow final aerial drone shot beginning tight on a single building, then pulling back to reveal the city block, then the neighborhood, then the city, then the metropolitan region, the individual story expanding to reveal its place in the larger human system, the world on fire and the people living in it"))

# ============================================================
# ADD CLIPS TO SEGMENTS
# ============================================================
segment_additions = {
    "Act I: Cold Open — The Iran War Begins": act1_add,
    "Act II: Oil Shock — Hormuz Dark, Brent at $115": act2_add,
    "Act III: Private Credit Crisis — $300 Billion Contagion": act3_add,
    "Act IV: The Fed's Impossible Bind — Stagflation Returns": act4_add,
    "Act V: Safe Havens — Gold $5,200, Silver $90, Bitcoin as Smoke Alarm": act5_add,
    "Act VI: Geopolitical Chess — Russia Wins, China Adapts, Europe Fractures": act6_add,
    "Act VII: The Reckoning — Synthesis and What Lies Ahead": act7_add,
}

for seg in segments:
    act_name = seg['act']
    if act_name in segment_additions:
        seg['clips'].extend(segment_additions[act_name])

# Renumber all clips sequentially
global_counter = 0
for seg in segments:
    for c in seg['clips']:
        global_counter += 1
        c['id'] = f"clip{global_counter:03d}"

# Write updated file
with open('/home/user/workspace/v5_script.json', 'w', encoding='utf-8') as f:
    json.dump(script, f, indent=2, ensure_ascii=False)

# Statistics
total_clips = sum(len(seg['clips']) for seg in segments)
total_narration_words = sum(
    len(c['narration'].split())
    for seg in segments
    for c in seg['clips']
)
duration_minutes = total_narration_words / 135

print(f"\n{'='*60}")
print(f"UPDATED DOCUMENTARY SCRIPT SUMMARY")
print(f"{'='*60}")
print(f"Title: {script['title']}")
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

import os
size = os.path.getsize('/home/user/workspace/v5_script.json')
print(f"\nFile size: {size/1024:.1f} KB")
print(f"\nFile saved: /home/user/workspace/v5_script.json")
