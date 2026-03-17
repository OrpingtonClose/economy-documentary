import json

STYLE = "cinematic documentary, photorealistic, Arri Alexa, anamorphic bokeh, film grain, shallow depth of field"
NEG = "blurry, low quality, distorted, text, watermark, logo, subtitles, words, letters, numbers, UI overlay, cartoon, anime, illustration, painting, drawing, screen with text, monitor with data"

# Color palettes per act
WARM = "warm amber tones, golden hour light"
COLD = "cold blue-grey tones, harsh fluorescent light, overcast atmosphere"
SUNRISE = "warm golden sunrise tones, hopeful amber light"

clip_counter = [0]

def clip(frames, prompt_body, color_temp=WARM):
    clip_counter[0] += 1
    dur_map = {257: 10.7, 105: 4.4, 73: 3.0}
    return {
        "id": f"clip{clip_counter[0]:03d}",
        "frames": frames,
        "duration_est": dur_map[frames],
        "prompt": f"{prompt_body}, {color_temp}, {STYLE}"
    }

segments = []

# ============================================================
# ACT 1 — WARM AMBER, GOLDEN HOUR, FIRELIGHT
# ============================================================

# seg01 (26.6s) → 3×257 = 32.1s
segments.append({
    "id": "seg01",
    "act": 1,
    "narration": "Twenty percent of the world's oil passes through a body of water that is thirty-three kilometers wide. The Strait of Hormuz. At its narrowest, you could see the other side. For decades, this corridor has been the quiet engine of the global economy — tankers moving through, day and night, carrying the energy that keeps the lights on in Tokyo, the factories running in Frankfurt, the trucks moving across America.",
    "tts_duration": 26.6,
    "clips": [
        clip(257, "A weathered sailor's hands grip binoculars, slowly raising them to scan across a narrow ocean strait, golden hour light catching salt spray on bridge windows, warm reflections dancing on aged brass instruments, the open sea visible through scratched glass"),
        clip(257, "POV through binoculars revealing a procession of enormous oil tankers stretching toward the horizon, each vessel riding low under heavy cargo, golden sunlight painting long amber shadows across rusted hulls, narrow strait waters shimmering between distant coastlines, atmospheric haze"),
        clip(257, "Tracking shot following thick mooring ropes being hauled by hydraulic winches on a tanker deck, dock workers in hard hats directing with broad hand signals, diesel exhaust mixing with sea mist in golden dawn light, chains clanking and cables tensing under load"),
    ]
})

# breathing01 → 1×105 = 4.4s
segments.append({
    "id": "breathing01",
    "act": 1,
    "narration": "",
    "tts_duration": 0,
    "clips": [
        clip(105, "Slow dolly across the still surface of dark ocean water at dusk, a single tanker's wake rippling outward in concentric golden rings, the last sliver of sunlight dissolving into the waterline"),
    ]
})

# seg02 (3.7s) → 2×73 = 6.0s
segments.append({
    "id": "seg02",
    "act": 1,
    "narration": "On March second, twenty twenty-six, that corridor closed.",
    "tts_duration": 3.7,
    "clips": [
        clip(73, "A heavy steel blast door swinging shut in slow motion inside a port facility, sparks flying from friction against the metal frame, amber emergency light spilling through the narrowing gap"),
        clip(73, "Close-up of a thick chain being pulled taut across a harbor boom, links snapping into tension one by one, water droplets shaking loose in golden backlight, the strait visible beyond as a dark silhouette"),
    ]
})

# seg03 (44.4s) → 4×257 + 1×105 = 47.2s
segments.append({
    "id": "seg03",
    "act": 1,
    "narration": "Iranian drones struck Qatar's gas infrastructure at Ras Laffan — the nerve center of the country's entire energy export system. Qatar is not a minor player. It is the world's second-largest exporter of liquefied natural gas. When those facilities went dark, Qatar's national energy company did something that is rarely seen in global commodity markets: it declared force majeure. That is a legal term, and what it means in practice is this — Qatar told every country it ships gas to, every long-term buyer in Europe, in Asia, across the world: we cannot deliver. The contracts are suspended.",
    "tts_duration": 44.4,
    "clips": [
        clip(257, "Aerial tracking shot over a vast industrial gas processing complex at twilight, flare stacks erupting with tall orange flames against a darkening amber sky, pipes and cooling towers stretching in geometric rows, heat shimmer distorting the air above massive cylindrical storage tanks"),
        clip(257, "Macro close-up of a pressure gauge needle plummeting rapidly from green to red zone, the glass face reflecting flickering amber warning lights, a gloved hand reaching to tap the gauge in futile urgency, condensation beading on steel pipes behind"),
        clip(257, "Over-the-shoulder shot of a control room operator's hands frantically pulling lever after lever on an industrial panel, rows of amber indicator lights switching from steady to flashing, the operator's hard hat visible at frame edge, steam venting visible through a reinforced window"),
        clip(257, "Dolly-in on a row of massive LNG carrier ships sitting motionless at anchor in amber-lit waters, their cargo arms hanging limp and disconnected from shore terminals, tugboats drifting idle alongside, no movement on any deck"),
        clip(105, "Close-up of an official rubber stamp pressing down hard onto a document, the impact sending a small burst of paper dust into warm side-light, a hand lifting to reveal the deep indentation, amber desk lamp casting long shadows"),
    ]
})

# seg04 (25.3s) → 3×257 = 32.1s
segments.append({
    "id": "seg04",
    "act": 1,
    "narration": "Within days, over one hundred fifty tankers were stranded at anchor, unable to transit the strait. Asian spot gas prices surged one hundred thirty-seven percent in under a week. LNG tanker charter rates doubled to two hundred thousand dollars per day. Leading maritime insurers cancelled all war risk coverage for vessels in the region. The world's most important energy chokepoint had become a wall.",
    "tts_duration": 25.3,
    "clips": [
        clip(257, "Sweeping crane shot revealing dozens of oil tankers sitting motionless in congested anchorage waters, their anchor chains disappearing into murky amber-lit sea, no wake behind any vessel, a sense of total paralysis across the water, heat haze rising from idle engine stacks"),
        clip(257, "Frantic hands on a trading floor desk slamming down phones and grabbing new ones, fingers jabbing at blinking switchboard lights, papers scattering, amber-tinted overhead lights reflecting off sweating foreheads, multiple arms reaching across the frame in chaotic choreography"),
        clip(257, "Tracking shot along a concrete harbor wall where massive mooring bollards stand empty, ropes coiled unused on the ground, a lone dockworker walking away from camera with hands in pockets, golden dust motes floating in low angled sunlight, the strait a still golden band in the background"),
    ]
})

