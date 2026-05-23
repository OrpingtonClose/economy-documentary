"""Research tools for the pipeline — web search + structured extraction.

The agent should never guess hardware requirements. It should RESEARCH them.
These tools use Brave Search, Exa, or Firecrawl to look up what a model
actually needs, then extract structured GPURequirements using instructor.

Architecture:
    Agent: "What GPU does Qwen3 TTS need?"
    → search_web("Qwen3 TTS GPU VRAM requirements")
    → extract(GPURequirements) from search results
    → text summary for agent reasoning
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

from models.gpu_requirements import GPURequirements, VastAIOfferMatch
from structured_extract import extract

logger = logging.getLogger(__name__)

_BRAVE_KEY = os.environ.get("BRAVE_API_KEY", "")
if not _BRAVE_KEY and os.path.exists(os.path.expanduser("~/api_keys/brave_key.txt")):
    with open(os.path.expanduser("~/api_keys/brave_key.txt")) as f:
        _BRAVE_KEY = f.read().strip()

_EXA_KEY = os.environ.get("EXA_API_KEY", "")
if not _EXA_KEY and os.path.exists(os.path.expanduser("~/api_keys/exa_api_key.txt")):
    with open(os.path.expanduser("~/api_keys/exa_api_key.txt")) as f:
        _EXA_KEY = f.read().strip()

_FIRECRAWL_KEY = os.environ.get("FIRECRAWL_API_KEY", "")
if not _FIRECRAWL_KEY and os.path.exists(os.path.expanduser("~/api_keys/firecrawl_api_key.txt")):
    with open(os.path.expanduser("~/api_keys/firecrawl_api_key.txt")) as f:
        _FIRECRAWL_KEY = f.read().strip()


# ---------------------------------------------------------------------------
# Web search backends
# ---------------------------------------------------------------------------

def _search_brave(query: str, count: int = 3) -> str:
    """Search Brave and return raw text results."""
    if not _BRAVE_KEY:
        return "Brave API key not available."
    url = f"https://api.search.brave.com/res/v1/web/search?q={urllib.parse.quote(query)}&count={count}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": _BRAVE_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("web", {}).get("results", [])
        lines = [f"Brave search: '{query}'"]
        for r in results:
            lines.append(f"\n--- {r.get('title', 'Untitled')} ---")
            lines.append(r.get("description", "No description"))
        return "\n".join(lines)
    except Exception as exc:
        return f"Brave search failed: {exc}"


def _search_exa(query: str, count: int = 3) -> str:
    """Search Exa and return raw text results."""
    if not _EXA_KEY:
        return "Exa API key not available."
    import json
    import urllib.request

    req = urllib.request.Request(
        "https://api.exa.ai/search",
        data=json.dumps({"query": query, "numResults": count}).encode(),
        headers={
            "Content-Type": "application/json",
            "x-api-key": _EXA_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("results", [])
        lines = [f"Exa search: '{query}'"]
        for r in results:
            lines.append(f"\n--- {r.get('title', 'Untitled')} ---")
            lines.append(r.get("text", "No text")[:500])
        return "\n".join(lines)
    except Exception as exc:
        return f"Exa search failed: {exc}"


# ---------------------------------------------------------------------------
# Public research API
# ---------------------------------------------------------------------------

def research_gpu_requirements(model_name: str) -> GPURequirements:
    """Research GPU requirements for a model using web search + extraction.

    This is the core research capability. The agent never guesses.
    It searches the web for authoritative requirements, then extracts
    structured data via instructor + DeepSeek v4-flash.
    """
    query = f"{model_name} GPU VRAM requirements minimum hardware"

    # Try multiple search backends, concatenate results
    raw_text = ""
    for search_fn in (_search_brave, _search_exa):
        result = search_fn(query, count=3)
        if "failed" not in result and "not available" not in result:
            raw_text += result + "\n\n"

    if not raw_text:
        # Fallback: return conservative defaults
        logger.warning("All search backends failed for %s", model_name)
        return GPURequirements(
            model_name=model_name,
            min_vram_gb=24.0,
            recommended_vram_gb=40.0,
            notes="Search failed — using conservative defaults. Agent should verify.",
        )

    return extract(
        GPURequirements,
        raw_text,
        system_prompt=(
            f"Extract GPU requirements for '{model_name}' from web search results. "
            "Be precise with VRAM numbers. If results conflict, use the most conservative (largest) number. "
            "If no specific GPU is mentioned, leave gpu_architecture empty. "
            "If no CUDA version is mentioned, leave it empty."
        ),
        temperature=0.0,
    )


def research_vastai_offers(requirements: GPURequirements, raw_offers_text: str) -> list[VastAIOfferMatch]:
    """Match Vast.ai offers against researched GPU requirements.

    The agent gets raw `vastai search offers` text. This function extracts
    which offers are suitable and ranks them.
    """
    from pydantic import BaseModel, Field

    class OfferList(BaseModel):
        offers: list[VastAIOfferMatch] = Field(default_factory=list)

    context = f"""GPU Requirements for {requirements.model_name}:
