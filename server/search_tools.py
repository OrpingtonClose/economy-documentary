"""Web search tools for all agents — Brave, Perplexity, Exa."""
from __future__ import annotations
import json
import os
import urllib.parse
import urllib.request

_BRAVE_KEY = ""
if not _BRAVE_KEY and os.path.exists(os.path.expanduser("~/api_keys/brave_key.txt")):
    with open(os.path.expanduser("~/api_keys/brave_key.txt")) as f:
        _BRAVE_KEY = f.read().strip()
if not _BRAVE_KEY and os.path.exists(os.path.expanduser("~/api_keys/LLMS/brave_key.txt")):
    with open(os.path.expanduser("~/api_keys/LLMS/brave_key.txt")) as f:
        _BRAVE_KEY = f.read().strip()

_PERPLEXITY_KEY = ""
if not _PERPLEXITY_KEY and os.path.exists(os.path.expanduser("~/api_keys/LLMS/perplexity_api_key.txt")):
    with open(os.path.expanduser("~/api_keys/LLMS/perplexity_api_key.txt")) as f:
        _PERPLEXITY_KEY = f.read().strip()
if not _PERPLEXITY_KEY and os.path.exists(os.path.expanduser("~/api_keys/perplexity_api_key.txt")):
    with open(os.path.expanduser("~/api_keys/perplexity_api_key.txt")) as f:
        _PERPLEXITY_KEY = f.read().strip()

_EXA_KEY = ""
if not _EXA_KEY and os.path.exists(os.path.expanduser("~/api_keys/exa_api_key.txt")):
    with open(os.path.expanduser("~/api_keys/exa_api_key.txt")) as f:
        _EXA_KEY = f.read().strip()
if not _EXA_KEY and os.path.exists(os.path.expanduser("~/api_keys/LLMS/exa_api_key.txt")):
    with open(os.path.expanduser("~/api_keys/LLMS/exa_api_key.txt")) as f:
        _EXA_KEY = f.read().strip()

def search_brave(query: str, count: int = 3) -> str:
    """Search the web using Brave Search."""
    if not _BRAVE_KEY:
        return "Brave API key not available."
    url = f"https://api.search.brave.com/res/v1/web/search?q={urllib.parse.quote(query)}&count={count}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "X-Subscription-Token": _BRAVE_KEY})
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("web", {}).get("results", [])
        lines = [f"Brave: '{query}'"]
        for r in results:
            lines.append(f"\n--- {r.get('title', 'Untitled')} ---")
            lines.append(r.get("description", "No description"))
        return "\n".join(lines)
    except Exception as exc:
        return f"Brave search failed: {exc}"

def search_perplexity(query: str, count: int = 3) -> str:
    """Search the web using Perplexity API."""
    if not _PERPLEXITY_KEY:
        return "Perplexity API key not available."
    req = urllib.request.Request(
        "https://api.perplexity.ai/chat/completions",
        data=json.dumps({
            "model": "sonar",
            "messages": [{"role": "user", "content": query}],
            "max_tokens": 1024,
        }).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_PERPLEXITY_KEY}"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        msg = data.get("choices", [{}])[0].get("message", {})
        lines = [f"Perplexity: '{query}'", msg.get("content", "No response")]
        citations = msg.get("citations", [])
        if citations:
            lines.append("\nCitations:")
            for c in citations[:count]:
                lines.append(f"  - {c}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Perplexity search failed: {exc}"

def search_exa(query: str, count: int = 3) -> str:
    """Search the web using Exa API."""
    if not _EXA_KEY:
        return "Exa API key not available."
    req = urllib.request.Request(
        "https://api.exa.ai/search",
        data=json.dumps({"query": query, "numResults": count}).encode(),
        headers={"Content-Type": "application/json", "x-api-key": _EXA_KEY},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("results", [])
        lines = [f"Exa: '{query}'"]
        for r in results:
            lines.append(f"\n--- {r.get('title', 'Untitled')} ---")
            lines.append(r.get("text", "No text")[:500])
        return "\n".join(lines)
    except Exception as exc:
        return f"Exa search failed: {exc}"