# seg05 (65.5s) → 6×257 + 1×73 = 67.2s
segments.append({
    "id": "seg05",
    "act": 1,
    "narration": "Now, an energy shock of this kind would be serious for any economy. But for Europe, it was something closer to an existential threat. And to understand why, you need to know what Europe did after twenty twenty-two. When Russia invaded Ukraine and pipeline gas from Moscow dried up, Europe made a bet. It replaced Russian gas — which had been cheap, continuous, and delivered by pipe — with liquefied natural gas shipped by sea, primarily from the United States and Qatar. Russia's share of European gas fell from forty percent to just six percent by twenty twenty-five. The US became the dominant supplier, providing sixty percent of Europe's LNG imports. European leaders called this diversification. But it was not diversification. It was a trade — exchanging one geopolitical vulnerability for another. Pipeline gas flows continuously. Seaborne gas passes through chokepoints. And now, the chokepoint had closed.",
    "tts_duration": 65.5,
    "clips": [
        clip(257, "Dolly reveal of a massive underground gas pipeline junction being welded shut, sparks cascading down the curved steel surface, workers in heavy protective gear stepping back as the final seam closes, amber torch light illuminating underground concrete tunnel walls"),
        clip(257, "Time-lapse style tracking shot of an LNG tanker being loaded at a US Gulf Coast export terminal at golden hour, articulated loading arms swinging into position and locking onto the ship's manifold, frost forming on cryogenic pipes as super-cooled gas flows, dock workers monitoring gauges"),
        clip(257, "Over-shoulder shot of a harbor pilot steering an LNG carrier through a narrow shipping lane, hands steady on the wheel, the pilot's weathered face lit by warm instrument panel glow, a vast ocean stretching ahead through the bridge windshield, spray hitting glass"),
        clip(257, "Macro shot of a nautical chart being unrolled across a wooden table by calloused hands, a finger tracing a sea route through narrow strait passages, pencil marks and coffee stains on aged paper, warm desk lamp casting sharp shadows over the chart's contour lines"),
        clip(257, "Tracking follow of a massive LNG tanker entering a narrow waterway between two rocky coastlines, the ship barely fitting between the landmasses, water churning against its bow, warm hazy light filtering through dust and sea salt, the vulnerability of the passage made visceral"),
        clip(257, "Close-up of a steel gate valve being cranked shut by two workers turning a large wheel, the valve's position indicator swinging from open to closed, a hiss of pressurized gas escaping from the seal, amber safety light flashing in the background"),
        clip(73, "A single navigation buoy rocking in empty water where tanker traffic should be, its amber warning light blinking rhythmically, no ships visible in any direction, the loneliness of a closed passage"),
    ]
})

# seg06 (23.4s) → 2×257 + 1×105 = 25.8s
segments.append({
    "id": "seg06",
    "act": 1,
    "narration": "European gas storage fell to thirty percent of capacity — the lowest since the twenty twenty-two crisis, and thirty percent below the seasonal average. Germany's storage sat at roughly the same level. The Netherlands dropped to just ten percent. Goldman Sachs warned that if the disruption lasted two months, European gas could reach one hundred euros per megawatt hour.",
    "tts_duration": 23.4,
    "clips": [
        clip(257, "Slow overhead crane shot descending into a cavernous underground gas storage cavern, the vast emptiness of the space becoming apparent as the camera drops lower, residual amber condensation dripping from the ceiling into shallow puddles on the floor, echoing drips, industrial scale revealing depletion"),
        clip(257, "Reaction shot of a gas facility engineer removing safety goggles and rubbing tired eyes, standing before a wall of analog pressure dials all reading near their minimums, needles hovering in amber-marked danger zones, the engineer's exhale visible in the cold facility air"),
        clip(105, "Macro close-up of a liquid level sight glass on a storage tank showing fluid barely visible at the bottom, amber light refracting through the glass tube, a slow drip forming and falling inside, the graduation marks above showing vast empty capacity"),
    ]
})

# seg07 (48.9s) → 5×257 = 53.5s
segments.append({
    "id": "seg07",
    "act": 1,
    "narration": "Germany was already in trouble before this. Two consecutive years of negative GDP growth. Over twenty-four thousand corporate insolvencies in twenty twenty-five — the highest in a decade. BASF, the world's largest chemical company, had already cut nearly five thousand jobs and permanently closed plants. Thyssenkrupp, the steel giant, announced plans to eliminate eleven thousand positions — forty percent of its steel division. A Federation of German Industries study found that one-fifth of Germany's total industrial output could permanently disappear by twenty thirty. The energy shock did not create this crisis. It accelerated one that was already underway.",
    "tts_duration": 48.9,
    "clips": [
        clip(257, "Tracking shot through an abandoned factory floor, camera gliding past silent conveyor belts and idle robotic arms frozen mid-gesture, amber dust particles floating in shafts of light from high windows, footprints in the dust leading toward an exit door"),
        clip(257, "Close-up of a worker's hands placing personal items into a cardboard box on a desk — a coffee mug, a framed photo face-down, safety goggles — each item placed deliberately, warm overhead light casting deep shadows into the box, a lanyard badge dropped on top last"),
        clip(257, "Dolly shot along a row of massive blast furnaces in a steel mill, their fires extinguished, the hearths dark and cold, residual amber heat shimmer still rising from cooling brick, a worker in a fireproof suit walking alone past the towering structures, scale emphasizing emptiness"),
        clip(257, "Over-shoulder shot of a factory foreman pulling a heavy switch downward on a power distribution panel, rows of indicator lights going dark one section at a time from left to right, the foreman's silhouette against the last amber glow before total darkness"),
        clip(257, "Wide tracking shot of an industrial park at dusk, camera moving past closed loading dock after closed loading dock, roller shutters all pulled down, weeds growing through cracks in the concrete, a single amber streetlight flickering on as daylight fades, no vehicles in any lot"),
    ]
})