- Min VRAM: {requirements.min_vram_gb}GB
- Recommended VRAM: {requirements.recommended_vram_gb}GB
- Notes: {requirements.notes}

Vast.ai search results:
{raw_offers_text}"""

    result = extract(
        OfferList,
        context,
        system_prompt=(
            "Analyze Vast.ai offers against GPU requirements. "
            "suitable=True ONLY if offer VRAM >= minimum required. "
            "match_score should reflect price/performance (higher VRAM per dollar = higher score). "
            "concerns should list specific issues (e.g. 'T4 too slow', 'K80 too old'). "
            "recommendation: 'ideal' if >= recommended VRAM and good price, "
            "'acceptable' if meets minimum, 'marginal' if barely meets, 'reject' if below."
        ),
        temperature=0.0,
    )
    return result.offers


# ---------------------------------------------------------------------------
# Agent-facing tool wrappers
# ---------------------------------------------------------------------------

def research_model_requirements(model_name: str) -> str:
    """Plain-text wrapper for agent consumption.

    Returns a text summary the agent can reason over.
    """
    req = research_gpu_requirements(model_name)
    lines = [
        f"Research results for '{model_name}':",
        f"  Minimum VRAM: {req.min_vram_gb}GB",
        f"  Recommended VRAM: {req.recommended_vram_gb}GB",
    ]
    if req.gpu_architecture:
        lines.append(f"  GPU Architecture: {req.gpu_architecture}")
    if req.min_cuda_version:
        lines.append(f"  Min CUDA: {req.min_cuda_version}")
    if req.min_disk_gb:
        lines.append(f"  Min Disk: {req.min_disk_gb}GB")
    if req.estimated_boot_time_min:
        lines.append(f"  Est. Boot Time: {req.estimated_boot_time_min} min")
    if req.docker_image:
        lines.append(f"  Docker Image: {req.docker_image}")
    if req.worker_ready_signal:
        lines.append(f"  Worker Ready Signal: {req.worker_ready_signal}")
    if req.notes:
        lines.append(f"  Notes: {req.notes}")
    return "\n".join(lines)


def evaluate_vastai_offers(model_name: str, raw_offers_text: str) -> str:
    """Plain-text wrapper for agent consumption."""
    req = research_gpu_requirements(model_name)
    matches = research_vastai_offers(req, raw_offers_text)

    if not matches:
        return f"No offers evaluated for {model_name}. Raw offers text may be empty."

    suitable = [m for m in matches if m.suitable]
    lines = [f"Offer evaluation for {model_name} (needs {req.min_vram_gb}GB+ VRAM):"]

    if suitable:
        lines.append(f"\n✅ Suitable offers ({len(suitable)}):" )
        for m in sorted(suitable, key=lambda x: x.match_score, reverse=True)[:5]:
            lines.append(
                f"  {m.recommendation.upper()}: {m.offer_id} — "
                f"{m.gpu_name} ({m.vram_gb}GB) at ${m.price_per_hour}/hr "
                f"[score: {m.match_score:.2f}]"
            )
            if m.concerns:
                lines.append(f"    Concerns: {', '.join(m.concerns)}")
    else:
        lines.append("\n⚠️ NO SUITABLE OFFERS found. All offers are below minimum requirements.")

    rejected = [m for m in matches if not m.suitable]
    if rejected:
        lines.append(f"\nRejected ({len(rejected)}):")
        for m in rejected[:3]:
            lines.append(f"  {m.offer_id}: {m.gpu_name} ({m.vram_gb}GB) — {m.concerns[0] if m.concerns else 'unknown reason'}")

    return "\n".join(lines)
