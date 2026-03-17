import json, os

base_dir = "/home/user/workspace/tool_calls/call_external_tool"

# Map of output file -> channel name (for fallback if channelTitle is missing)
channel_files = {
    "output_mmp7lysl.json": "Adam Taggart | Thoughtful Money",
    "output_mmp7lyvq.json": "Altcoin Daily",
    "output_mmp7lyec.json": "Anna Bocca",
    "output_mmp7lyfv.json": "Azul",
    "output_mmp7lykl.json": "Bankless",
    "output_mmp7mjkh.json": "Benjamin Cowen",
    "output_mmp7miuw.json": "Bram Kanstein",
    "output_mmp7miz1.json": "Coin Bureau Finance",
    "output_mmp7mj4s.json": "Commodity Culture",
    "output_mmp7mj6i.json": "Conor Harris",
    "output_mmp7mx7j.json": "David Lin",
    "output_mmp7mv7i.json": "Econ Lessons",
    "output_mmp7muxp.json": "Ed Yardeni",
    "output_mmp7musm.json": "EllioTrades",
    "output_mmp7muss.json": "Eurodollar University",
    "output_mmp7n7ti.json": "Fundamental Investing Institute",
    "output_mmp7n80r.json": "Heresy Financial",
    "output_mmp7n80y.json": "ITM TRADING, INC.",
    "output_mmp7n8mq.json": "Joe Blogs",
    "output_mmp7n7t4.json": "Josh Olszewicz",
    "output_mmp7q9n7.json": "Ken McElroy Podcast",
    "output_mmp7q9jo.json": "Maggie Lake Talking Markets",
    "output_mmp7q9h4.json": "Market Insider",
    "output_mmp7q9mj.json": "Nobel Fest",
    "output_mmp7q9n1.json": "Oxbow Advisors",
    "output_mmp7qwuf.json": "Polityka Zagraniczna",
    "output_mmp7ql18.json": "Projekt: 100X",
    "output_mmp7qkya.json": "Rosenberg Research",
    "output_mmp7ql64.json": "Soar Financially",
    "output_mmp7ql09.json": "Stoic Finance",
    "output_mmp7qwff.json": "The Ezra Klein Show",
    "output_mmp7qzn8.json": "The Mark Thompson Show",
    "output_mmp7qwc2.json": "The Meb Faber Show",
    "output_mmp7qyhu.json": "The Monetary Matters Network",
    "output_mmp7r7v1.json": "WEALTHTRACK",
    "output_mmp7ra7s.json": "We Study Billionaires",
    "output_mmp7r81w.json": "Wealthion",
}

all_videos = []
channel_counts = {}

for fname, fallback_channel in channel_files.items():
    fpath = os.path.join(base_dir, fname)
    if not os.path.exists(fpath):
        print(f"WARNING: File not found: {fpath}")
        continue
    
    with open(fpath) as f:
        data = json.load(f)
    
    # Handle nested result key
    if isinstance(data, dict) and "result" in data:
        items = data["result"]
    elif isinstance(data, list):
        items = data
    else:
        items = [data]
    
    # Handle further nesting: result might be a dict with items key
    if isinstance(items, dict):
        items = items.get("items", [])
    
    count = 0
    for item in items:
        snippet = item.get("snippet", {})
        # Try different paths for video_id
        video_id = (
            snippet.get("resourceId", {}).get("videoId") or
            item.get("contentDetails", {}).get("videoId") or
            item.get("id", {}).get("videoId") if isinstance(item.get("id"), dict) else None
        )
        
        # Get channel name - prefer snippet.channelTitle, fallback to our map
        channel_name = snippet.get("channelTitle") or fallback_channel
        
        title = snippet.get("title", "")
        published_at = snippet.get("publishedAt", "")
        
        if video_id and title != "Deleted video" and title != "Private video":
            all_videos.append({
                "video_id": video_id,
                "title": title,
                "published_at": published_at,
                "channel_name": channel_name
            })
            count += 1
    
    channel_counts[fallback_channel] = count
    print(f"  {fallback_channel}: {count} videos")

# Sort by published_at descending
all_videos.sort(key=lambda x: x.get("published_at") or "", reverse=True)

# Save
with open("/home/user/workspace/v5_videos.json", "w") as f:
    json.dump(all_videos, f, indent=2)

print(f"\nTotal videos: {len(all_videos)}")
print(f"\nTop 10 most recent:")
for v in all_videos[:10]:
    print(f"  [{v['published_at'][:10]}] {v['channel_name']}: {v['title'][:70]}")

print(f"\nTop 50 video IDs for transcript download:")
for v in all_videos[:50]:
    print(f"  {v['video_id']} | {v['published_at'][:10]} | {v['channel_name']}")