# seg08 (43.0s) → 4×257 + 1×73 = 45.8s
segments.append({
    "id": "seg08",
    "act": 1,
    "narration": "But the damage does not stop at industry. Natural gas is not just fuel. It is the primary feedstock for nitrogen fertilizer. When gas prices spike, ammonia plants shut down — because the gas is not just powering the factory, it is the raw ingredient. This happened in twenty twenty-two, when seventy percent of European ammonia production went offline. It is happening again now. And when fertilizer prices rise, food prices follow — not immediately, but within three to six months. The second-order effects of this energy shock have not yet arrived at the grocery store. But they are coming.",
    "tts_duration": 43.0,
    "clips": [
        clip(257, "Close-up of white ammonia granules pouring from a conveyor chute into a collection bin, the stream thinning to a trickle and then stopping entirely, the last granules bouncing and settling, warm industrial lighting reflecting off the white crystalline surface"),
        clip(257, "Tracking shot following a farmer's weathered hands running through depleted soil in a field, fingers crumbling dry earth that falls like dust in golden afternoon light, the hands reaching for an empty fertilizer bag and finding nothing, turning it upside down"),
        clip(257, "POV shot pushing a shopping cart slowly down a grocery store aisle, hands reaching for items on shelves, some shelves partially bare, warm overhead fluorescent light with amber tint, other shoppers visible in soft focus examining products with concerned expressions"),
        clip(257, "Macro shot of wheat stalks in a field bending under their own weight in golden wind, camera pulling focus to reveal the stalks are thin and pale, underfertilized, the amber sunset behind creating silhouettes of struggling crops stretching to the horizon"),
        clip(73, "Close-up of a kitchen faucet dripping water into a pot on a stove, each drop creating expanding rings, warm kitchen light catching steam beginning to rise, a metaphor for slow-building pressure about to reach boiling point"),
    ]
})

# seg09 (51.9s) → 5×257 = 53.5s
segments.append({
    "id": "seg09",
    "act": 1,
    "narration": "Meanwhile, the European Central Bank was caught flat-footed. Its entire macroeconomic framework for twenty twenty-six and twenty twenty-seven had been built on one assumption: that energy prices would remain flat or decline. A seventy percent gas spike invalidated that assumption overnight. Peer-reviewed models show that a ten percent gas price increase raises Euro area inflation by point six percentage points after one year. The math on a seventy percent spike is devastating. The euro fell three and a half percent from its January high, making dollar-priced energy imports even more expensive — creating a self-reinforcing loop. Cut rates, and inflation spirals. Raise rates, and what remains of European industry collapses. The ECB is trapped.",
    "tts_duration": 51.9,
    "clips": [
        clip(257, "Dolly-in on a conference room where officials sit around a curved mahogany table, hands clasped or gripping papers, one figure removing glasses to pinch the bridge of their nose, warm amber chandelier light catching the polished table surface, body language showing collective paralysis"),
        clip(257, "Macro shot of a compass needle spinning wildly without settling on any direction, the brass housing reflecting warm amber light, a hand trying to steady the compass on a table but the needle keeps oscillating, the instability visceral and physical"),
        clip(257, "Over-shoulder shot of an economist's hands working through calculations on paper with a mechanical pencil, crossing out figures and rewriting larger ones, the paper filling with corrections, warm desk lamp illuminating mounting anxiety in the arithmetic, eraser shavings accumulating"),
        clip(257, "Tracking shot of euro coins cascading through a coin-counting machine, the stream accelerating and the coins becoming a blur, some coins bouncing off and scattering across a metal surface, warm overhead light catching their spinning faces"),
        clip(257, "Close-up of two hands gripping opposite ends of a rope in a tug-of-war, the rope fraying at the center where the tension is greatest, individual fibers snapping and curling in warm side-light, neither side yielding, the rope about to break at its midpoint"),
    ]
})

# ============================================================
# TRANSITION — WARM TO COLD
# ============================================================

# seg10_transition (20.0s) → 2×257 = 21.4s
segments.append({
    "id": "seg10_transition",
    "act": "transition",
    "narration": "The energy shock would be dangerous enough on its own. But it did not arrive into a healthy economy. It arrived into an economy that was already fracturing — in oil markets, in equities, in credit, and in the labor market. And each of those fractures is now feeding the others.",
    "tts_duration": 20.0,
    "clips": [
        clip(257, "Slow overhead shot of a frozen lake surface beginning to crack, fracture lines spreading outward from a central point in an intricate web pattern, the ice shifting from warm amber translucency to cold blue-grey opacity as the cracks multiply, meltwater seeping through fissures", "color shifting from warm amber to cold steel blue, transitional twilight"),
        clip(257, "Tracking shot along a concrete dam wall where water is finding its way through hairline cracks, each trickle feeding into larger streams, the lighting transitioning from warm golden remnants to harsh cold blue-white industrial floodlights, the structure under compound stress", "color shifting from warm amber remnants to cold blue-grey, transitional dusk"),
    ]
})

# ============================================================
# ACT 2 — COLD BLUE, GREY, FLUORESCENT, OVERCAST
# ============================================================

