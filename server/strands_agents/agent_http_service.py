"""FastAPI surface for pipeline agents — HTTP base protocol.

Each agent runs as an independent HTTP service.
All endpoints speak free-flowing plain text (text/plain).

  GET  /  — inspect agent. Never interrupts running work.
  POST /  — process text as task, run multi-turn loop internally,
            return final prose result.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response

logger = logging.getLogger(__name__)


def _load_tools():
    """Lazy-load tool modules."""
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    _SERVER_DIR = os.path.dirname(_SCRIPT_DIR)
    if _SERVER_DIR not in sys.path:
        sys.path.insert(0, _SERVER_DIR)

    from search_tools import search_brave, search_exa, search_perplexity
    from skill_loader import load_skill, read_skill_resource
    return {
        "search_brave": search_brave,
        "search_exa": search_exa,
        "search_perplexity": search_perplexity,
        "load_skill": load_skill,
        "read_skill_resource": read_skill_resource,
    }


def _handle_skill_load(response: str, tools: dict) -> str | None:
    lines = response.splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("LOAD_SKILL:"):
            name = stripped.split(":", 1)[1].strip()
            return f"[SKILL LOADED: {name}]\n{tools['load_skill'](name)}"
        if stripped.upper().startswith("READ_RESOURCE:"):
            path = stripped.split(":", 1)[1].strip()
            if "/" in path:
                skill_name, resource = path.split("/", 1)
                return f"[RESOURCE: {path}]\n{tools['read_skill_resource'](skill_name, resource)}"
            return f"[ERROR] READ_RESOURCE format is '<skill_name>/<path>'. Got: {path}"
    return None


def _handle_research(response: str, tools: dict) -> str | None:
    lines = response.splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("RESEARCH_DEEP:"):
            query = stripped.split(":", 1)[1].strip()
            return f"[RESEARCH_DEEP result for '{query}']\n{tools['search_perplexity'](query, count=3)}"
        if stripped.upper().startswith("RESEARCH_NEWS:"):
            query = stripped.split(":", 1)[1].strip()
            return f"[RESEARCH_NEWS result for '{query}']\n{tools['search_exa'](query, count=3)}"
        if stripped.upper().startswith("RESEARCH:"):
            query = stripped.split(":", 1)[1].strip()
            return f"[RESEARCH result for '{query}']\n{tools['search_brave'](query, count=3)}"
    return None


def _handle_bash(response: str) -> str | None:
    import subprocess
    commands: list[str] = []
    in_bash = False
    for line in response.splitlines():
        stripped = line.strip()
        if stripped.startswith("```bash") or stripped.startswith("```sh"):
            in_bash = True
            continue
        if stripped == "```" and in_bash:
            in_bash = False
            continue
        if in_bash:
            commands.append(stripped)
        elif stripped.startswith("$ "):
            commands.append(stripped[2:])
    if not commands:
        return None
    results: list[str] = []
    for cmd in commands:
        if not cmd.strip():
            continue
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            results.append(
                f"$ {cmd}\nexit={result.returncode}\nstdout:\n{result.stdout[:2000]}\nstderr:\n{result.stderr[:1000]}"
            )
        except Exception as exc:
            results.append(f"$ {cmd}\nerror: {exc}")
    return "[BASH RESULTS]\n" + "\n---\n".join(results)


def build_agent_http_service(
    agent_id: str,
    system_prompt: str,
    client: Any,
    model: str = "deepseek-v4-flash",
) -> FastAPI:
    """Construct a lightweight HTTP service for a pipeline agent.

    The service receives prompts via POST, runs a multi-turn loop
    internally (skills, research, bash), and returns free-form prose.
    """
    app = FastAPI(title=f"agent-{agent_id}")

    _last_task: str = ""
    _last_result: str = ""
    _uptime_start: float = time.time()
    _call_count: int = 0

    @app.get("/")
    def _inspect() -> Response:
        uptime = time.time() - _uptime_start
        lines = [f"I am the {agent_id} agent."]
        if _last_task:
            lines.append(f"My last task was: {_last_task[:200]}")
        if _last_result:
            lines.append(f"My last result was: {_last_result[:200]}")
        lines.append(f"I have been running for {round(uptime, 1)} seconds.")
        lines.append(f"Total calls: {_call_count}")
        return Response(content="\n".join(lines), media_type="text/plain")

    @app.post("/")
    async def _invoke(request: Request) -> Response:
        nonlocal _last_task, _last_result, _call_count
        body = await request.body()
        text = body.decode("utf-8").strip()
        if not text:
            return Response(
                content="error: empty body",
                media_type="text/plain",
                status_code=400,
            )

        _last_task = text
        _call_count += 1
        logger.info("Agent '%s' received task: %s", agent_id, text[:80])

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ]

        tools = _load_tools()

        try:
            while True:
                result = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.7,
                )
                response = str(result.choices[0].message.content)

                skill_result = _handle_skill_load(response, tools)
                if skill_result:
                    logger.info("  [SKILL] Agent '%s' requested skill", agent_id)
                    messages.append({"role": "assistant", "content": response})
                    messages.append({
                        "role": "user",
                        "content": f"You requested additional information. Here is the result:\n\n{skill_result}\n\nNow continue with your task.",
                    })
                    continue

                research_result = _handle_research(response, tools)
                if research_result:
                    logger.info("  [RESEARCH] Agent '%s' requested research", agent_id)
                    messages.append({"role": "assistant", "content": response})
                    messages.append({
                        "role": "user",
                        "content": f"You requested research. Here are the results:\n\n{research_result}\n\nNow continue with your task.",
                    })
                    continue

                bash_result = _handle_bash(response)
                if bash_result:
                    logger.info("  [BASH] Agent '%s' requested bash", agent_id)
                    messages.append({"role": "assistant", "content": response})
                    messages.append({
                        "role": "user",
                        "content": f"You requested bash commands. Here are the results:\n\n{bash_result}\n\nNow continue with your task.",
                    })
                    continue

                _last_result = response
                logger.info("Agent '%s' completed. Result length: %d chars", agent_id, len(response))
                return Response(content=response, media_type="text/plain")

        except Exception as exc:
            logger.exception("Agent '%s' failed: %s", agent_id, exc)
            _last_result = f"error: {exc}"
            return Response(
                content=f"error: {exc}",
                media_type="text/plain",
                status_code=500,
            )

    return app
