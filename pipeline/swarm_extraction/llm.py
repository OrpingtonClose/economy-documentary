"""
LLM call layer — multi-provider with fallback chain.

Provider stack (non-Western first):
  1. MiniMax M2.7     — 200K ctx, OpenAI-compat, fast, primary reasoning
  2. DeepSeek V3      — 64K ctx, very cheap ($0.27/M in), general fallback
  3. DeepSeek R1      — 64K ctx, reasoning model, for hard verification
  4. Perplexity Sonar — grounded search with citations (tool-only)
  5. Gemini Flash     — free tier fallback (rate limited)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Optional

import httpx

log = logging.getLogger("enrichment")

# ── Shared HTTP client ────────────────────────────────────────
# One persistent client with connection pool limits prevents socket
# exhaustion when running many concurrent verification subagents.

_shared_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    """Return a shared, long-lived httpx.AsyncClient."""
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(180.0, connect=30.0),
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=60,
            ),
        )
    return _shared_client


async def close_client():
    """Close the shared client (call on shutdown)."""
    global _shared_client
    if _shared_client and not _shared_client.is_closed:
        await _shared_client.aclose()
        _shared_client = None


# ── API Keys ────────────────────────────────────────────────────

def _read_key(filename: str) -> str:
    """Read API key from environment variable.

    Env var name is derived from filename: 'minmax_api.txt' -> MINMAX_API
    """
    env_name = filename.replace(".txt", "").upper()
    return os.getenv(env_name, "")


MINIMAX_KEY = _read_key("minmax_api.txt")
DEEPSEEK_KEY = _read_key("deepseek_api.txt")
PERPLEXITY_KEY = _read_key("perplexity_api_key.txt")
GEMINI_KEY = _read_key("gemini_api_key.txt")
XAI_KEY = _read_key("grok_api.txt")
OPENROUTER_KEY = _read_key("openrouter_api.txt")
GROQ_KEY = _read_key("groq_api.txt")
MISTRAL_KEY = _read_key("mistral_api_key.txt")
OPENAI_KEY = _read_key("openai_api_key.txt")
ANTHROPIC_KEY = _read_key("anthropic_api.txt")


# ── Rate limiting ───────────────────────────────────────────────

_last_call_time: dict[str, float] = {}
_MIN_DELAY: dict[str, float] = {
    "minimax": 0.5,
    "deepseek": 0.3,
    "deepseek-r1": 1.0,
    "perplexity": 2.0,
    "gemini": 4.0,
    "xai": 0.3,
    "openrouter": 0.3,
    "groq": 0.2,
    "mistral": 0.5,
    "openai": 0.3,
    "anthropic": 0.5,
}


async def _rate_limit(provider: str):
    now = time.monotonic()
    last = _last_call_time.get(provider, 0)
    delay = _MIN_DELAY.get(provider, 1.0)
    elapsed = now - last
    if elapsed < delay:
        await asyncio.sleep(delay - elapsed)
    _last_call_time[provider] = time.monotonic()


# ── MiniMax M2.7 ───────────────────────────────────────────────

async def _call_minimax(
    messages: list[dict],
    model: str = "MiniMax-M2.7",
    max_tokens: int = 4096,
    temperature: float = 0.3,
    tools: list[dict] | None = None,
) -> dict:
    """MiniMax M2.7 — 200K context, OpenAI-compatible, interleaved thinking."""
    await _rate_limit("minimax")

    body: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    client = _get_client()
    resp = await client.post(
        "https://api.minimax.io/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {MINIMAX_KEY}",
            "Content-Type": "application/json",
        },
        json=body,
    )
    resp.raise_for_status()
    data = resp.json()

    choice = data["choices"][0]
    msg = choice.get("message", {})
    content = msg.get("content", "") or ""

    # Strip <think> tags, preserve thinking separately
    thinking = ""
    if "<think>" in content:
        think_match = re.search(r'<think>(.*?)</think>', content, re.DOTALL)
        if think_match:
            thinking = think_match.group(1).strip()
            content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()

    result: dict = {"content": content}
    if thinking:
        result["thinking"] = thinking
    if msg.get("tool_calls"):
        result["tool_calls"] = msg["tool_calls"]
    return result


# ── DeepSeek V3 / R1 ───────────────────────────────────────────

async def _call_deepseek(
    messages: list[dict],
    model: str = "deepseek-chat",
    max_tokens: int = 4096,
    temperature: float = 0.3,
    tools: list[dict] | None = None,
) -> dict:
    """DeepSeek V3 (chat) or R1 (reasoner) — 64K context, OpenAI-compatible."""
    provider_key = "deepseek-r1" if "reasoner" in model else "deepseek"
    await _rate_limit(provider_key)

    body: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools and "reasoner" not in model:  # R1 doesn't support tools
        body["tools"] = tools
        body["tool_choice"] = "auto"

    client = _get_client()
    resp = await client.post(
        "https://api.deepseek.com/chat/completions",
        headers={
            "Authorization": f"Bearer {DEEPSEEK_KEY}",
            "Content-Type": "application/json",
        },
        json=body,
    )
    resp.raise_for_status()
    data = resp.json()

    choice = data["choices"][0]
    msg = choice.get("message", {})
    content = msg.get("content", "") or ""

    # DeepSeek R1 uses reasoning_content field
    thinking = msg.get("reasoning_content", "")

    result: dict = {"content": content}
    if thinking:
        result["thinking"] = thinking
    if msg.get("tool_calls"):
        result["tool_calls"] = msg["tool_calls"]
    return result


# ── Perplexity Sonar ────────────────────────────────────────────

async def _call_perplexity(
    messages: list[dict],
    model: str = "sonar-pro",
    max_tokens: int = 2048,
    temperature: float = 0.3,
    tools: list[dict] | None = None,
) -> dict:
    """Perplexity Sonar Pro — grounded search with citations."""
    await _rate_limit("perplexity")

    client = _get_client()
    resp = await client.post(
        "https://api.perplexity.ai/chat/completions",
        headers={"Authorization": f"Bearer {PERPLEXITY_KEY}"},
        json={
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
    )
    resp.raise_for_status()
    data = resp.json()

    content = data["choices"][0]["message"]["content"]
    citations = data.get("citations", [])
    return {"content": content, "citations": citations}


# ── Gemini Flash ────────────────────────────────────────────────

async def _call_gemini(
    messages: list[dict],
    model: str = "gemini-2.5-flash",
    max_tokens: int = 4096,
    temperature: float = 0.3,
    tools: list[dict] | None = None,
) -> dict:
    """Gemini 2.5 Flash — free tier fallback."""
    await _rate_limit("gemini")

    contents = []
    system_instruction = None
    for msg in messages:
        if msg["role"] == "system":
            system_instruction = msg["content"]
        else:
            gemini_role = "user" if msg["role"] == "user" else "model"
            contents.append({"role": gemini_role, "parts": [{"text": msg["content"]}]})

    body: dict = {
        "contents": contents,
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if system_instruction:
        body["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_KEY}"
    client = _get_client()
    resp = await client.post(url, json=body)
    resp.raise_for_status()
    data = resp.json()

    candidates = data.get("candidates", [])
    if not candidates:
        return {"error": "No candidates", "content": ""}
    content = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
    return {"content": content}


# ── xAI / Grok ────────────────────────────────────────────────

async def _call_xai(
    messages: list[dict],
    model: str = "grok-3-fast",
    max_tokens: int = 4096,
    temperature: float = 0.3,
    tools: list[dict] | None = None,
) -> dict:
    """xAI Grok — uncensored, fast, tool-calling."""
    await _rate_limit("xai")
    body: dict = {
        "model": model, "messages": messages,
        "max_tokens": max_tokens, "temperature": temperature,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    client = _get_client()
    resp = await client.post(
        "https://api.x.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {XAI_KEY}", "Content-Type": "application/json"},
        json=body,
    )
    resp.raise_for_status()
    data = resp.json()

    choice = data["choices"][0]
    msg = choice.get("message", {})
    result: dict = {"content": msg.get("content", "") or ""}
    if msg.get("tool_calls"):
        result["tool_calls"] = msg["tool_calls"]
    return result


# ── OpenRouter ────────────────────────────────────────────────

async def _call_openrouter(
    messages: list[dict],
    model: str = "google/gemini-2.5-flash",
    max_tokens: int = 4096,
    temperature: float = 0.3,
    tools: list[dict] | None = None,
) -> dict:
    """OpenRouter — multi-model routing."""
    await _rate_limit("openrouter")
    body: dict = {
        "model": model, "messages": messages,
        "max_tokens": max_tokens, "temperature": temperature,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    client = _get_client()
    resp = await client.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
        json=body,
    )
    resp.raise_for_status()
    data = resp.json()

    choice = data["choices"][0]
    msg = choice.get("message", {})
    result: dict = {"content": msg.get("content", "") or ""}
    if msg.get("tool_calls"):
        result["tool_calls"] = msg["tool_calls"]
    return result


# ── Groq ──────────────────────────────────────────────────────

async def _call_groq(
    messages: list[dict],
    model: str = "llama-3.3-70b-versatile",
    max_tokens: int = 4096,
    temperature: float = 0.3,
    tools: list[dict] | None = None,
) -> dict:
    """Groq — ultra-fast inference."""
    await _rate_limit("groq")
    body: dict = {
        "model": model, "messages": messages,
        "max_tokens": max_tokens, "temperature": temperature,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    client = _get_client()
    resp = await client.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
        json=body,
    )
    resp.raise_for_status()
    data = resp.json()

    choice = data["choices"][0]
    msg = choice.get("message", {})
    result: dict = {"content": msg.get("content", "") or ""}
    if msg.get("tool_calls"):
        result["tool_calls"] = msg["tool_calls"]
    return result


# ── Mistral ───────────────────────────────────────────────────

async def _call_mistral(
    messages: list[dict],
    model: str = "mistral-large-latest",
    max_tokens: int = 4096,
    temperature: float = 0.3,
    tools: list[dict] | None = None,
) -> dict:
    """Mistral — strong reasoning, native tool calling."""
    await _rate_limit("mistral")
    body: dict = {
        "model": model, "messages": messages,
        "max_tokens": max_tokens, "temperature": temperature,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    client = _get_client()
    resp = await client.post(
        "https://api.mistral.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {MISTRAL_KEY}", "Content-Type": "application/json"},
        json=body,
    )
    resp.raise_for_status()
    data = resp.json()

    choice = data["choices"][0]
    msg = choice.get("message", {})
    result: dict = {"content": msg.get("content", "") or ""}
    if msg.get("tool_calls"):
        result["tool_calls"] = msg["tool_calls"]
    return result


# ── OpenAI ────────────────────────────────────────────────────

async def _call_openai(
    messages: list[dict],
    model: str = "gpt-4o",
    max_tokens: int = 4096,
    temperature: float = 0.3,
    tools: list[dict] | None = None,
) -> dict:
    """OpenAI — GPT-4o and variants."""
    await _rate_limit("openai")
    body: dict = {
        "model": model, "messages": messages,
        "max_tokens": max_tokens, "temperature": temperature,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"

    client = _get_client()
    resp = await client.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
        json=body,
    )
    resp.raise_for_status()
    data = resp.json()

    choice = data["choices"][0]
    msg = choice.get("message", {})
    result: dict = {"content": msg.get("content", "") or ""}
    if msg.get("tool_calls"):
        result["tool_calls"] = msg["tool_calls"]
    return result


# ── Anthropic ─────────────────────────────────────────────────

async def _call_anthropic(
    messages: list[dict],
    model: str = "claude-sonnet-4-20250514",
    max_tokens: int = 4096,
    temperature: float = 0.3,
    tools: list[dict] | None = None,
) -> dict:
    """Anthropic Claude — via Messages API."""
    await _rate_limit("anthropic")

    # Convert OpenAI format to Anthropic format
    system_text = ""
    api_messages = []
    for m in messages:
        if m["role"] == "system":
            system_text += m["content"] + "\n"
        elif m["role"] == "tool":
            api_messages.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""), "content": m["content"]}],
            })
        else:
            api_messages.append({"role": m["role"], "content": m["content"]})

    body: dict = {
        "model": model, "messages": api_messages,
        "max_tokens": max_tokens, "temperature": temperature,
    }
    if system_text:
        body["system"] = system_text.strip()
    if tools:
        body["tools"] = [
            {"name": t["function"]["name"], "description": t["function"]["description"],
             "input_schema": t["function"]["parameters"]}
            for t in tools
        ]

    client = _get_client()
    resp = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json=body,
    )
    resp.raise_for_status()
    data = resp.json()

    content = ""
    tool_calls = []
    for block in data.get("content", []):
        if block["type"] == "text":
            content += block["text"]
        elif block["type"] == "tool_use":
            tool_calls.append({
                "id": block["id"],
                "type": "function",
                "function": {"name": block["name"], "arguments": json.dumps(block["input"])},
            })

    result: dict = {"content": content}
    if tool_calls:
        result["tool_calls"] = tool_calls
    return result


# ── Unified call_llm ────────────────────────────────────────────

# Provider registry: name → (function, default_model)
_PROVIDERS = {
    "minimax":     (_call_minimax,    "MiniMax-M2.7"),
    "deepseek":    (_call_deepseek,   "deepseek-chat"),
    "deepseek-r1": (_call_deepseek,   "deepseek-reasoner"),
    "perplexity":  (_call_perplexity, "sonar-pro"),
    "gemini":      (_call_gemini,     "gemini-2.5-flash"),
    "xai":         (_call_xai,        "grok-3-fast"),
    "openrouter":  (_call_openrouter, "google/gemini-2.5-flash"),
    "groq":        (_call_groq,       "llama-3.3-70b-versatile"),
    "mistral":     (_call_mistral,    "mistral-large-latest"),
    "openai":      (_call_openai,     "gpt-4o"),
    "anthropic":   (_call_anthropic,  "claude-sonnet-4-20250514"),
}

# Fallback chain — deep and wide
_FALLBACK = {
    "minimax": "xai",
    "xai": "deepseek",
    "deepseek": "groq",
    "deepseek-r1": "mistral",
    "groq": "mistral",
    "mistral": "openrouter",
    "openrouter": "perplexity",
    "perplexity": "gemini",
    "openai": "anthropic",
    "anthropic": "openrouter",
    "gemini": None,
}


async def call_llm(
    messages: list[dict],
    req_id: str = "",
    *,
    provider: str = "minimax",
    model: str = "",
    max_tokens: int = 4096,
    temperature: float = 0.3,
    retries: int = 3,
    tools: list[dict] | None = None,
) -> dict:
    """Unified LLM call with retries and fallback chain.

    Default: MiniMax M2.7 → DeepSeek V3 → Perplexity → Gemini

    Pass tools= for native function calling (OpenAI format).
    MiniMax and DeepSeek V3 support it natively.
    """
    entry = _PROVIDERS.get(provider, _PROVIDERS["minimax"])
    func, default_model = entry
    resolved_model = model or default_model

    for attempt in range(retries):
        try:
            result = await func(
                messages, model=resolved_model,
                max_tokens=max_tokens, temperature=temperature,
                tools=tools,
            )
            return result
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 429:
                wait = min(30, 2 ** (attempt + 2))
                log.warning(f"[{req_id}] Rate limited ({provider}), waiting {wait}s")
                await asyncio.sleep(wait)
            elif status >= 500:
                wait = 2 ** (attempt + 1)
                log.warning(f"[{req_id}] Server error {status} ({provider}), retry {wait}s")
                await asyncio.sleep(wait)
            else:
                log.error(f"[{req_id}] HTTP {status} from {provider}: {e.response.text[:200]}")
                break  # Don't retry client errors, fall through to fallback
        except Exception as e:
            log.warning(f"[{req_id}] {provider} error (attempt {attempt+1}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(2 ** attempt)

    # Fallback to next provider
    next_provider = _FALLBACK.get(provider)
    if next_provider:
        log.info(f"[{req_id}] Falling back {provider} → {next_provider}")
        return await call_llm(
            messages, req_id, provider=next_provider,
            max_tokens=max_tokens, temperature=temperature,
            retries=retries, tools=tools,
        )

    return {"error": f"All providers exhausted (started at {provider})"}