# seg11 (77.3s) → 7×257 + 1×105 = 79.3s
segments.append({
    "id": "seg11",
    "act": 2,
    "narration": "Start with oil. West Texas Intermediate crude went from sixty-seven dollars to ninety-one dollars a barrel in a single week — the largest weekly percentage gain since WTI futures began trading in nineteen eighty-three. In subsequent days, it touched one hundred twenty before settling around one hundred seven. Now, the instinct is to focus on the price level. But the level is not what matters most. What matters is the speed. Every major recession in modern history was preceded not by high oil prices, but by rapidly rising oil prices. It is the velocity of the move that disrupts supply chains, spikes input costs across every industry, and forces central banks into impossible decisions. Transportation costs rise immediately. Petrochemical inputs follow within weeks. Food production costs within months. And headline inflation within a quarter. A thirty-five percent weekly oil spike is not a headline. It is a mechanism — the mechanism that has ended business cycles before, and is now active again.",
    "tts_duration": 77.3,
    "clips": [
        clip(257, "Close-up of crude oil erupting from a wellhead valve that has been opened too fast, black liquid spraying upward under enormous pressure, droplets catching cold harsh overhead light, workers in rain gear scrambling to control the flow with wrenches and wheel valves", COLD),
        clip(257, "POV shot from inside a pit at a commodity trading floor, hands everywhere grabbing at signal cards and waving frantically, cold fluorescent light washing out faces twisted in urgency, bodies pressing against the railing, the physical crush of panic selling", COLD),
        clip(257, "Tracking shot following a long line of eighteen-wheeler trucks stopped dead on a grey overcast highway, drivers leaning against their cabs with arms crossed, diesel exhaust rising into cold air, the line stretching beyond visible horizon, freight frozen in place", COLD),
        clip(257, "Macro shot of crude oil being slowly poured from a steel drum, the viscous black liquid stretching and pooling on a cold concrete surface, overhead fluorescent light creating blue-white reflections in the spreading darkness, the pour accelerating ominously", COLD),
        clip(257, "Dolly shot through a petrochemical plant where workers rush along catwalks between massive distillation columns, their breath visible in cold air, warning strobes flashing blue and white, hands gripping cold steel railings, steam venting from emergency relief valves", COLD),
        clip(257, "Over-shoulder shot of a truck driver filling his tank at a gas station, the fuel pump meter spinning with increasing speed, his hand gripping the nozzle tightly, cold overcast sky reflected in the truck's chrome, rain beginning to spot the concrete", COLD),
        clip(257, "Wide tracking shot of a container port where giant cranes stand motionless over stacked shipping containers, a single forklift moving slowly through empty lanes, cold grey fog rolling in from the water, the port operating at a fraction of capacity", COLD),
        clip(105, "Macro close-up of a single drop of crude oil falling in slow motion onto the surface of still water, the impact creating concentric ripples that expand outward, cold blue-grey light reflecting in the oily rainbow sheen spreading across the surface", COLD),
    ]
})

# breathing02 → 1×105 = 4.4s
segments.append({
    "id": "breathing02",
    "act": 2,
    "narration": "",
    "tts_duration": 0,
    "clips": [
        clip(105, "Static wide shot of an empty trading floor after hours, chairs pushed back from desks, cold blue emergency lighting casting long shadows across rows of dark terminals, a single overhead light swinging gently from building vibration", COLD),
    ]
})

# seg12 (35.8s) → 3×257 + 2×73 = 38.1s
segments.append({
    "id": "seg12",
    "act": 2,
    "narration": "Turn to the stock market. The S and P five hundred peaked in late January. It has not crashed — it is doing something quieter and, in many ways, more dangerous. It is rolling over. A slow, grinding decline that happens in the denial phase, while investors insist the dip is buyable. The price-to-earnings ratio sits at twenty-eight. During the last major oil crisis, in nineteen seventy-nine, it was eight. That comparison is not for drama — it is a measurement of how much further there is to fall if this environment persists.",
    "tts_duration": 35.8,
    "clips": [
        clip(257, "Reaction shot of a trader sitting motionless at his desk while colleagues rush past behind him, his eyes fixed on something off-screen, one hand slowly lowering a phone receiver, cold fluorescent light creating harsh shadows under his eyes, the stillness amid chaos", COLD),
        clip(257, "Tracking shot following a marble rolling across a tilted glass table, gaining speed almost imperceptibly, the marble's path curving toward the edge, cold blue light refracting through the glass, the slow inevitable acceleration of the roll, the edge approaching", COLD),
        clip(257, "Overhead dolly shot of a financial district sidewalk at dusk, businesspeople walking with heads down and briefcases gripped tight, cold rain beginning to fall, reflections of grey buildings stretching across wet pavement, umbrellas opening one by one in a slow chain reaction", COLD),
        clip(73, "Close-up of a hand reaching for an elevator button labeled with a down arrow, pressing it firmly, the button illuminating cold blue-white, the hand withdrawing as elevator machinery begins to hum and descend", COLD),
        clip(73, "Macro shot of sand trickling through a narrowing hourglass neck, the upper chamber almost empty, individual grains tumbling in cold diffused light, the flow unstoppable and measured, gravity doing its quiet work", COLD),
    ]
})

# seg13 (57.5s) → 5×257 + 2×73 = 59.5s
segments.append({
    "id": "seg13",
    "act": 2,
    "narration": "But the risk that keeps institutional investors awake is not in the public markets. It is in private credit — a three-trillion-dollar sector that barely existed fifteen years ago. After the two thousand eight crisis, regulations pushed banks away from lending to riskier borrowers. Private firms stepped in: Apollo, Blackstone, Blue Owl Capital. They made loans — trillions of dollars worth — to small and mid-sized companies that traditional banks would no longer touch. And here is the critical detail: those loans are not marked to market. The firms that made them get to decide what they are worth. The industry calls this mark-to-model. Critics call it mark-to-fantasy.",
    "tts_duration": 57.5,
    "clips": [
        clip(257, "Dolly shot through a dimly lit vault corridor, camera moving past rows of safe deposit boxes, a banker's gloved hands turning a key in one lock then another, the heavy door swinging open to reveal an empty compartment inside, cold steel blue lighting reflecting off polished metal", COLD),
        clip(257, "Over-shoulder shot of an investor sitting in a cold modern office at night, staring at multiple screens showing complex derivative structures as interconnected lines, his finger tracing a single thread that connects to dozens of others, cold blue monitor glow on his face, realizing the web's fragility", COLD),
        clip(257, "Tracking shot of hands in an archive room pulling thick loan document binders from shelves, stacking them higher and higher on a metal cart, the stack growing precarious, the cart's wheels beginning to buckle under weight, cold overhead fluorescent strips buzzing", COLD),
        clip(257, "Macro close-up of a jeweler's scale being loaded with small weights on one side, while the other side holds a feather — the weights keep being added but the scale reads balanced, a hand manipulating the fulcrum point underneath to maintain the illusion, cold clinical light exposing the trick", COLD),
        clip(257, "POV shot of someone walking through a construction site for an unfinished office building, exposed concrete and rebar visible, rain dripping through the skeletal roof structure, construction equipment sitting idle and rusting, cold grey daylight filtering through plastic sheeting, a half-built project that may never be completed", COLD),
        clip(73, "Close-up of a house of cards being built on a glass table, a hand carefully placing the final card on top, the structure trembling from the vibration of the hand's withdrawal, cold diffused light making the cards translucent, everything balanced on nothing", COLD),
        clip(73, "Macro shot of a magnifying glass being held over a financial document, but instead of clarifying the text it distorts and warps the surface beneath, the lens creating illusions of order in chaos, cold forensic lighting from above", COLD),
    ]
})

