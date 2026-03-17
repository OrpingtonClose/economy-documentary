"""
Build v5_knowledge.json: comprehensive knowledge base with all video metadata,
full transcripts (for 46 fetched), top comments (filtered: >30 words OR >5 likes),
and summary of key economic topics/narratives. Focus on March 1-13, 2026 events.
"""
import json
import os
import glob

WORKSPACE = "/home/user/workspace"
TRANSCRIPTS_DIR = f"{WORKSPACE}/v5_transcripts"
COMMENTS_DIR = f"{WORKSPACE}/v5_comments"
VIDEOS_JSON = f"{WORKSPACE}/v5_videos.json"

# Load all videos
with open(VIDEOS_JSON) as f:
    all_videos = json.load(f)

# Create a lookup by video_id
video_lookup = {v['video_id']: v for v in all_videos}

# ---------- LOAD TRANSCRIPTS ----------
transcripts = {}
txt_files = glob.glob(f"{TRANSCRIPTS_DIR}/**/*.txt", recursive=True)
for txt_path in txt_files:
    # Extract video_id from directory name
    vid_id = txt_path.split("/")[-2]
    with open(txt_path) as f:
        text = f.read().strip()
    if text:
        transcripts[vid_id] = text

print(f"Loaded {len(transcripts)} transcripts")

# ---------- LOAD COMMENTS ----------
comments_by_vid = {}
json_files = glob.glob(f"{COMMENTS_DIR}/*.json")
for jf in json_files:
    vid_id = os.path.basename(jf).replace('.json', '')
    try:
        with open(jf) as f:
            comments_raw = json.load(f)
        if not isinstance(comments_raw, list):
            continue
        # Filter: >30 words OR >5 likes
        filtered = []
        for c in comments_raw:
            text = c.get('text', '')
            likes = c.get('like_count', 0) or 0
            word_count = len(text.split())
            if word_count > 30 or likes > 5:
                filtered.append({
                    'text': text,
                    'like_count': likes,
                    'author': c.get('author', ''),
                    'timestamp': c.get('_time_text', '') or c.get('timestamp', ''),
                    'is_pinned': c.get('is_pinned', False)
                })
        # Sort by likes desc
        filtered.sort(key=lambda x: x['like_count'], reverse=True)
        comments_by_vid[vid_id] = filtered[:50]  # max 50 top comments per video
    except Exception as e:
        print(f"  Failed to load comments for {vid_id}: {e}")

print(f"Loaded comments for {len(comments_by_vid)} videos")
total_comments = sum(len(v) for v in comments_by_vid.values())
print(f"Total filtered comments: {total_comments}")

# ---------- BUILD KNOWLEDGE BASE ----------
knowledge_videos = []

for video in all_videos:
    vid_id = video['video_id']
    
    # Build the knowledge entry
    entry = {
        'video_id': vid_id,
        'title': video['title'],
        'published_at': video['published_at'],
        'channel_name': video['channel_name'],
        'url': f"https://www.youtube.com/watch?v={vid_id}",
        'has_transcript': vid_id in transcripts,
        'transcript_word_count': len(transcripts.get(vid_id, '').split()) if vid_id in transcripts else 0,
        'transcript': transcripts.get(vid_id, None),
        'has_comments': vid_id in comments_by_vid,
        'comments_count': len(comments_by_vid.get(vid_id, [])),
        'top_comments': comments_by_vid.get(vid_id, [])
    }
    
    knowledge_videos.append(entry)

