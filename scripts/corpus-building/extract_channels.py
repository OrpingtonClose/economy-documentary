import json
import re

# This will be populated with data extracted from screenshots/page text
# For now, let's create a structure to track channels as we go

channels_found = set()

# From screenshots so far:
initial_channels = [
    "Heresy Financial",
    "Polityka Zagraniczna | Marcin Kuśmierczyk",
    "Rosenberg Research",
    "Wealthion",
    "The Ezra Klein Show",  # and 2 more - need to check what this means
    "We Study Billionaires",
    "Adam Taggart | Thoughtful Money®",
    "ITM TRADING, INC.",
    "The Monetary Matters Network",
    "Ken McElroy Podcast",
    "Soar Financially",
    "Bravos Research",
    "Haymaker",
    "Macro Voices",
    "DoubleLine Capital",
    "Excess Returns",
    "In it to Win it",
    "TFTC"
]

for channel in initial_channels:
    channels_found.add(channel)

print(f"Channels found so far: {len(channels_found)}")
print(json.dumps(sorted(list(channels_found)), indent=2))
