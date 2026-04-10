"""
Scoring functions for enrichment pipeline.

Provides trust scoring for URLs and serendipity scoring for conditions.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse


# Domain trust tiers
_TIER1_DOMAINS = {
    "fred.stlouisfed.org", "bls.gov", "bea.gov", "census.gov",
    "federalreserve.gov", "treasury.gov", "sec.gov", "cbo.gov",
    "imf.org", "worldbank.org", "oecd.org", "nber.org",
    "brookings.edu", "piie.com",
}

_TIER2_DOMAINS = {
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com",
    "economist.com", "nature.com", "science.org",
    "arxiv.org", "ssrn.com", "jstor.org",
    "nytimes.com", "washingtonpost.com", "bbc.com",
}

_TIER3_DOMAINS = {
    "wikipedia.org", "investopedia.com", "statista.com",
    "tradingeconomics.com", "macrotrends.net",
}

_BLACKLIST_PATTERNS = [
    r"reddit\.com",
    r"twitter\.com",
    r"x\.com",
    r"facebook\.com",
    r"tiktok\.com",
    r"youtube\.com",
    r"medium\.com/.*",
]


def trust_score_url(url: str) -> float:
    """Score a URL's trustworthiness on a 0.0-1.0 scale.

    Tier 1 (government/institutional): 0.9
    Tier 2 (quality journalism/academic): 0.7
    Tier 3 (reference): 0.5
    Unknown: 0.3
    Blacklisted: 0.1
    """
    if not url:
        return 0.3

    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
    except Exception:
        return 0.2

    # Remove www prefix
    if domain.startswith("www."):
        domain = domain[4:]

    # Check blacklist
    for pattern in _BLACKLIST_PATTERNS:
        if re.search(pattern, url, re.I):
            return 0.1

    # Check tiers
    for t1 in _TIER1_DOMAINS:
        if domain.endswith(t1):
            return 0.9

    for t2 in _TIER2_DOMAINS:
        if domain.endswith(t2):
            return 0.7

    for t3 in _TIER3_DOMAINS:
        if domain.endswith(t3):
            return 0.5

    # .gov and .edu get a boost
    if domain.endswith(".gov"):
        return 0.85
    if domain.endswith(".edu"):
        return 0.65

    return 0.3


def serendipity_score(
    fact: str,
    existing_facts: list[str],
    domains: list[str],
) -> float:
    """Score how serendipitous (surprisingly relevant) a finding is.

    Higher scores for findings that:
    - Introduce new vocabulary not seen before
    - Connect to known domains in unexpected ways
    - Contain specific numbers or data points
    - Reference entities not previously mentioned
    """
    if not fact or not existing_facts:
        return 0.5

    fact_words = set(fact.lower().split())

    # Vocabulary novelty
    all_existing_words: set[str] = set()
    for ef in existing_facts:
        all_existing_words.update(ef.lower().split())

    novel_words = fact_words - all_existing_words
    novelty_ratio = len(novel_words) / max(len(fact_words), 1)

    # Specificity bonus (numbers, proper nouns)
    has_numbers = bool(re.search(r'\d+\.?\d*', fact))
    has_percentage = bool(re.search(r'\d+\.?\d*\s*%', fact))
    has_dollar = bool(re.search(r'\$[\d,]+', fact))

    specificity_bonus = 0.0
    if has_numbers:
        specificity_bonus += 0.1
    if has_percentage:
        specificity_bonus += 0.1
    if has_dollar:
        specificity_bonus += 0.1

    # Domain connection (does it touch known domains?)
    domain_connection = 0.0
    for domain in domains:
        if domain.lower() in fact.lower():
            domain_connection += 0.15

    score = (novelty_ratio * 0.5) + specificity_bonus + min(domain_connection, 0.3)
    return min(max(score, 0.0), 1.0)