# ---------- KEY ECONOMIC THEMES (March 1-13, 2026) ----------
key_themes = {
    "iran_war": {
        "title": "Iran War (started ~March 1, 2026)",
        "summary": "US and Israel launched strikes on Iran around March 1, 2026. Ayatollah Khamenei and senior IRGC command were killed. War is ongoing into its second week as of March 13. Iranian proxies and possible retaliation scenarios being analyzed. Ben Rhodes (Ezra Klein Show) calls it 'The Great Lie of War'. Ex-Trump officials asked to justify the war.",
        "key_videos": ["HOLUMfRLaI4", "O6fr0LTReoE", "le1VYgPadFA", "bz4n-Fu2RWE"],
        "channels": ["The Ezra Klein Show", "The Mark Thompson Show", "David Lin"]
    },
    "oil_shock": {
        "title": "Oil Shock / Strait of Hormuz Crisis",
        "summary": "Strait of Hormuz disruption: ship traffic reduced 94% (from 138 to 4 ships/day per Projekt:100X). WTI ~$80, Brent hit $115-120 (up 35%+). 20% of global LNG offline, LNG prices +137% in 5 days. QatarEnergy declared force majeure. '$100 oil the new normal?' (Maggie Lake). Oil reshaping US-China relationship (Gavekal's Charles Gave via Soar Financially).",
        "key_videos": ["9c2oA1alUNw", "A3Fz95j9Mv8", "dHdbC2PdGHw", "7XIAVZ-DRK4", "La3jtGhT75I", "Y7hHdoS9eU8", "FHoxbN0cIDw", "bA__Ckc1eJU", "tHVX_FQ04Lo", "ZDtdJQeaQ1M", "CqlpgQKTxpk", "dea4kKDA83U"],
        "channels": ["Maggie Lake Talking Markets", "Market Insider", "Soar Financially", "Projekt: 100X", "ITM TRADING, INC.", "Eurodollar University"]
    },
    "private_credit_crisis": {
        "title": "Private Credit / Liquidity Crisis",
        "summary": "Major private credit funds blocked redemptions: BlackRock $26B fund, Morgan Stanley $8B North Haven, Cliffwater $33B, Blue Owl, Blackstone $21B. $300B contagion risk identified by Stoic Finance. Spreading to UK economy ('Private Credit COLLAPSES British Economy'). Adam Taggart/Thoughtful Money featured Chris Irons discussing private credit meltdown threat.",
        "key_videos": ["e1kzj3AxoEg", "V5nh4ZI6PJY", "ng-eVor1wBM", "CqlpgQKTxpk"],
        "channels": ["Stoic Finance", "Adam Taggart | Thoughtful Money", "Eurodollar University"]
    },
    "fed_stagflation_bind": {
        "title": "Federal Reserve / Stagflation Dilemma",
        "summary": "The Fed is caught in a bind: rising oil prices from Iran war create inflation pressure while economy is weakening. Cannot cut rates (would fuel inflation) nor raise them (would worsen recession). K-shaped economy analysis: recession already for bottom 90%, wealth effect for top. Negative payroll signals. Rosenberg Research on K-shaped dynamics.",
        "key_videos": ["cV5UK0K3kXw", "ZDtdJQeaQ1M", "bA__Ckc1eJU", "4wob38gZ2yQ", "sdHfq4f_q2w"],
        "channels": ["The Monetary Matters Network", "ITM TRADING, INC.", "Soar Financially", "Rosenberg Research", "Econ Lessons"]
    },
    "gold_silver_surge": {
        "title": "Gold & Silver Surge",
        "summary": "Gold sounding hyperinflation alarms (ITM Trading). Silver hit $90; First Majestic Silver predicts $150-175. Incrementum/Ronald Stoeferle forecasts $5,200 gold. Multiple analysts say gold not contrarian anymore — mainstream allocation. David Lin: 'Gold About To Double Again as Financial Crisis Now Inevitable'.",
        "key_videos": ["G4OPjC4ev_w", "G1HNeez2v1g", "kxiCmu3gdRA", "a7L1w_Qo6fI"],
        "channels": ["Soar Financially", "ITM TRADING, INC.", "David Lin"]
    },
    "bitcoin_crypto": {
        "title": "Bitcoin ~$70K / Crypto as Canary",
        "summary": "Luke Gromen (via various channels): Bitcoin is the 'last functioning smoke alarm' for the global financial system. Polymarket sees $25M volume on crude oil price. EllioTrades: Iran Oil Crisis impact on Bitcoin. Arthur Hayes (Wealthion): Fed will always print money, bullish BTC. Bitcoin as liquidity cycle canary.",
        "key_videos": ["qNCL3aUAWbE", "gLHDU1ohRqo", "WluDH6yAiDM", "izcQcZpZNRY"],
        "channels": ["Bankless", "EllioTrades", "Wealthion", "Bankless"]
    },
    "us_china_geopolitics": {
        "title": "US-China / Global Geopolitical Reshaping",
        "summary": "Gavekal's Charles Gave (Soar Financially): Oil shock reshaping US-China relationship; China less vulnerable to Hormuz disruption due to pipeline access. Russia benefiting from Iran war as oil price windfall. Coin Bureau: 'The Only Winner in the Iran War is Unexpectedly Russia'. Global liquidity cycle: Michael Howell (CrossBorder Capital) says global liquidity peaked fall 2025, now turning down.",
        "key_videos": ["La3jtGhT75I", "t2UGsur2BOM", "pHAG8c6fFG4"],
        "channels": ["Soar Financially", "Coin Bureau Finance", "Bankless"]
    },
    "europe_energy_crisis": {
        "title": "Europe Energy Crisis",
        "summary": "20% of global LNG offline threatens European energy supply. QatarEnergy force majeure. UK economy under pressure from both private credit crisis and energy supply disruption. Potential deindustrialization scenarios.",
        "key_videos": ["V5nh4ZI6PJY", "e1kzj3AxoEg"],
        "channels": ["Stoic Finance"]
    },
    "k_shaped_economy_recession": {
        "title": "K-Shaped Economy / Recession Signals",
        "summary": "Rosenberg Research analysis: recession already underway for bottom 90% of US economy, only wealth effect keeping top afloat. Negative payrolls signal. Joseph Stiglitz (Monetary Matters): 'Economic Chaos Threatened By Middle East War'. Martin Wolf (FT, Monetary Matters): 'Could the Iran War Cause a New Oil Crisis?' — yes, with stagflation risk.",
        "key_videos": ["cV5UK0K3kXw", "4wob38gZ2yQ", "izcQcZpZNRY"],
        "channels": ["The Monetary Matters Network", "Rosenberg Research", "Bankless"]
    },
    "copper_commodities": {
        "title": "Copper & Industrial Commodities Supercycle",
        "summary": "Copper next to shock the market: 'Serious' deficit as demand skyrockets (Commodity Culture). Driven by AI energy super cycle, data center buildout, and electrification. Jeremy Schwartz (Wealthion): Copper and AI energy super cycle.",
        "key_videos": ["N9Mjk-GMyNM", "2zIuYj7W6gI"],
        "channels": ["Commodity Culture", "Wealthion"]
    }
}

