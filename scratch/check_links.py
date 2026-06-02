#!/usr/bin/env python3
import os
import re
from pathlib import Path

VAULT_DIR = Path("/Users/orpington/Documents/economy-documentary-work/obsidian-vault")

def main():
    md_files = {f.stem: f.name for f in VAULT_DIR.glob("*.md")}
    print(f"Existing markdown files: {list(md_files.keys())}")
    
    broken_links_found = 0
    total_links = 0
    
    for fpath in VAULT_DIR.glob("*.md"):
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read()
            
        # Find all [[link]] or [[link|label]] or [[link#anchor]]
        # We want to match anything inside [[ and ]] except newlines
        links = re.findall(r'\[\[([^\]\n]+)\]\]', content)
        
        for link in links:
            total_links += 1
            # Split off anchor (#) or label (|)
            # The structure is target#anchor|label or target|label or target#anchor
            target = link
            if "|" in target:
                target = target.split("|")[0]
            if "#" in target:
                target = target.split("#")[0]
                
            target = target.strip()
            
            # Target can be empty if it's an anchor within the same file (e.g. [[#5.1|Section 5.1]])
            if not target:
                continue
                
            if target not in md_files:
                print(f"Broken link in {fpath.name}: [[{link}]] (Target file '{target}' does not exist)")
                broken_links_found += 1
                
    print(f"\nScan complete. Total links checked: {total_links}. Broken links: {broken_links_found}")

if __name__ == "__main__":
    main()
