"""
Configuration for the Iran War Documentary Pipeline
Topic: The US-Iran War of 2026 — Strait of Hormuz, Global Energy Crisis, and the New World Order
"""

import os

# === TOPIC ===
DOCUMENTARY_TOPIC = "The US-Iran War of 2026"
DOCUMENTARY_SUBTITLE = "Strait of Hormuz, Global Energy Crisis, and the New World Order"
DATE_RANGE_START = "2026-02-25"  # A few days before the war started (Feb 28)
DATE_RANGE_END = "2026-03-18"    # Today

# === API KEYS ===
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")  # via pipedream connector
APIFY_API_KEY = os.environ.get("APIFY_API_KEY", "${APIFY_API_KEY}")
VAST_API_KEY = os.environ.get("VAST_API_KEY", "${VAST_API_KEY}")
HF_TOKEN = os.environ.get("HF_TOKEN", "${HF_TOKEN}")
B2_KEY_ID = os.environ.get("B2_KEY_ID", "${B2_KEY_ID}")
B2_APP_KEY = os.environ.get("B2_APP_KEY", "${B2_APP_KEY}")

# === STAGING OPEN WEBUI ===
STAGING_URL = "https://staging.deep-search.uk"
STAGING_LOCAL_URL = "http://localhost:3001"  # when running on the VM directly
STAGING_ADMIN_EMAIL = "orpington.close@gmail.com"
STAGING_ADMIN_PASSWORD = "${STAGING_PASSWORD}"

# === VM ===
STAGING_VM_SSH = "ssh -o StrictHostKeyChecking=no -i ~/.ssh/vast_v3 -p 18770 root@ssh5.vast.ai"
RTX5090_VM_ID = 33006689

# === SEARCH QUERIES ===
# These will be used to discover YouTube content about the war
SEARCH_QUERIES = [
    "US Iran war 2026",
    "Strait of Hormuz blockade",
    "Iran war oil crisis",
    "Israel Lebanon ground invasion 2026",
    "Hormuz oil prices economic impact",
    "Iran missile attacks Gulf states",
    "NATO Hormuz coalition",
    "Iran war global recession",
    "Khamenei assassination US Israel strikes",
    "Iran nuclear sites bombing 2026",
    "Hezbollah Israel war Lebanon 2026",
    "Iran war civilian casualties",
    "oil price shock 2026 inflation",
    "Middle East war escalation",
    "Iran retaliatory strikes",
    "Gulf states Iran drone attacks",
    "US troops casualties Iran",
    "Hormuz shipping blockade energy crisis",
    "Iran war anti-war protests",
    "Joe Kent resignation counterterrorism",
]

# === CHANNELS TO MONITOR ===
# News and analysis channels covering the conflict
SEED_CHANNELS = {
    # Major news
    "UCeY0bbntWzzVIaj2z3QigXg": "NBC News",
    "UCupvZG-5ko_eiXAupbDfxWw": "CNN",
    "UCBi2mrWuNuyYy4gbM6fU18Q": "ABC News",
    "UC16niRr50-MSBwiO3YDb3RA": "BBC News",
    "UCIRYBXDze5krPDzAEOxFGVA": "Democracy Now!",
    "UCef1-8eOpJgud7szVPlZQAQ": "PBS NewsHour",
    "UCvAvFl2OGsuDSoOo93Fbi4A": "Al Jazeera English",
    "UCGy6uV7yqGWDeUWTZzT3ZEQ": "Global News",
    # Geopolitics & Military
    "UCwnKziETDbHJtx78nIkfYug": "CaspianReport",
    "UCBVjMGOIkavEAhyqpxJ73Dw": "Vox",
    "UCsXVk37bltHxD1rDPwtNM8Q": "Kurzgesagt",
    "UC2C_jShtL725hvbm1arSV9w": "CGP Grey",
    "UCV_8wfBOTP0NS_0-KFPs7Zg": "Task & Purpose",
    "UC4QZ_LsYcvcq7qOsOhpAI4A": "Real Life Lore",
    # Finance/Economy
    "UCL_f53ZEJxp8TtlOkHwMV9Q": "Jordan Peterson",
    "UCZSiicaoIA9lFHe7E_gKB-w": "Heresy Financial",
    "UCfR0TBzh0CG7IUMoZJHEGNg": "Wealthion",
    "UCEmMJpfbU0_mbN6MiAVl9PQ": "Adam Taggart",
    "UCJpnbfZLwd3ZkgMOJFmyLSg": "Macro Voices",
    "UCVNkMBJR2ULjIizVJObbxbA": "Peter Zeihan",
    "UCY2ifv8iH1Dsgjrz-338X-A": "Rosenberg Research",
    "UCnpDurdOKk8YZHVLuxABEgg": "Soar Financially",
    "UCYmFpYOWSTEBgPDaRfK_eLw": "ITM Trading",
    # Think tanks & long-form analysis  
    "UCQfwfsi5VrQ8yKZ-UWmAEFg": "VICE News",
    "UCsT0YIqwnpJCM-mx7-gSA4Q": "TEDx Talks",
    "UCHd62-u_v4DvJ8TCFtpi4GA": "60 Minutes",
    "UCLRxQ-pkGDaUVMGSYFD7ePw": "Gravitas PLUS (WION)",
    "UC16niRr50-MSBwiO3YDb3RA": "BBC News",
}

# === OUTPUT ===
OUTPUT_DIR = "/home/user/workspace/iran-war-doc"
CORPUS_DIR = f"{OUTPUT_DIR}/corpus"
TRANSCRIPTS_DIR = f"{CORPUS_DIR}/transcripts"
COMMENTS_DIR = f"{CORPUS_DIR}/comments"
METADATA_DIR = f"{CORPUS_DIR}/metadata"
KNOWLEDGE_DIR = f"{OUTPUT_DIR}/knowledge"

# === DEEP SEARCH REFINEMENT ===
KNOWLEDGE_BASE_NAME = "Iran War 2026 Documentary Corpus"
REFINEMENT_MODEL = "mistral-large-thinking"  # The thinking model on staging

# Categories for knowledge refinement
NARRATIVE_CATEGORIES = [
    "military_operations",      # Strikes, ground ops, casualties
    "strait_of_hormuz",         # Blockade, shipping, energy disruption
    "economic_impact",          # Oil prices, inflation, recession risks
    "civilian_casualties",      # Human cost, displacement, refugee crisis
    "geopolitical_alliances",   # NATO response, Gulf states, China/Russia
    "domestic_politics",        # US anti-war movement, resignations, protests
    "israel_lebanon",           # Israeli ground invasion, Hezbollah
    "nuclear_dimension",        # Iran nuclear sites, escalation risks
    "information_war",          # Media coverage, propaganda, censorship
    "historical_context",       # Comparisons to past conflicts
]