# seg14 (52.6s) → 5×257 + 1×73 = 56.5s
segments.append({
    "id": "seg14",
    "act": 2,
    "narration": "When investors at BlackRock recently asked to withdraw their money from a private credit fund, the fund said no — approving only fifty-four percent of redemption requests and capping withdrawals at five percent. When a fund will not let you leave, that tells you something about what the assets inside it are actually worth. An ex-Lehman Brothers vice president recently noted that securities being carried at full face value on fund books are now being marked to zero — going from par to nothing in a matter of weeks. He counted two known problem spots six months ago. Today there are over seventeen. Jamie Dimon of JP Morgan used the word cockroaches — and the implication was clear. Where you see one, there are many more behind the walls.",
    "tts_duration": 52.6,
    "clips": [
        clip(257, "Tracking shot of a person walking toward a heavy bank vault door and pulling the handle, but the door does not budge, they pull harder with both hands, their shoes sliding on the polished floor, cold steel blue reflections on the vault's surface, the futility of the effort visible in straining arms", COLD),
        clip(257, "Close-up of hands fanning through a thick stack of bond certificates, flipping faster and faster, each page revealing a stamped red mark, the hands slowing as realization sets in, the final pages completely blank, cold overhead light making the paper starkly white", COLD),
        clip(257, "Dolly shot along a row of filing cabinets in a cold basement archive, drawers sliding open one after another as if by invisible hands, each drawer revealing folders marked with red tabs, the row extending deeper into shadow, the number multiplying as the camera advances", COLD),
        clip(257, "Reaction shot of a boardroom where executives sit in frozen silence, one slowly pushing back from the conference table, another pressing fingers against closed eyelids, cold blue-white light from floor-to-ceiling windows illuminating a room full of people who have just received devastating news", COLD),
        clip(257, "Macro shot of a concrete wall where a single hairline crack is visible, camera slowly pulling back to reveal the crack branching into dozens of fracture lines spreading in every direction, cold clinical fluorescent light revealing the full extent of structural failure previously hidden", COLD),
        clip(73, "Close-up of a hand lifting a decorative wallpaper corner in a luxury office, peeling it back to reveal crumbling drywall and dark stains underneath, cold light spilling into the exposed cavity, the facade literally being peeled away", COLD),
    ]
})

# breathing03 → 1×105 = 4.4s
segments.append({
    "id": "breathing03",
    "act": 2,
    "narration": "",
    "tts_duration": 0,
    "clips": [
        clip(105, "Wide static shot of rain streaming down a floor-to-ceiling window in a dark empty office, the city skyline beyond reduced to blurred grey shapes, a single desk chair slowly spinning from residual momentum, cold blue ambient light", COLD),
    ]
})

# seg15 (38.9s) → 4×257 = 42.8s
segments.append({
    "id": "seg15",
    "act": 2,
    "narration": "And this brings us to the trap at the center of it all — the debt. The United States needs to refinance approximately ten trillion dollars in government debt this year. That is not a projection or an estimate. It is a calendar event. And the Federal Reserve faces a dilemma with no clean exit. If it cuts interest rates to ease the refinancing burden, it risks unleashing inflation — especially with oil prices surging. If it raises rates to fight that inflation, it triggers a recession in an economy already shedding jobs. If it does nothing, the refinancing cost at current rates is staggering on its own.",
    "tts_duration": 38.9,
    "clips": [
        clip(257, "Overhead tracking shot of an enormous printing press running at full speed, sheets of thick bond paper feeding through massive rollers, the mechanical rhythm relentless, cold industrial lighting catching ink mist in the air, the sheer volume of output creating a visual sense of overwhelming scale", COLD),
        clip(257, "Dolly-in on a three-way intersection where all traffic signals are simultaneously showing red, cars stopped in every direction with nowhere to go, drivers' hands visible gripping steering wheels, cold overcast light reflecting off wet asphalt, gridlock as metaphor for policy paralysis", COLD),
        clip(257, "Close-up of a tightrope walker's feet inching along a cable, each step calculated, the cable vibrating with tension, a balancing pole gripped by white-knuckled hands just visible at frame edge, cold blue-grey fog obscuring what lies on either side, total focus on the next step", COLD),
        clip(257, "Macro shot of a calendar page being torn away to reveal the next month, the paper ripping in slow motion, behind it another page and another, the act of time passing made physical and urgent, cold diffused window light catching the paper fragments floating downward", COLD),
    ]
})

# seg16 (34.2s) → 3×257 + 1×105 = 36.5s
segments.append({
    "id": "seg16",
    "act": 2,
    "narration": "For the first time in twenty-five years, US Treasury bonds are selling off during a geopolitical crisis — not rallying. In every prior crisis — September eleventh, COVID, the Russia-Ukraine war — investors fled to US bonds for safety. Now they are doing the opposite. Yields are rising, meaning investors are selling. Part of the reason: China, which holds enormous quantities of US debt, gets ninety percent of its oil from Iran. It has every incentive to retaliate economically.",
    "tts_duration": 34.2,
    "clips": [
        clip(257, "Tracking shot of investors physically exiting a bond trading room, pushing through glass doors one after another, their reflections multiplying in the cold blue-tinted glass, the room emptying in an orderly but unstoppable procession, overhead fluorescents buzzing in the increasingly vacant space", COLD),
        clip(257, "Close-up of a compass that has always pointed in one direction now slowly rotating to point the opposite way, the needle swinging past magnetic north and continuing, cold steel blue light on the compass face, a hand holding it steady while the impossible happens", COLD),
        clip(257, "Over-shoulder shot of a port worker guiding an enormous cargo crane loading containers onto a vessel bound in the opposite direction from usual, the crane's boom swinging from east to west, cold grey harbor water below, container markings deliberately illegible, the reversal of trade flows made physical", COLD),
        clip(105, "Macro shot of dominos that have been stood back up in a line, but are now falling in reverse — from the end back to the beginning — each one toppling the previous, cold blue backlight creating sharp silhouettes of each falling piece, the chain reaction running backward", COLD),
    ]
})

