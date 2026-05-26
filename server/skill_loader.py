"""Progressive skill disclosure for documentary pipeline agents.

Inspired by pydantic-deepagents skills system:
https://vstorm-co.github.io/pydantic-deepagents/concepts/skills/

Three-tier disclosure:
1. Discovery — list skill names + descriptions (cheap, always loaded)
2. Loading — full skill instructions (loaded on demand into prompt)
3. Resources — additional files accessed individually (as needed)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any


_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_SKILLS_DIR = os.path.join(_SCRIPT_DIR, "skills")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    version: str
    tags: list[str]
    author: str
    content: str
    directory: str


_skill_cache: dict[str, Skill] = {}


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML-like frontmatter from SKILL.md content."""
    if not text.startswith("---"):
        return {}, text

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text

    front = parts[1].strip()
    content = parts[2].strip()
    meta: dict[str, Any] = {}

    current_key = ""
    current_list: list[str] = []
    in_list = False

    for line in front.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # List item
        if stripped.startswith("-"):
            item = stripped[1:].strip()
            if in_list:
                current_list.append(item)
            else:
                in_list = True
                current_list = [item]
            continue

        # Key: value
        if ":" in stripped:
            # Save previous list if any
            if in_list and current_key:
                meta[current_key] = current_list
                in_list = False
                current_list = []

            key, val = stripped.split(":", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            meta[key] = val
            current_key = key
            continue

    if in_list and current_key:
        meta[current_key] = current_list

    return meta, content


def discover_skills(skills_dir: str | None = None) -> list[Skill]:
    """Scan skills directory and return all discovered skills (discovery tier)."""
    skills_dir = skills_dir or _DEFAULT_SKILLS_DIR
    skills: list[Skill] = []

    if not os.path.isdir(skills_dir):
        return skills

    for entry in sorted(os.listdir(skills_dir)):
        skill_path = os.path.join(skills_dir, entry)
        skill_file = os.path.join(skill_path, "SKILL.md")
        if not os.path.isdir(skill_path) or not os.path.isfile(skill_file):
            continue

        try:
            with open(skill_file, encoding="utf-8") as f:
                raw = f.read()
            meta, content = _parse_frontmatter(raw)
            skills.append(
                Skill(
                    name=meta.get("name", entry),
                    description=meta.get("description", ""),
                    version=meta.get("version", "1.0.0"),
                    tags=meta.get("tags", []),
                    author=meta.get("author", ""),
                    content=content,
                    directory=skill_path,
                )
            )
        except Exception:
            continue

    return skills


def _ensure_cache(skills_dir: str | None = None) -> dict[str, Skill]:
    """Build or return the skill cache."""
    global _skill_cache
    if not _skill_cache:
        for skill in discover_skills(skills_dir):
            _skill_cache[skill.name] = skill
    return _skill_cache


def list_skills(agent_tag_filter: list[str] | None = None) -> str:
    """Return a formatted list of available skills for agent discovery.

    If agent_tag_filter is provided, only show skills matching at least one tag.
    """
    cache = _ensure_cache()
    lines = ["Available Skills (load any with LOAD_SKILL: <name>):"]

    for name, skill in sorted(cache.items()):
        if agent_tag_filter:
            if not any(tag in skill.tags for tag in agent_tag_filter):
                continue
        tag_str = ", ".join(skill.tags) if skill.tags else "general"
        lines.append(f"  - {name}: {skill.description} [{tag_str}]")

    if len(lines) == 1:
        lines.append("  (no skills match your role)")

    return "\n".join(lines)


def load_skill(name: str) -> str:
    """Load full instructions for a skill (loading tier).

    Returns the complete SKILL.md content body. If skill not found, returns
    a helpful error message.
    """
    cache = _ensure_cache()
    skill = cache.get(name)
    if not skill:
        available = ", ".join(sorted(cache.keys()))
        return f"Skill '{name}' not found. Available: {available}"
    return f"--- SKILL: {skill.name} v{skill.version} ---\n{skill.content}\n--- END SKILL ---"


def read_skill_resource(skill_name: str, resource_path: str) -> str:
    """Read a resource file from within a skill directory (resource tier).

    Blocks path traversal attempts for security.
    """
    cache = _ensure_cache()
    skill = cache.get(skill_name)
    if not skill:
        return f"Skill '{skill_name}' not found."

    # Block path traversal
    cleaned = resource_path.replace("..", "").lstrip("/")
    if not cleaned or cleaned.startswith("."):
        return f"Invalid resource path: {resource_path}"

    full_path = os.path.join(skill.directory, cleaned)
    real_skill_dir = os.path.realpath(skill.directory)
    real_resource = os.path.realpath(full_path)

    if not real_resource.startswith(real_skill_dir + os.sep) and real_resource != real_skill_dir:
        return f"Resource path escapes skill directory: {resource_path}"

    if not os.path.isfile(full_path):
        return f"Resource not found: {resource_path} in skill '{skill_name}'"

    try:
        with open(full_path, encoding="utf-8") as f:
            return f.read()
    except Exception as exc:
        return f"Error reading resource: {exc}"


def get_agent_skill_discovery(agent_id: str) -> str:
    """Get the discovery-tier skill listing tailored to an agent role.

    Each agent sees only skills relevant to their domain.
    """
    tag_map: dict[str, list[str]] = {
        "scenario": ["writing", "documentary", "script", "storytelling"],
        "audio": ["audio", "tts", "narration", "documentary"],
        "video": ["video", "ltx", "diffusion", "prompts", "documentary"],
        "assembly": ["editing", "ffmpeg", "assembly", "documentary"],
        "provisioner": ["provisioning", "vastai", "gpu", "devops"],
        "orchestrator": ["troubleshooting", "debugging", "pipeline", "orchestration"],
    }
    tags = tag_map.get(agent_id, [])
    discovery = list_skills(tags)

    # Add orchestrator universal access
    if agent_id == "orchestrator":
        all_skills = list_skills()
        if "Available Skills" in all_skills and all_skills != discovery:
            discovery += "\n\nAll pipeline skills (orchestrator access):\n"
            for line in all_skills.splitlines()[1:]:
                if line not in discovery:
                    discovery += line + "\n"

    return discovery


def get_skill_prompt_fragment(agent_id: str) -> str:
    """Build a prompt fragment that teaches the agent about skills + self-research.

    This is injected into every agent's system prompt.
    """
    discovery = get_agent_skill_discovery(agent_id)

    fragment = f"""## Progressive Skill System

You have access to specialized skills that contain deep domain knowledge. Skills use progressive disclosure — only summaries are shown now; full instructions load on demand.

{discovery}

### How to Use Skills
1. **Load a skill** by writing `LOAD_SKILL: <name>` in your response. The pipeline will inject the full skill instructions and ask you to continue.
2. **Read a resource** by writing `READ_RESOURCE: <skill_name>/<path>` for additional files within a skill.

### Self-Directed Research
You may conduct web research to inform your decisions:
- `RESEARCH: <query>` — Quick web search (Brave)
- `RESEARCH_DEEP: <query>` — Synthesized deep dive (Perplexity)
- `RESEARCH_NEWS: <query>` — Recent developments search (Exa)

Use research when:
- The brief involves unfamiliar subject matter
- A previous attempt failed and you need to understand why
- You need current facts, statistics, or technical specifications
- You want to verify a claim before including it in output

After loading a skill or receiving research results, integrate the knowledge into your work. Do not simply echo it back.
"""
    return fragment
