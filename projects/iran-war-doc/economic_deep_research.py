#!/usr/bin/env python3
"""
Deep economic research using Perplexity, Exa, and Tavily APIs.
Focused on finding unusual, non-obvious economic angles from the Iran war corpus.
"""

import json
import requests
import time
import os

# API Keys
PERPLEXITY_KEY = "PPLX_API_KEY"
EXA_KEY = "EXA_API_KEY"
TAVILY_KEY = "TAVILY_API_KEY"

results = {}

# ========== PERPLEXITY QUERIES ==========
# These are complex analytical queries that benefit from Perplexity's reasoning
perplexity_queries = [
    {
        "id": "war_cost_economics",
        "query": "In the March 2026 US-Iran war, the cost went from $891 million per day in the first 4 days to $1.5 billion per day by day 11. What was driving this cost escalation? What percentage came from interceptor missiles vs aircraft operations vs naval deployment? How does this compare to the Iraq war daily costs? Who are the defense contractors benefiting most?"
    },
    {
        "id": "russia_sanctions_oil_paradox",
        "query": "In March 2026, Trump eased sanctions on Russian oil specifically to lower gas prices during the Iran war. Analyze the economic paradox: the US is fighting Iran (Russia's ally) while simultaneously enriching Russia by lifting oil sanctions. How much additional revenue did Russia gain? What was the impact on Ukraine? How did European allies react economically?"
    },
    {
        "id": "china_hormuz_dependency",
        "query": "94% of China's oil imports pass through the Strait of Hormuz. During the March 2026 Iran war and Hormuz disruption, what happened to Chinese oil prices, manufacturing costs, and GDP growth? How did China respond economically? Did this accelerate their pivot away from Middle East oil? What was the tanker war risk premium and who profited from it?"
    },
    {
        "id": "gas_price_political_economy",
        "query": "US gas prices rose 70 cents per gallon during the March 2026 Iran war, with oil hitting $100/barrel. This coincided with spring break travel season and summer blend gasoline switchover. What was the total consumer cost impact? How did this affect Trump's approval ratings? What was the GDP impact of the Q4 2025 revision down to 0.7% combined with the war's energy shock? Who benefited economically - oil companies, refiners, speculators?"
    },
    {
        "id": "khamenei_money_laundering",
        "query": "Mojtaba Khamenei, who became Iran's new Supreme Leader in March 2026, reportedly held cryptocurrency holdings, London properties overlooking the Israeli embassy, a Dubai villa, and European hotel investments. Investigate: How did the Iranian regime's money laundering network operate through London real estate and crypto? Were these assets frozen during the war? What's the estimated total wealth of the Khamenei family hidden abroad?"
    },
    {
        "id": "interceptor_economics",
        "query": "During the March 2026 Iran war, the US burned through interceptor missiles at an unprecedented rate defending against Iranian ballistic missiles and drones. What is the cost per interceptor for SM-3, THAAD, and Patriot missiles? What was the total estimated expenditure on interceptors alone? How long would it take to replenish stocks? Which defense contractors (Raytheon/RTX, Lockheed Martin) saw stock price increases?"
    },
    {
        "id": "iran_oil_terminal_seizure",
        "query": "Reports in March 2026 suggested the US considered seizing Iran's Kharg Island oil terminal, described as 'Iran's lifeline for foreign currency.' What percentage of Iran's government revenue comes from Kharg Island oil exports? What would seizure mean for global oil markets? How did the threat alone affect oil futures? What are the historical parallels to resource seizure in warfare?"
    },
    {
        "id": "spr_release_economics",
        "query": "During the March 2026 Iran war oil price spike, the US coordinated an international Strategic Petroleum Reserve release described as a 'record amount.' How many barrels were released? Which countries participated? What was the price impact? How depleted is the US SPR now compared to historical levels? What are the long-term economic risks of SPR depletion?"
    }
]

print("=" * 80)
print("PERPLEXITY DEEP RESEARCH")
print("=" * 80)

for q in perplexity_queries:
    print(f"\n--- {q['id']} ---")
    try:
        resp = requests.post(
            "https://api.perplexity.ai/chat/completions",
            headers={
                "Authorization": f"Bearer {PERPLEXITY_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "sonar",
                "messages": [
                    {"role": "system", "content": "You are an economic analyst. Provide specific numbers, data points, and sourced facts. Focus on non-obvious economic connections and second-order effects. Be concise but data-rich."},
                    {"role": "user", "content": q["query"]}
                ],
                "max_tokens": 1500
            },
            timeout=60
        )
        if resp.status_code == 200:
            data = resp.json()
            answer = data["choices"][0]["message"]["content"]
            citations = data.get("citations", [])
            results[f"pplx_{q['id']}"] = {
                "answer": answer,
                "citations": citations
            }
            print(f"✓ Got answer ({len(answer)} chars, {len(citations)} citations)")
            # Print first 500 chars
            print(answer[:500])
        else:
            print(f"✗ HTTP {resp.status_code}: {resp.text[:200]}")
            results[f"pplx_{q['id']}"] = {"error": f"HTTP {resp.status_code}"}
        time.sleep(1)  # Rate limit
    except Exception as e:
        print(f"✗ Error: {e}")
        results[f"pplx_{q['id']}"] = {"error": str(e)}

