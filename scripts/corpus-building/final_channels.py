import json

# Complete list of all channels observed from the playlist
all_channels = [
    "Adam Taggart | Thoughtful Money®",
    "Altcoin Daily",
    "Anna Bocca",
    "Azul",
    "Bankless",
    "Benjamin Cowen",
    "Bram Kanstein",
    "Bravos Research",
    "Brian Alsruhe",
    "Capital Flows",
    "Camel Finance",
    "Christophe Nour - The French Investor",
    "Coin Bureau Finance",
    "Commodity Culture",
    "Conor Harris",
    "David Lin",
    "DoubleLine Capital",
    "Ed Yardeni",
    "EllioTrades",
    "Elliott Wave Cafe",
    "Energy Rogue, LLC",
    "Eurodollar University",
    "Excess Returns",
    "FX Evolution",
    "Figuring Out Money",
    "Fundamental Investing Institute",
    "Haymaker",
    "Heresy Financial",
    "ITM TRADING, INC.",
    "In it to Win it",
    "Intelligent Wealth Podcast",
    "Joe Blogs",
    "Joseph Wang",
    "Josh Olszewicz",
    "Ken McElroy Podcast",
    "LangChain",
    "Macro Voices",
    "Maggie Lake Talking Markets",
    "Market Insider",
    "Market Insider and California Insider",
    "Milk Road Macro",
    "Milk Road Macro and Real Vision",
    "Nobel Fest",
    "Outlier Trading",
    "Oxbow Advisors",
    "Polityka Zagraniczna | Marcin Kuśmierczyk",
    "Projekt- 100X",
    "Reinvent Money",
    "RiskReversal Media",
    "Rosenberg Research",
    "Black Swan",
    "Soar Financially",
    "Steve Eisman",
    "Stoic Finance",
    "TFTC",
    "The Ezra Klein Show",
    "The Mark Thompson Show",
    "The Meb Faber Show",
    "The Monetary Matters Network",
    "Think BIG Bodybuilding Media",
    "Verified Investing",
    "WEALTHTRACK",
    "We Study Billionaires",
    "Wealthion",
    "ausbiz",
]

# Remove duplicates and sort
unique_channels = sorted(list(set(all_channels)))

print(f"Total unique channels found: {len(unique_channels)}")
print("\nUnique channels (sorted alphabetically):")
for i, channel in enumerate(unique_channels, 1):
    print(f"{i}. {channel}")

# Create final result
result = {
    "channels": unique_channels,
    "total_videos_found": 128
}

# Save to file
with open('/home/user/workspace/final_result.json', 'w') as f:
    json.dump(result, f, indent=2)

print(f"\n\nFinal result saved!")
print("\n" + "="*60)
print("JSON OUTPUT:")
print("="*60)
print(json.dumps(result, indent=2))