# seg17 (34.0s) → 3×257 + 1×105 = 36.5s
segments.append({
    "id": "seg17",
    "act": 2,
    "narration": "The labor market is already weakening beneath all of this. January layoffs were the highest since two thousand nine. The economy shed ninety-two thousand jobs in February. But the monthly number does not capture the full picture. The United States needs roughly two and a half million new jobs per year just to keep pace with population growth and workforce needs. It has been averaging about one million. The cumulative gap is now eight point seven million jobs — a deficit that does not show up in any single month's headline, but compounds quietly in the background.",
    "tts_duration": 34.0,
    "clips": [
        clip(257, "Tracking shot through a long unemployment office hallway, people seated in plastic chairs along both walls, each filling out paperwork on clipboards, the line extending around a corner and out of sight, cold fluorescent overhead strips casting flat shadowless light on tired faces", COLD),
        clip(257, "Close-up of hands removing a nameplate from an office door, sliding it out of the metal bracket and placing it face-down in a cardboard box, the empty bracket left behind, cold hallway light spilling through the door crack, the small act carrying enormous weight", COLD),
        clip(257, "Overhead dolly shot of an empty parking lot at a large office complex, painted lines visible but no cars, a few leaves blowing across the concrete surface in cold wind, the parking lot designed for hundreds showing single-digit occupancy, grey overcast sky reflected in scattered puddles", COLD),
        clip(105, "Macro shot of water slowly dripping into a bucket that is almost overflowing, each drop raising the level imperceptibly closer to the rim, cold diffused light catching each droplet's impact, the accumulation of small additions about to breach the container", COLD),
    ]
})

# seg18 (28.2s) → 3×257 = 32.1s
segments.append({
    "id": "seg18",
    "act": 2,
    "narration": "That gap is now visible in housing. In Laredo, Texas, mortgage delinquency rates have reached twenty-four percent. Nationally, home sales have hit decade lows — and this is with mortgage rates at their lowest point in three and a half years. In a normal cycle, low rates stimulate buying. This time they are not — because the low rates do not reflect economic strength. They reflect weakness. And buyers know it.",
    "tts_duration": 28.2,
    "clips": [
        clip(257, "Tracking shot along a suburban street where every other house has a for-sale sign in the front yard, the signs weathered and faded from months of exposure, overgrown lawns and accumulated mail visible at doorsteps, cold grey overcast light flattening all color from the scene", COLD),
        clip(257, "Close-up of a hand turning a house key in a front door lock, but hesitating and pulling the key back out, the hand retreating into a coat pocket, the door remaining locked, cold wind blowing dead leaves across the porch, the decision not to buy made visible", COLD),
        clip(257, "Dolly shot through an empty model home where staged furniture sits in rooms no one visits, a kitchen counter with fake fruit, cold window light illuminating dust on every surface, the artificiality of optimism in a dying market, a realtor's heels echoing on hollow floors as she walks alone through rooms", COLD),
    ]
})

# seg19 (25.0s) → 3×257 = 32.1s
segments.append({
    "id": "seg19",
    "act": 2,
    "narration": "The foundation is not cracking from one thing. It is cracking because everything is happening at once — the energy shock amplifying the debt problem, the debt problem constraining the Fed, the constrained Fed unable to support the labor market, the weak labor market dragging down housing, and all of it circling back to an inflation dynamic that has no simple policy answer.",
    "tts_duration": 25.0,
    "clips": [
        clip(257, "Overhead tracking shot of a massive dam with water pouring through multiple breach points simultaneously, each stream feeding into the next, the cascading failures interconnected and accelerating, cold blue-grey water against cold grey concrete, structural failure happening in real time", COLD),
        clip(257, "Close-up of an intricate clockwork mechanism where gears of different sizes are interlocked, one gear jamming and the seizure propagating through every connected gear in sequence, springs popping loose, cold clinical lighting revealing each mechanical failure as it chains to the next", COLD),
        clip(257, "Tracking shot of a line of books standing upright on a shelf beginning to topple in sequence like dominoes, each falling book knocking the next, the camera following the chain reaction around a corner where more books wait, cold library light, the cascade inevitable and self-reinforcing", COLD),
    ]
})

# ============================================================
# TRANSITION — COLD TO WARM
# ============================================================

# seg20_transition (9.5s) → 1×257 = 10.7s
segments.append({
    "id": "seg20_transition",
    "act": "transition",
    "narration": "In an environment like this, capital does not sit still. It moves. And right now, it is moving in two very specific directions.",
    "tts_duration": 9.5,
    "clips": [
        clip(257, "Slow tracking shot of water finding its way through cold blue-grey rock formations, seeping into crevices and emerging as a flowing stream that catches the first warm rays of sunrise breaking through clouds, the water transitioning from cold steel reflection to warm golden shimmer as it moves toward light", "color shifting from cold blue-grey to warm golden, dawn breaking through overcast"),
    ]
})

# ============================================================
# ACT 3 — WARM GOLD RETURNING, SUNRISE TONES
# ============================================================

