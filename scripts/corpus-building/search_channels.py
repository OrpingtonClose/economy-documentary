import json

# Load known channels
with open('all_channels.json') as f:
    channels = json.load(f)

# Create a list of channel search commands (will use YouTube API connector)
channel_list = []
for ch_id, ch_name in channels.items():
    channel_list.append({"id": ch_id, "name": ch_name})

# Save for batch processing
with open('channels_to_search.json', 'w') as f:
    json.dump(channel_list, f, indent=2)

print(f"Prepared {len(channel_list)} channels for searching")

# Also prepare the missing channel names for browser-based search
all_browser = json.load(open('tool_calls/browser_task/output_mmm8r8ef.json'))['channels']
known_titles = set(channels.values())

# Normalize and find missing
missing = []
for name in all_browser:
    # Check if it matches any known title (with some fuzzy matching)
    found = False
    for kt in known_titles:
        if name.strip().lower() == kt.strip().lower():
            found = True
            break
    if not found:
        missing.append(name)

with open('missing_channels.json', 'w') as f:
    json.dump(missing, f, indent=2)
    
print(f"Missing channel IDs for: {len(missing)} channels")
for m in missing:
    print(f"  - {m}")
