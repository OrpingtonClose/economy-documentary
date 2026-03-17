import json

# All channels observed from the screenshots
channels = [
    "Heresy Financial",
    "Polityka Zagraniczna | Marcin Kuśmierczyk",
    "Rosenberg Research",
    "Wealthion",
    "The Ezra Klein Show",
    "We Study Billionaires",
    "Adam Taggart | Thoughtful Money®",
    "ITM TRADING, INC.",
    "The Monetary Matters Network",
    "Ken McElroy Podcast",
    "Soar Financially",
    "Azul",
    "Stoic Finance",
    "Joe Blogs",
    "Stoic Finance",
    "Bram Kanstein",
    "Anna Bocca",
    "Maggie Lake Talking Markets",
    "EllioTrades",
    "Benjamin Cowen",
    "Altcoin Daily",
    "Eurodollar University",
    "Conor Harris",
    "Fundamental Investing Institute",
    "Projekt- 100X",
    "Oxbow Advisors",
    "Josh Olszewicz",
    "WEALTHTRACK",
    "Bankless",
    "Commodity Culture",
    "Market Insider and California Insider",
    "Ed Yardeni",
    "Nobel Fest",
    "Figuring Out Money",
    "Outlier Trading",
    "David Lin",
    "FX Evolution",
    "Bravos Research",
    "Haymaker",
    "Macro Voices",
    "DoubleLine Capital",
    "Excess Returns",
    "In it to Win it",
    "TFTC",
    "Capital Flows",
    "Intelligent Wealth Podcast",
    "Stoic Finance",
    "Steve Eisman",
    "ausbiz",
    "Joseph Wang",
    "The Meb Faber Show",
    "Verified Investing",
    "Milk Road Macro and Real Vision",
    "Brian Alsruhe",
]

# Remove duplicates by converting to set and back to sorted list
unique_channels = sorted(list(set(channels)))

print(f"Total unique channels found: {len(unique_channels)}")
print("\nUnique channels:")
for i, channel in enumerate(unique_channels, 1):
    print(f"{i}. {channel}")

# Save as JSON
result = {
    "channels": unique_channels,
    "total_videos_found": 128
}

with open('/home/user/workspace/channels_result.json', 'w') as f:
    json.dump(result, f, indent=2)

print(f"\n\nResult saved to channels_result.json")
print(json.dumps(result, indent=2))