# ---------- TOP DOCUMENTARY-RELEVANT VIDEOS ----------
documentary_picks = [
    {"video_id": "cV5UK0K3kXw", "title": "Could the Iran War Cause a New Oil Crisis? | FT's Chief Economics Commentator Martin Wolf", "channel": "The Monetary Matters Network", "reason": "FT's Martin Wolf on Iran war economics — authoritative voice"},
    {"video_id": "4wob38gZ2yQ", "title": "Economic Chaos Threatened By Middle East War | Joseph Stiglitz", "channel": "The Monetary Matters Network", "reason": "Nobel laureate Stiglitz on war economics"},
    {"video_id": "9c2oA1alUNw", "title": "Is $100 Oil the New Normal?", "channel": "Maggie Lake Talking Markets", "reason": "Market-level oil price analysis"},
    {"video_id": "A3Fz95j9Mv8", "title": "Oil Soars, Private Credit Shakes", "channel": "Maggie Lake Talking Markets", "reason": "Two crises intersection"},
    {"video_id": "dHdbC2PdGHw", "title": "A Closed Oil Route Could Hit Food Prices | Strait of Hormuz", "channel": "Market Insider", "reason": "Food security implications of Hormuz closure"},
    {"video_id": "7XIAVZ-DRK4", "title": "Is the War With Iran Already Changing the U.S. Economy?", "channel": "Market Insider", "reason": "Direct economic impact framing"},
    {"video_id": "V5nh4ZI6PJY", "title": "Private Credit COLLAPSES British Economy As Contagion Spreads GLOBALLY", "channel": "Stoic Finance", "reason": "Private credit crisis documentary evidence"},
    {"video_id": "e1kzj3AxoEg", "title": "BlackRock Just Triggered A $300 Billion Private Credit COLLAPSE", "channel": "Stoic Finance", "reason": "Specific crisis mechanics"},
    {"video_id": "La3jtGhT75I", "title": "Why Oil Could Reshape the US-China Relationship | Charles Gave", "channel": "Soar Financially", "reason": "Geopolitical oil/China analysis"},
    {"video_id": "G4OPjC4ev_w", "title": "GOLD Isn't Contrarian Anymore | Stoeferle on $5,200 Gold", "channel": "Soar Financially", "reason": "Gold super-cycle thesis"},
    {"video_id": "HOLUMfRLaI4", "title": "The Great Lie of War | Ben Rhodes", "channel": "The Ezra Klein Show", "reason": "Political context of Iran war"},
    {"video_id": "O6fr0LTReoE", "title": "Ezra Klein Asks a Former Trump Official to Justify This War", "channel": "The Ezra Klein Show", "reason": "Political accountability"},
    {"video_id": "Y7hHdoS9eU8", "title": "Der Iran-Krieg steht kurz davor, die globalen Märkte zu zerreißen", "channel": "Projekt: 100X", "reason": "Hormuz shipping data: 138 to 4 ships/day"},
    {"video_id": "1UyHDWCf8SY", "title": "War, Oil, and the Debt Spiral: How to Invest Through the Chaos", "channel": "Wealthion", "reason": "Comprehensive chaos overview"},
    {"video_id": "bA__Ckc1eJU", "title": "WARFLATION: Oil Shock + Debt Crisis Could Break the Economy", "channel": "Soar Financially", "reason": "Warflation concept — oil + debt + war"},
]