# seg21 (56.6s) → 5×257 + 1×105 = 57.9s
segments.append({
    "id": "seg21",
    "act": 3,
    "narration": "The first is hard assets. Gold has reached fifty-two hundred dollars an ounce. But the price is not the story. The story is in the earnings. Most gold mining companies reported their latest results based on gold at forty-one hundred — because there is a lag between spot prices and reported financials. At fifty-two hundred, with largely fixed production costs, the operating leverage is extraordinary. Miners are generating cash at rates the market has not yet priced. Some are beginning share buybacks — the same capital return strategy that powered the technology mega-caps for years. Gold supply itself is declining even as prices rise, due to geological depletion. Central banks are buying at the fastest pace in modern history. The supply is shrinking. The demand is structural. And the repricing in mining equities has barely begun.",
    "tts_duration": 56.6,
    "clips": [
        clip(257, "Macro close-up of molten gold being poured from a crucible into a bar mold, the liquid metal flowing like thick honey, warm amber light radiating from its surface, a goldsmith's tongs steadying the pour, sparks and heat shimmer rising, the glow illuminating the entire frame", SUNRISE),
        clip(257, "Tracking shot through an underground gold mine where a miner operates a pneumatic drill against a rock face, gold-bearing quartz veins visible in the warm headlamp light, rock dust swirling like golden particles, the physical labor of extraction made vivid and tactile", SUNRISE),
        clip(257, "Over-shoulder shot of a central bank vault worker carefully stacking newly cast gold bars onto a reinforced pallet, each bar placed with precision, gloved hands adjusting alignment, warm overhead spotlights making each bar glow like a small sun, the growing stack reflecting amber light onto the worker's face", SUNRISE),
        clip(257, "Dolly shot along a geological core sample being extracted from a drill pipe, the cylindrical rock core sliding out to reveal diminishing gold flecks, the earlier sections rich and golden, the deeper sections increasingly barren, warm laboratory light catching the mineral contrast, depletion made visible", SUNRISE),
        clip(257, "Close-up of a miner's hands weighing a gold nugget on a simple balance scale, the other side loaded with small counterweights, the scale tipping decisively toward the gold, warm sunrise light streaming through a window catching dust motes that look like gold flakes suspended in air", SUNRISE),
        clip(105, "Macro shot of a gold bar's surface catching warm sunrise light at a low angle, revealing the subtle texture and imperfections in the cast metal, the reflection slowly brightening as the light source rises, the bar seeming to come alive with warmth", SUNRISE),
    ]
})

# breathing04 → 1×105 = 4.4s
segments.append({
    "id": "breathing04",
    "act": 3,
    "narration": "",
    "tts_duration": 0,
    "clips": [
        clip(105, "Wide shot of an open-pit mine at sunrise, the terraced walls catching golden light tier by tier as the sun rises, long warm shadows retreating down the excavation, a moment of quiet industrial beauty", SUNRISE),
    ]
})

# seg22 (52.4s) → 5×257 = 53.5s
segments.append({
    "id": "seg22",
    "act": 3,
    "narration": "Silver has tripled in a year — from under thirty dollars to over eighty. But this is not twenty eleven. In twenty eleven, silver's spike was speculative momentum. This time, it is being pulled by two forces simultaneously: monetary demand, as investors seek alternatives to depreciating currencies, and industrial demand — from solar panels, from electric vehicles, and increasingly from artificial intelligence infrastructure. The United States has declared silver a critical mineral for national security. China controls seventy-three percent of global silver smelting. America is building its first domestic silver smelter in fifty years — an eight-billion-dollar joint venture backed by the Department of Defense. Hard assets are not a trade right now. They are a structural position.",
    "tts_duration": 52.4,
    "clips": [
        clip(257, "Close-up of molten silver being ladled from a furnace into casting molds, the bright white-hot metal transitioning to warm golden reflections as it cools, a smelter worker's protective visor reflecting the pour, sparks bouncing off leather apron, warm industrial amber light filling the foundry", SUNRISE),
        clip(257, "Tracking shot along a solar panel assembly line where robotic arms precisely solder silver paste onto photovoltaic cells, each connection point sparking briefly with golden light, the panels moving along conveyors in rhythmic succession, warm factory lighting giving a sense of industrial purpose and momentum", SUNRISE),
        clip(257, "POV shot of hands connecting silver-coated electrical contacts inside an EV battery module, the precision of the work visible in steady surgical movements, warm workspace lighting reflecting off the lustrous silver surfaces, other battery modules stacked in orderly rows behind, production at scale", SUNRISE),
        clip(257, "Dolly reveal of a massive construction site where steel beams and concrete foundations are being laid for a new smelting facility, cranes swinging materials into position, workers directing operations with sunrise light painting long golden shadows across raw earth, the scale of investment visible in every frame", SUNRISE),
        clip(257, "Overhead tracking shot of silver ingots being loaded by forklift onto armored transport vehicles, armed security visible at the perimeter, each ingot catching warm morning light as it moves from warehouse shadow into open air, the strategic value made tangible through the security apparatus", SUNRISE),
    ]
})

# seg23 (20.8s) → 2×257 + 1×73 = 24.4s
segments.append({
    "id": "seg23",
    "act": 3,
    "narration": "The second direction is digital infrastructure. Bitcoin's price is down. The chart has been a textbook midterm-year cycle — weakness into February, a brief rally in early March to seventy-three thousand five hundred, and then a fade. Analysts tracking prior cycles see no meaningful price move until the third or fourth quarter.",
    "tts_duration": 20.8,
    "clips": [
        clip(257, "Tracking shot through a massive Bitcoin mining facility, rows of ASIC machines running with blinking green and amber indicator lights, warm air exhaust creating visible heat distortion, a technician walking between racks checking connections, warm overhead sodium vapor lighting casting golden pools between the rows", SUNRISE),
        clip(257, "Close-up of a server rack's ventilation fans spinning at high speed, warm amber status lights reflecting off brushed aluminum housings, a hand reaching in to swap a component with practiced efficiency, the hum of computational power made physical through vibration and airflow", SUNRISE),
        clip(73, "Macro shot of fiber optic cables carrying pulsing golden light signals through transparent junction boxes, each pulse traveling at visible speed through the glass threads, warm amber light making the digital physical, the infrastructure of a parallel financial system made tangible", SUNRISE),
    ]
})