# ========== EXA QUERIES ==========
# Exa is best for finding specific articles and sources
exa_queries = [
    {
        "id": "defense_stock_rally",
        "query": "defense contractor stock prices Raytheon Lockheed Martin Iran war March 2026 record profits"
    },
    {
        "id": "oil_speculation_profits",
        "query": "oil futures speculation profits Iran war Strait of Hormuz March 2026 traders hedge funds"
    },
    {
        "id": "global_supply_chain_impact",
        "query": "Iran war supply chain disruption shipping costs container rates March 2026 economic impact"
    },
    {
        "id": "crypto_iran_sanctions_evasion",
        "query": "Iran cryptocurrency sanctions evasion Khamenei family crypto holdings blockchain March 2026"
    },
    {
        "id": "us_gdp_war_impact",
        "query": "US GDP revised down Q4 2025 Iran war economic impact recession risk 2026"
    },
    {
        "id": "tanker_insurance_premiums",
        "query": "tanker war risk insurance premium Strait of Hormuz $4 million per transit March 2026"
    },
    {
        "id": "spring_break_gas_prices",
        "query": "spring break gas prices Iran war record travel costs consumer impact March 2026"
    },
    {
        "id": "iran_foreign_currency_crisis",
        "query": "Iran foreign currency reserves Kharg Island oil terminal economic collapse war 2026"
    },
    {
        "id": "weapons_production_meeting",
        "query": "Trump defense contractors meeting weapons production boost Iran war military industrial complex 2026"
    },
    {
        "id": "stryker_cyberattack",
        "query": "Stryker medical company cyberattack Iran proxy hackers March 2026"
    }
]

print("\n" + "=" * 80)
print("EXA RESEARCH")
print("=" * 80)

for q in exa_queries:
    print(f"\n--- {q['id']} ---")
    try:
        resp = requests.post(
            "https://api.exa.ai/search",
            headers={
                "x-api-key": EXA_KEY,
                "Content-Type": "application/json"
            },
            json={
                "query": q["query"],
                "type": "auto",
                "numResults": 5,
                "contents": {
                    "text": {"maxCharacters": 1000}
                }
            },
            timeout=30
        )
        if resp.status_code == 200:
            data = resp.json()
            articles = []
            for r in data.get("results", []):
                articles.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "text": r.get("text", "")[:500],
                    "publishedDate": r.get("publishedDate", "")
                })
            results[f"exa_{q['id']}"] = articles
            print(f"✓ Found {len(articles)} articles")
            for a in articles[:3]:
                print(f"  - [{a['title'][:80]}]({a['url'][:80]})")
        else:
            print(f"✗ HTTP {resp.status_code}: {resp.text[:200]}")
            results[f"exa_{q['id']}"] = {"error": f"HTTP {resp.status_code}"}
        time.sleep(0.5)
    except Exception as e:
        print(f"✗ Error: {e}")
        results[f"exa_{q['id']}"] = {"error": str(e)}

# ========== TAVILY QUERIES ==========
# Tavily for current news and fact-checking
tavily_queries = [
    {
        "id": "war_daily_cost_breakdown",
        "query": "US Iran war daily cost billion dollars breakdown missiles aircraft March 2026"
    },
    {
        "id": "russia_sanctions_lifted_oil",
        "query": "Trump lifted Russia oil sanctions March 2026 Iran war gas prices Ukraine impact"
    },
    {
        "id": "china_oil_hormuz_impact",
        "query": "China oil imports Strait of Hormuz disruption Iran war economic impact 2026"
    },
    {
        "id": "khamenei_wealth_london",
        "query": "Mojtaba Khamenei London properties crypto wealth money laundering 2026"
    },
    {
        "id": "spr_coordinated_release",
        "query": "Strategic Petroleum Reserve release Iran war coordinated international record 2026"
    },
    {
        "id": "mortgage_rates_war_impact",
        "query": "mortgage rates Iran war impact housing market GDP revised down 2026"
    },
    {
        "id": "defense_industry_windfall",
        "query": "defense industry profits Iran war Raytheon Lockheed weapons production 2026"
    },
    {
        "id": "global_recession_risk",
        "query": "Iran war global recession risk oil shock economic analysis March 2026"
    }
]

print("\n" + "=" * 80)
print("TAVILY RESEARCH")
print("=" * 80)

for q in tavily_queries:
    print(f"\n--- {q['id']} ---")
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            headers={"Content-Type": "application/json"},
            json={
                "api_key": TAVILY_KEY,
                "query": q["query"],
                "search_depth": "advanced",
                "max_results": 5,
                "include_answer": True
            },
            timeout=30
        )
        if resp.status_code == 200:
            data = resp.json()
            answer = data.get("answer", "")
            sources = []
            for r in data.get("results", []):
                sources.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", "")[:400]
                })
            results[f"tavily_{q['id']}"] = {
                "answer": answer,
                "sources": sources
            }
            print(f"✓ Answer ({len(answer)} chars), {len(sources)} sources")
            if answer:
                print(answer[:400])
        else:
            print(f"✗ HTTP {resp.status_code}: {resp.text[:200]}")
            results[f"tavily_{q['id']}"] = {"error": f"HTTP {resp.status_code}"}
        time.sleep(0.5)
    except Exception as e:
        print(f"✗ Error: {e}")
        results[f"tavily_{q['id']}"] = {"error": str(e)}

# Save all results
output_path = "/home/user/workspace/iran-war-doc/economic_deep_research.json"
with open(output_path, "w") as f:
    json.dump(results, f, indent=2)

print(f"\n\n{'='*80}")
print(f"SAVED: {output_path}")
print(f"Total results: {len(results)}")
print(f"{'='*80}")