# ---------- STATISTICS ----------
stats = {
    "total_channels": 37,
    "total_videos_fetched": len(all_videos),
    "videos_with_transcripts": len(transcripts),
    "videos_with_comments": len(comments_by_vid),
    "total_filtered_comments": total_comments,
    "date_range": {
        "earliest": all_videos[-1]['published_at'][:10] if all_videos else None,
        "latest": all_videos[0]['published_at'][:10] if all_videos else None
    },
    "channels": sorted(list(set(v['channel_name'] for v in all_videos)))
}

# ---------- ASSEMBLE FINAL KNOWLEDGE JSON ----------
knowledge = {
    "metadata": {
        "pipeline_version": "v5",
        "generated_at": "2026-03-13",
        "description": "YouTube financial channel data pipeline. Source material for 1-hour economy documentary covering March 1-13, 2026 events.",
        "focus_period": "March 1-13, 2026",
        "stats": stats
    },
    "key_economic_themes": key_themes,
    "documentary_priority_videos": documentary_picks,
    "videos": knowledge_videos
}

# Save
output_path = f"{WORKSPACE}/v5_knowledge.json"
with open(output_path, 'w') as f:
    json.dump(knowledge, f, indent=2, ensure_ascii=False)

file_size = os.path.getsize(output_path)
print(f"\nv5_knowledge.json written: {file_size:,} bytes ({file_size/1024/1024:.1f} MB)")
print(f"Total videos in knowledge base: {len(knowledge_videos)}")
print(f"Videos with transcripts: {stats['videos_with_transcripts']}")
print(f"Videos with filtered comments: {stats['videos_with_comments']}")
print(f"Total filtered comments: {stats['total_filtered_comments']}")
print(f"Date range: {stats['date_range']['earliest']} to {stats['date_range']['latest']}")
