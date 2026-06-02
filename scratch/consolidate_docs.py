#!/usr/bin/env python3
import os
import re
import json
from pathlib import Path

VAULT_DIR = Path("/Users/orpington/Documents/economy-documentary-work/obsidian-vault")

MAPPING = {
    "00 - Index.md": ["00 - Index.md"],
    "01 - Philosophy and Topology.md": [
        "01 - Core Philosophy.md",
        "02 - System Topology.md",
        "19 - Discarded Propositions and Rationale.md"
    ],
    "02 - Event Store and Effect Schemas.md": [
        "03 - Effect Type Family Complete Schemas.md",
        "05 - Event Store.md",
        "A. Appendix EventStoreDB Migration Path.md"
    ],
    "03 - Timeline Projections.md": [
        "06 - Projections.md"
    ],
    "04 - Agent Architecture and Systems.md": [
        "04 - Rules as Prompt No State Machine No Rules Engine Code.md",
        "07 - Situation Types Agent Guidance.md",
        "08 - Agent Architecture pydantic-deep.md",
        "09 - Agents Per-Agent Implementations.md",
        "09.5 - Effect Parser Semantic Extraction Pipeline.md"
    ],
    "05 - Provisioning and GPU Infrastructure.md": [
        "07 - Agent Environment and Tools.md",
        "10 - Provisioner Agent.md",
        "11 - VM Worker.md"
    ],
    "06 - Data Flows, Config, and Structure.md": [
        "12 - Data Flows.md",
        "14 - Configuration.md",
        "15 - File Structure.md"
    ],
    "07 - Security, Traceability, and Auditing.md": [
        "13 - Security Model.md",
        "16 - Traceability and Observability.md",
        "17 - pydantic Ecosystem Deep Audit V7.1 Addendum.md"
    ],
    "08 - Testing, Concurrency, and Rollout.md": [
        "18 - Authoring Workflow for Quasi-Deterministic Agents.md",
        "21 - Unit Agent and Integration Tests.md",
        "22 - Concurrency and Timeouts Invariants.md"
    ],
    "09 - Glossary.md": [
        "20 - Glossary.md"
    ]
}

RENAME_MAP = {
    "00 - Index": "00 - Index",
    "01 - Core Philosophy": "01 - Philosophy and Topology",
    "02 - System Topology": "01 - Philosophy and Topology",
    "19 - Discarded Propositions and Rationale": "01 - Philosophy and Topology",
    "03 - Effect Type Family Complete Schemas": "02 - Event Store and Effect Schemas",
    "05 - Event Store": "02 - Event Store and Effect Schemas",
    "A. Appendix EventStoreDB Migration Path": "02 - Event Store and Effect Schemas",
    "06 - Projections": "03 - Timeline Projections",
    "04 - Rules as Prompt No State Machine No Rules Engine Code": "04 - Agent Architecture and Systems",
    "07 - Situation Types Agent Guidance": "04 - Agent Architecture and Systems",
    "08 - Agent Architecture pydantic-deep": "04 - Agent Architecture and Systems",
    "09 - Agents Per-Agent Implementations": "04 - Agent Architecture and Systems",
    "09.5 - Effect Parser Semantic Extraction Pipeline": "04 - Agent Architecture and Systems",
    "07 - Agent Environment and Tools": "05 - Provisioning and GPU Infrastructure",
    "10 - Provisioner Agent": "05 - Provisioning and GPU Infrastructure",
    "11 - VM Worker": "05 - Provisioning and GPU Infrastructure",
    "12 - Data Flows": "06 - Data Flows, Config, and Structure",
    "14 - Configuration": "06 - Data Flows, Config, and Structure",
    "15 - File Structure": "06 - Data Flows, Config, and Structure",
    "13 - Security Model": "07 - Security, Traceability, and Auditing",
    "16 - Traceability and Observability": "07 - Security, Traceability, and Auditing",
    "17 - pydantic Ecosystem Deep Audit V7.1 Addendum": "07 - Security, Traceability, and Auditing",
    "18 - Authoring Workflow for Quasi-Deterministic Agents": "08 - Testing, Concurrency, and Rollout",
    "21 - Unit Agent and Integration Tests": "08 - Testing, Concurrency, and Rollout",
    "22 - Concurrency and Timeouts Invariants": "08 - Testing, Concurrency, and Rollout",
    "20 - Glossary": "09 - Glossary"
}

def clean_content(content):
    # Strip leading/trailing whitespaces
    content = content.strip()
    # Strip standard yaml frontmatter blocks if present
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            content = parts[2].strip()
    return content

def get_frontmatter_info(content):
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                info = json.loads(parts[1].strip())
                return info
            except Exception:
                pass
    return {}

def rewrite_links(text):
    for old_name, new_name in RENAME_MAP.items():
        # Match [[old_name (followed by |, #, or ]])
        pattern = r'\[\[' + re.escape(old_name) + r'([\|#\]])'
        replacement = r'[[' + new_name + r'\1'
        text = re.sub(pattern, replacement, text)
    return text

def main():
    old_files_to_remove = set()
    for source_list in MAPPING.values():
        for f in source_list:
            old_files_to_remove.add(VAULT_DIR / f)

    # Dictionary to keep track of new files and their combined content
    new_files_content = {}

    for new_file, old_files in MAPPING.items():
        combined_parts = []
        tags_union = set()
        titles = []
        section = ""

        for old_file in old_files:
            old_path = VAULT_DIR / old_file
            if not old_path.exists():
                print(f"Warning: source file {old_file} not found!")
                continue
            
            with open(old_path, "r", encoding="utf-8") as f:
                content = f.read()

            info = get_frontmatter_info(content)
            if info.get("tags"):
                tags_union.update(info["tags"])
            if info.get("title"):
                titles.append(info["title"])
            if info.get("section") and not section:
                section = info["section"]

            cleaned = clean_content(content)
            
            # Clean up the breadcrumbs inside cleaned file e.g. "<- [[...]]"
            cleaned = re.sub(r'^<-\s+\[\[.*\]\]\s*(\|)?\s*(\[\[.*\]\])?\s*(\|)?\s*(\[\[.*\]\])?\s*->\n*', '', cleaned)
            cleaned = cleaned.strip()

            combined_parts.append(cleaned)

        # Build new frontmatter
        title_str = " & ".join(titles) if titles else new_file.replace(".md", "")
        # Remove numbers from title
        title_str = re.sub(r'^\d+\s*-\s*', '', title_str)
        
        new_frontmatter = {
            "title": title_str,
            "section": section,
            "tags": sorted(list(tags_union))
        }

        frontmatter_text = "---\n" + json.dumps(new_frontmatter, indent=2) + "\n---\n\n"
        
        # Build breadcrumbs for index and neighbors
        # We can dynamically generate prev/next links later or just have Index link
        if new_file != "00 - Index.md":
            breadcrumbs = "[[00 - Index|Index]]\n\n"
        else:
            breadcrumbs = ""

        body = "\n\n---\n\n".join(combined_parts)
        full_content = frontmatter_text + breadcrumbs + body
        
        # Rewrite links inside full_content
        full_content = rewrite_links(full_content)

        new_files_content[VAULT_DIR / new_file] = full_content

    # Now remove all old files first
    for old_path in old_files_to_remove:
        if old_path.exists():
            print(f"Removing old file: {old_path.name}")
            os.remove(old_path)

    # Write the new files
    for new_path, content in new_files_content.items():
        print(f"Writing new file: {new_path.name}")
        with open(new_path, "w", encoding="utf-8") as f:
            f.write(content)

    print("Consolidation complete!")

if __name__ == "__main__":
    main()