# seg24 (40.2s) → 4×257 = 42.8s
segments.append({
    "id": "seg24",
    "act": 3,
    "narration": "But beneath the price, something else is happening. Kraken has obtained a Federal Reserve master account — the first cryptocurrency firm in history to do so. The new CFTC chairman, who spent years defending crypto companies against regulatory assault, is now building statutory frameworks designed to survive future administrations regardless of their political direction. Michael Saylor's company is purchasing over two thousand Bitcoin daily — four times the rate at which new coins are mined. And the twentieth million Bitcoin has just been created. The remaining one million will take one hundred fourteen years to produce.",
    "tts_duration": 40.2,
    "clips": [
        clip(257, "Dolly-in on an official's hands placing a heavy embossed seal onto a formal document at a mahogany desk, the seal pressing down with ceremonial weight, warm golden light from a desk lamp catching the fresh impression in the wax, the formality of institutional legitimacy being conferred", SUNRISE),
        clip(257, "Over-shoulder shot of a regulator at a podium signing a thick legislative binder, multiple pens laid out in a row, each used for a few strokes then set aside as keepsakes, warm amber spotlight illuminating the signing ceremony, photographers' warm flashes in the background", SUNRISE),
        clip(257, "Tracking shot of a massive data center being constructed, workers welding server rack frames into position, sparks cascading like golden rain, electrical conduit being threaded through ceiling trays by teams working in coordinated rhythm, warm construction lighting creating a cathedral-like atmosphere", SUNRISE),
        clip(257, "Macro close-up of a mining rig's hash board with its final chip being soldered into place, the soldering iron tip touching the connection point with a brief golden spark, the board then being slotted into a chassis and powered on, warm indicator lights blooming to life one by one across the surface", SUNRISE),
    ]
})

# breathing05 → 1×105 = 4.4s
segments.append({
    "id": "breathing05",
    "act": 3,
    "narration": "",
    "tts_duration": 0,
    "clips": [
        clip(105, "Wide shot of a sunrise breaking over a mountain ridge, warm golden light spilling into a valley below, mist burning off in layers, the world waking up with possibility, the quality of light suggesting both ending and beginning", SUNRISE),
    ]
})

# seg25 (14.7s) → 1×257 + 2×73 = 16.7s
segments.append({
    "id": "seg25",
    "act": 3,
    "narration": "The rails are being built. Whether you choose to use them is a decision you will make later. But the option is being created now — and options have value, especially when the systems they exist alongside are showing strain.",
    "tts_duration": 14.7,
    "clips": [
        clip(257, "Tracking shot following freshly laid railroad tracks extending toward a warm golden horizon, the rails gleaming in sunrise light, a work crew visible in the middle distance tamping ballast into place, each worker's movements synchronized, the physical act of building infrastructure that will outlast the builders", SUNRISE),
        clip(73, "Close-up of a railroad switch lever being pulled from one position to another by a strong hand, the mechanism clicking into its new alignment with satisfying precision, warm sunrise light catching the polished steel lever, a path being chosen", SUNRISE),
        clip(73, "Macro shot of two railroad rails converging toward a vanishing point on the warm golden horizon, heat shimmer rising from the sun-warmed steel, the parallel lines appearing to meet in the distance, infinite possibility compressed into a single point of perspective", SUNRISE),
    ]
})

# seg26_closing (42.2s) → 4×257 + 1×73 = 45.8s
segments.append({
    "id": "seg26_closing",
    "act": 3,
    "narration": "An energy shock struck an economy that was already fragile — and the fragilities are feeding each other. Capital is flowing toward hard assets and toward new financial infrastructure, away from the assumptions that have guided investment for the last four decades. Bonds selling off during a war instead of rallying. Rate cuts signaling weakness instead of stimulus. An entire continent's energy strategy collapsing in a single week. This is not a correction. This is not a cycle. This is a regime change. And regime changes reward those who recognize them early.",
    "tts_duration": 42.2,
    "clips": [
        clip(257, "Sweeping crane shot rising from a turbulent ocean surface where waves crash against an old stone lighthouse, lifting to reveal calm golden waters stretching beyond, the camera continuing upward to show the full panoramic contrast between storm and stillness, warm sunrise tones painting the calm waters amber and gold", SUNRISE),
        clip(257, "Tracking shot of a river at dawn where the current splits around a massive boulder, the water dividing into two distinct channels flowing in different directions, golden light making one channel shimmer with warmth while the other remains in shadow, the divergence of paths made natural and inevitable", SUNRISE),
        clip(257, "Dolly shot following a compass needle that has settled firmly in a new direction, the brass instrument resting on an old navigation table beside rolled charts, warm sunrise light streaming through a ship's porthole illuminating the compass face, a new bearing established with certainty", SUNRISE),
        clip(257, "Over-shoulder shot of a figure standing at the prow of a ship moving toward a sunrise on open water, hands gripping the bow rail, the golden horizon expanding as the vessel gains speed, warm light intensifying on the figure's face and shoulders, forward momentum into uncertain but illuminated waters", SUNRISE),
        clip(73, "Final wide shot: a golden sunrise fully established over a calm sea, the light now complete and unwavering, a single ship's wake visible as a golden line across the water heading toward the horizon, warmth saturating every element of the frame, the journey underway", SUNRISE),
    ]
})

# ============================================================
# ASSEMBLE FINAL JSON
# ============================================================

script = {
    "style_string": STYLE,
    "negative_prompt": NEG,
    "segments": segments
}

# Validate durations
total_clips = 0
issues = []
for seg in segments:
    seg_clip_dur = sum(c["duration_est"] for c in seg["clips"])
    num_clips = len(seg["clips"])
    total_clips += num_clips
    
    if seg["tts_duration"] > 0:
        required = seg["tts_duration"] + 1.0
        if seg_clip_dur < required:
            issues.append(f"  WARN: {seg['id']} clips={seg_clip_dur:.1f}s < required={required:.1f}s (tts={seg['tts_duration']}s)")
    
    print(f"{seg['id']:20s}: {num_clips} clips, {seg_clip_dur:6.1f}s clip dur, {seg['tts_duration']:5.1f}s tts")

print(f"\nTotal clips: {total_clips}")
total_dur = sum(c["duration_est"] for seg in segments for c in seg["clips"])
print(f"Total clip duration: {total_dur:.1f}s ({total_dur/60:.1f} min)")
total_tts = sum(seg["tts_duration"] for seg in segments)
print(f"Total TTS duration: {total_tts:.1f}s ({total_tts/60:.1f} min)")

if issues:
    print("\nISSUES:")
    for i in issues:
        print(i)
else:
    print("\nAll segments have sufficient clip duration. ✓")

with open("/home/user/workspace/v4_script.json", "w") as f:
    json.dump(script, f, indent=2)
    
print("\nSaved to /home/user/workspace/v4_script.json")
