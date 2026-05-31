#!/usr/bin/env python3
"""Split ARCHITECTURE_V7.1.md into Obsidian vault notes."""

import re
import json
from pathlib import Path

SOURCE = Path("/Users/orpington/Documents/economy-documentary-work/ARCHITECTURE_V7.1.md")
VAULT = Path("/Users/orpington/Documents/economy-documentary-work/obsidian-vault")


def _link_section(m, sections, prefix=""):
    ref = m.group(1)
    for sec in sections:
        if sec["num"] == ref:
            return f"{prefix}[[{sec['wikilink']}|§{ref}]]"
    return m.group(0)


def _link_section_text(m, sections):
    ref = m.group(1)
    for sec in sections:
        if sec["num"] == ref:
            return f"see [[{sec['wikilink']}|§{ref}]]"
    return m.group(0)


def _link_bare_section(m, sections):
    ref = m.group(1)
    for sec in sections:
        if sec["num"] == ref:
            return f"[[{sec['wikilink']}|§{ref}]]"
    return m.group(0)


# Read source
content = SOURCE.read_text()
lines = content.splitlines(keepends=True)

# Preamble (before first ##)
preamble_lines = []
section_lines = []  # list of (heading, lines)
current_heading = None
current_lines = []
in_code_block = False

for line in lines:
    stripped = line.strip()
    if stripped.startswith("```"):
        in_code_block = not in_code_block

    if not in_code_block and stripped.startswith("## "):
        if current_heading is not None:
            section_lines.append((current_heading, current_lines))
        else:
            preamble_lines = current_lines
        current_heading = stripped[3:].strip()
        current_lines = [line]
    else:
        current_lines.append(line)

if current_heading is not None:
    section_lines.append((current_heading, current_lines))
else:
    preamble_lines = current_lines

# Build section metadata
sections = []
for heading, lines in section_lines:
    m = re.match(r"^([A-Z]?\d+(?:\.\d+)?)[\.:\-]?\s+(.+)$", heading)
    if m:
        num = m.group(1)
        title = m.group(2).strip()
    else:
        num = ""
        title = heading
    sections.append({
        "heading": heading,
        "num": num,
        "title": title,
        "lines": lines,
    })

# Build filenames with sortable prefixes
for sec in sections:
    num = sec["num"]
    title = sec["title"]
    clean = re.sub(r"[^\w\s\-\.]", "", title).strip()
    clean = re.sub(r"\s+", " ", clean)
    if num:
        if num.startswith("A"):
            sort_prefix = "99A"
            fname = f"{sort_prefix} - {clean}.md"
        elif "." in num:
            # Pad integer part for sorting: 9.5 -> 09.5
            parts = num.split(".")
            sort_num = f"{int(parts[0]):02d}.{'.'.join(parts[1:])}"
            fname = f"{sort_num} - {clean}.md"
        else:
            fname = f"{num.zfill(2)} - {clean}.md"
    else:
        fname = f"{clean}.md"
    sec["filename"] = fname
    sec["wikilink"] = fname.replace(".md", "")

# Clear vault and write each section
for f in VAULT.glob("*.md"):
    f.unlink()

for sec in sections:
    path = VAULT / sec["filename"]
    frontmatter = {
        "title": sec["title"],
        "section": sec["num"],
        "tags": ["architecture", "v7.1"],
    }
    fm_lines = ["---\n", json.dumps(frontmatter, indent=2) + "\n", "---\n", "\n"]

    idx = sections.index(sec)
    nav_links = []
    if idx > 0:
        nav_links.append(f"<- [[{sections[idx-1]['wikilink']}|{sections[idx-1]['title']}]]")
    nav_links.append("[[00 - Index|Index]]")
    if idx < len(sections) - 1:
        nav_links.append(f"[[{sections[idx+1]['wikilink']}|{sections[idx+1]['title']}]] ->")
    nav = " | ".join(nav_links)

    body = "".join(sec["lines"])
    body_lines = body.splitlines(keepends=True)
    if body_lines and body_lines[0].startswith("## "):
        body = "".join(body_lines[1:])

    # Add wiki-links for section references
    body = re.sub(r"\(§([A-Z]?\d+(?:\.\d+)*)\)", lambda m: _link_section(m, sections), body)
    body = re.sub(r"see §([A-Z]?\d+(?:\.\d+)*)", lambda m: _link_section_text(m, sections), body)
    body = re.sub(r"(?<![\w\[])§([A-Z]?\d+(?:\.\d+)*)", lambda m: _link_bare_section(m, sections), body)

    note = "".join(fm_lines) + nav + "\n\n# " + sec["title"] + "\n\n" + body
    path.write_text(note)
    print(f"Wrote {path.name}")

# Write Index - deduplicate the preamble heading
index_lines = ["---\n", '{"title": "Architecture V7.1 Index", "tags": ["architecture", "v7.1", "index"]}\n', "---\n", "\n"]
index_body = "".join(preamble_lines)
# Remove the leading # heading from preamble since we'll add our own
idx_body_lines = index_body.splitlines(keepends=True)
if idx_body_lines and idx_body_lines[0].startswith("# "):
    index_body = "".join(idx_body_lines[1:])

index_body = re.sub(r"\(§([A-Z]?\d+(?:\.\d+)*)\)", lambda m: _link_section(m, sections), index_body)
index_body = re.sub(r"see §([A-Z]?\d+(?:\.\d+)*)", lambda m: _link_section_text(m, sections), index_body)
index_body = re.sub(r"(?<![\w\[])§([A-Z]?\d+(?:\.\d+)*)", lambda m: _link_bare_section(m, sections), index_body)

index_content = "".join(index_lines)
index_content += "# Architecture V7.1 — Documentary Pipeline\n\n"
index_content += index_body + "\n\n"
index_content += "## Sections\n\n"
for sec in sections:
    index_content += f"- [[{sec['wikilink']}|{sec['heading']}]]\n"

(VAULT / "00 - Index.md").write_text(index_content)
print(f"Wrote 00 - Index.md")

print(f"\nVault created at: {VAULT}")
print(f"Total sections: {len(sections)}")
