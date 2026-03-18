#!/usr/bin/env python3
"""
Deep Search Refinement Module
==============================
Uses staging.deep-search.uk (Open WebUI v0.8.8) to:
1. Create a Knowledge Base and upload the corpus
2. Use Mistral Large (Thinking) with RAG to analyze, score, and categorize each video
3. Extract key narratives, claims, and facts
4. Cross-reference and fact-check across sources
5. Build a refined, expanded knowledge base for scenario writing
6. Cluster content into narrative arcs suitable for documentary structure

API: Open WebUI REST API (JWT auth)
Model: mistral-large-thinking (via Mistral API)
"""

import json
import os
import sys
import time
import subprocess
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import *


class DeepSearchClient:
    """Client for staging.deep-search.uk Open WebUI API."""

    def __init__(self, base_url=None, use_ssh=True):
        self.use_ssh = use_ssh
        self.base_url = base_url or STAGING_LOCAL_URL
        self.token = None
        self.knowledge_base_id = None

    def _ssh_curl(self, method, endpoint, data=None, files=None, timeout=120):
        """Execute a curl command on the VM via SSH."""
        url = f"{self.base_url}{endpoint}"
        headers = []
        if self.token:
            headers.append(f'-H "Authorization: Bearer {self.token}"')

        if files:
            # File upload via SSH — need to transfer file first
            raise NotImplementedError("Use direct upload for files")

        if method == "GET":
            cmd = f'curl -s {" ".join(headers)} "{url}"'
        elif method == "POST":
            if data:
                json_str = json.dumps(data).replace("'", "'\\''")
                cmd = f"curl -s -X POST {' '.join(headers)} -H 'Content-Type: application/json' -d '{json_str}' \"{url}\""
            else:
                cmd = f'curl -s -X POST {" ".join(headers)} "{url}"'
        elif method == "DELETE":
            cmd = f'curl -s -X DELETE {" ".join(headers)} "{url}"'
        else:
            raise ValueError(f"Unknown method: {method}")

        ssh_cmd = f'{STAGING_VM_SSH} "{cmd}"'
        result = subprocess.run(ssh_cmd, shell=True, capture_output=True, text=True, timeout=timeout)

        if result.returncode != 0:
            print(f"  [SSH ERROR] {result.stderr[:200]}")
            return None

        # Strip SSH welcome banner
        output = result.stdout
        for prefix in ["Welcome to vast.ai", "Have fun!"]:
            if prefix in output:
                lines = output.split("\n")
                output = "\n".join(l for l in lines if prefix not in l)

        output = output.strip()
        if not output:
            return None

        try:
            return json.loads(output)
        except json.JSONDecodeError:
            # Might be streaming response
            return output

    def authenticate(self):
        """Sign in and get JWT token."""
        print("  Authenticating with staging portal...")
        result = self._ssh_curl("POST", "/api/v1/auths/signin", {
            "email": STAGING_ADMIN_EMAIL,
            "password": STAGING_ADMIN_PASSWORD,
        })
        if result and isinstance(result, dict) and "token" in result:
            self.token = result["token"]
            print(f"  Authenticated as {result.get('name', '?')} ({result.get('role', '?')})")
            return True
        print(f"  [ERROR] Authentication failed: {result}")
        return False

    def create_knowledge_base(self, name, description=""):
        """Create a new knowledge base."""
        print(f"  Creating knowledge base: {name}")
        result = self._ssh_curl("POST", "/api/v1/knowledge/create", {
            "name": name,
            "description": description,
        })
        if result and isinstance(result, dict):
            self.knowledge_base_id = result.get("id")
            print(f"  Created KB: {self.knowledge_base_id}")
            return self.knowledge_base_id
        print(f"  [ERROR] Failed to create KB: {result}")
        return None

    def upload_file_to_kb(self, kb_id, file_path, filename=None):
        """Upload a file to a knowledge base via SSH (transfer + curl)."""
        if not filename:
            filename = os.path.basename(file_path)

        print(f"    Uploading {filename} to KB...")

        # Transfer file to VM
        scp_cmd = f"scp -o StrictHostKeyChecking=no -i ~/.ssh/vast_v3 -P 18770 {file_path} root@ssh5.vast.ai:/tmp/{filename}"
        subprocess.run(scp_cmd, shell=True, capture_output=True, timeout=60)

        # Upload via curl on VM
        ssh_cmd = (
            f'{STAGING_VM_SSH} "'
            f"curl -s -X POST {self.base_url}/api/v1/files/ "
            f"-H 'Authorization: Bearer {self.token}' "
            f"-F 'file=@/tmp/{filename}' "
            f'"'
        )
        result = subprocess.run(ssh_cmd, shell=True, capture_output=True, text=True, timeout=120)
        output = result.stdout.strip()
        # Strip SSH banner
        for prefix in ["Welcome to vast.ai", "Have fun!"]:
            lines = output.split("\n")
            output = "\n".join(l for l in lines if prefix not in l).strip()

        try:
            file_data = json.loads(output)
            file_id = file_data.get("id")
            if file_id:
                # Add file to knowledge base
                add_result = self._ssh_curl("POST", f"/api/v1/knowledge/{kb_id}/file/add", {
                    "file_id": file_id,
                })
                if add_result:
                    print(f"    Uploaded and added to KB: {filename} (file_id: {file_id})")
                    return file_id
        except json.JSONDecodeError:
            pass

        print(f"    [ERROR] Upload failed for {filename}")
        return None

    def chat_completion(self, messages, model=None, knowledge_ids=None, stream=False, timeout=180):
        """
        Send a chat completion request, optionally with knowledge base RAG.
        Returns the full response text.
        """
        model = model or REFINEMENT_MODEL
        payload = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }

        # Add knowledge base context if specified
        if knowledge_ids:
            payload["knowledge"] = knowledge_ids

        result = self._ssh_curl("POST", "/api/chat/completions", payload, timeout=timeout)

        if stream or isinstance(result, str):
            # Parse streaming response
            text_parts = []
            for line in (result if isinstance(result, str) else "").split("\n"):
                line = line.strip()
                if line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        chunk = json.loads(line[6:])
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            text_parts.append(content)
                    except json.JSONDecodeError:
                        pass
            return "".join(text_parts)
        elif isinstance(result, dict):
            choices = result.get("choices", [])
            if choices:
                return choices[0].get("message", {}).get("content", "")
        return None

    def list_knowledge_bases(self):
        """List all knowledge bases."""
        result = self._ssh_curl("GET", "/api/v1/knowledge/")
        if result and isinstance(result, dict):
            return result.get("items", [])
        return []


# ─────────────────────────────────────────────
# REFINEMENT PIPELINE
# ─────────────────────────────────────────────

def prepare_corpus_files(corpus_path):
    """
    Split the corpus into digestible files for the knowledge base.
    Open WebUI works best with individual text files per topic.
    """
    print("\n" + "="*60)
    print("PREPARING CORPUS FOR KNOWLEDGE BASE")
    print("="*60)

    with open(corpus_path) as f:
        corpus = json.load(f)

    kb_files_dir = f"{KNOWLEDGE_DIR}/kb_files"
    os.makedirs(kb_files_dir, exist_ok=True)

    files_created = []

    for video in corpus:
        vid_id = video["video_id"]
        transcript = video.get("transcript", "")
        if not transcript or len(transcript) < 200:
            continue

        # Create a rich document combining metadata + transcript + comments
        doc_parts = [
            f"# {video.get('title', 'Untitled')}",
            f"Channel: {video.get('channel_name', 'Unknown')}",
            f"Published: {video.get('published_at', 'Unknown')}",
            f"Views: {video.get('view_count', 0):,} | Likes: {video.get('like_count', 0):,}",
            f"Duration: {video.get('duration', 'Unknown')}",
            f"URL: https://www.youtube.com/watch?v={vid_id}",
            "",
            "## Description",
            video.get("description", "")[:2000],
            "",
            "## Full Transcript",
            transcript[:80000],  # Cap per file
        ]

        # Add comments if available
        comments = video.get("comments", [])
        if comments:
            doc_parts.append("")
            doc_parts.append("## Top Audience Comments")
            for c in comments[:20]:
                likes = c.get("like_count", 0)
                doc_parts.append(f"- [{likes} likes] {c.get('text', '')[:500]}")

        doc_text = "\n".join(doc_parts)

        # Save as .md file (Open WebUI handles these well)
        safe_title = re.sub(r'[^\w\s-]', '', video.get("title", vid_id)[:60]).strip()
        filename = f"{vid_id}_{safe_title}.md"
        filepath = f"{kb_files_dir}/{filename}"

        with open(filepath, "w") as f:
            f.write(doc_text)

        files_created.append(filepath)

    print(f"  Created {len(files_created)} knowledge base files in {kb_files_dir}")
    return files_created


import re


def score_and_categorize(client, corpus_path):
    """
    Use the LLM to score each video's relevance and categorize it
    into narrative categories defined in config.
    """
    print("\n" + "="*60)
    print("SCORING & CATEGORIZING VIDEOS")
    print("="*60)

    with open(corpus_path) as f:
        corpus = json.load(f)

    # Only process videos with transcripts
    videos_with_transcripts = [v for v in corpus if v.get("transcript") or v.get("has_transcript")]
    print(f"  Processing {len(videos_with_transcripts)} videos with transcripts")

    scored = []
    categories_prompt = "\n".join(f"- {cat}" for cat in NARRATIVE_CATEGORIES)

    for i, video in enumerate(videos_with_transcripts):
        vid_id = video["video_id"]
        title = video.get("title", "?")
        transcript = video.get("transcript", "")[:15000]  # Send first 15k chars

        print(f"  [{i+1}/{len(videos_with_transcripts)}] Scoring: {title[:60]}...")

        prompt = f"""Analyze this YouTube video transcript for a documentary about the US-Iran War of 2026.

VIDEO: {title}
CHANNEL: {video.get('channel_name', '?')}
VIEWS: {video.get('view_count', 0):,}

TRANSCRIPT (first portion):
{transcript}

Please provide:
1. RELEVANCE_SCORE (1-10): How relevant is this to documenting the US-Iran War, Strait of Hormuz crisis, and global impact?
2. CATEGORIES: Which narrative categories does this video primarily cover? Choose from:
{categories_prompt}
3. KEY_CLAIMS: List 3-5 key factual claims or insights made in this video
4. KEY_QUOTES: Extract 2-3 powerful quotes that could be used in narration
5. NARRATIVE_VALUE: Brief assessment of what unique perspective or information this adds
6. EMOTIONAL_TONE: (analytical/alarming/hopeful/angry/somber/neutral)

Respond in JSON format:
{{"relevance_score": N, "categories": ["cat1", "cat2"], "key_claims": ["..."], "key_quotes": ["..."], "narrative_value": "...", "emotional_tone": "..."}}"""

        try:
            response = client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                stream=True,
                timeout=120
            )

            if response:
                # Try to parse JSON from response (might have markdown wrapping)
                json_match = re.search(r'\{[\s\S]*\}', response)
                if json_match:
                    try:
                        analysis = json.loads(json_match.group())
                        video["analysis"] = analysis
                        scored.append(video)
                        score = analysis.get("relevance_score", "?")
                        cats = ", ".join(analysis.get("categories", []))
                        print(f"    Score: {score}/10 | Categories: {cats}")
                    except json.JSONDecodeError:
                        print(f"    [WARN] Could not parse JSON from response")
                        video["analysis"] = {"raw_response": response[:500]}
                        scored.append(video)
                else:
                    print(f"    [WARN] No JSON in response")
                    video["analysis"] = {"raw_response": response[:500]}
                    scored.append(video)
        except Exception as e:
            print(f"    [ERROR] {str(e)[:80]}")

        time.sleep(2)  # Rate limiting for the Mistral API

    # Save scored corpus
    scored_file = f"{KNOWLEDGE_DIR}/scored_corpus.json"
    with open(scored_file, "w") as f:
        json.dump(scored, f, indent=2)

    print(f"\n  Scored {len(scored)} videos")
    print(f"  Saved to {scored_file}")
    return scored


def build_narrative_clusters(client, scored_corpus_path):
    """
    Use the LLM to cluster scored videos into narrative arcs
    and build the documentary structure.
    """
    print("\n" + "="*60)
    print("BUILDING NARRATIVE CLUSTERS")
    print("="*60)

    with open(scored_corpus_path) as f:
        scored = json.load(f)

    # Filter to high-relevance videos (score >= 6)
    high_relevance = [v for v in scored
                      if isinstance(v.get("analysis", {}).get("relevance_score"), (int, float))
                      and v["analysis"]["relevance_score"] >= 6]

    print(f"  High-relevance videos (score >= 6): {len(high_relevance)}")

    # Group by category
    category_groups = {}
    for v in high_relevance:
        cats = v.get("analysis", {}).get("categories", [])
        for cat in cats:
            if cat not in category_groups:
                category_groups[cat] = []
            category_groups[cat].append({
                "title": v.get("title", ""),
                "channel": v.get("channel_name", ""),
                "score": v.get("analysis", {}).get("relevance_score", 0),
                "key_claims": v.get("analysis", {}).get("key_claims", []),
                "key_quotes": v.get("analysis", {}).get("key_quotes", []),
                "narrative_value": v.get("analysis", {}).get("narrative_value", ""),
                "emotional_tone": v.get("analysis", {}).get("emotional_tone", ""),
                "views": v.get("view_count", 0),
                "video_id": v.get("video_id", ""),
            })

    # Sort each category by score
    for cat in category_groups:
        category_groups[cat].sort(key=lambda x: x["score"], reverse=True)

    # Save category groups
    with open(f"{KNOWLEDGE_DIR}/category_groups.json", "w") as f:
        json.dump(category_groups, f, indent=2)

    # Now ask the LLM to build narrative arcs
    categories_summary = []
    for cat, videos in category_groups.items():
        top_claims = []
        top_quotes = []
        for v in videos[:10]:
            top_claims.extend(v.get("key_claims", [])[:2])
            top_quotes.extend(v.get("key_quotes", [])[:1])
        categories_summary.append(f"""
### {cat} ({len(videos)} videos)
Key claims: {json.dumps(top_claims[:8])}
Key quotes: {json.dumps(top_quotes[:4])}
Top videos: {', '.join(v['title'][:40] for v in videos[:5])}
""")

    narrative_prompt = f"""You are building a documentary about the US-Iran War of 2026 and its global consequences.

Below is a summary of {len(high_relevance)} analyzed YouTube videos, grouped by category:

{''.join(categories_summary)}

Based on this body of evidence, design a documentary narrative structure:

1. TITLE: A compelling documentary title
2. THESIS: The central argument/thesis of the documentary (2-3 sentences)
3. NARRATIVE_ARCS: 5-8 major narrative arcs (acts/chapters), each with:
   - name: Arc title
   - description: What this arc covers (2-3 sentences)
   - categories: Which category groups feed into this arc
   - key_moments: 3-5 pivotal moments/claims to highlight
   - emotional_progression: How the emotional tone shifts through this arc
4. OPENING_HOOK: A powerful opening scene description
5. CLOSING_MESSAGE: The final message/call to action

Respond in JSON format."""

    print("  Generating narrative structure with Mistral Large (Thinking)...")
    response = client.chat_completion(
        messages=[{"role": "user", "content": narrative_prompt}],
        stream=True,
        timeout=300
    )

    narrative = None
    if response:
        json_match = re.search(r'\{[\s\S]*\}', response)
        if json_match:
            try:
                narrative = json.loads(json_match.group())
            except json.JSONDecodeError:
                narrative = {"raw_response": response}
        else:
            narrative = {"raw_response": response}

    # Save narrative structure
    narrative_file = f"{KNOWLEDGE_DIR}/narrative_structure.json"
    with open(narrative_file, "w") as f:
        json.dump(narrative, f, indent=2)
    print(f"  Saved narrative structure to {narrative_file}")

    return narrative


def expand_with_deep_search(client, narrative_path, scored_corpus_path):
    """
    Use the Deep Search portal to expand the knowledge base:
    1. For each narrative arc, identify knowledge gaps
    2. Generate additional research questions
    3. Use web search (SearXNG) through the portal to find answers
    4. Cross-reference claims across multiple sources
    """
    print("\n" + "="*60)
    print("EXPANDING KNOWLEDGE VIA DEEP SEARCH")
    print("="*60)

    with open(narrative_path) as f:
        narrative = json.load(f)

    with open(scored_corpus_path) as f:
        scored = json.load(f)

    arcs = narrative.get("NARRATIVE_ARCS") or narrative.get("narrative_arcs", [])
    if not arcs:
        print("  [ERROR] No narrative arcs found")
        return None

    expanded_knowledge = {}

    for arc in arcs:
        arc_name = arc.get("name", "Unknown Arc")
        print(f"\n  Arc: {arc_name}")

        # Ask LLM to identify gaps and generate research questions
        gap_prompt = f"""For a documentary arc titled "{arc_name}":
Description: {arc.get('description', '')}
Key moments: {json.dumps(arc.get('key_moments', []))}

What are 5 critical knowledge gaps that need to be filled with additional research?
For each gap, provide a specific web search query that would help fill it.

Respond in JSON: {{"gaps": [{{"topic": "...", "search_query": "...", "why_important": "..."}}]}}"""

        response = client.chat_completion(
            messages=[{"role": "user", "content": gap_prompt}],
            stream=True,
            timeout=120
        )

        if response:
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                try:
                    gaps = json.loads(json_match.group())
                    expanded_knowledge[arc_name] = {
                        "gaps": gaps.get("gaps", []),
                        "arc_info": arc,
                    }
                    for g in gaps.get("gaps", []):
                        print(f"    Gap: {g.get('topic', '?')}")
                        print(f"    Query: {g.get('search_query', '?')}")
                except json.JSONDecodeError:
                    expanded_knowledge[arc_name] = {"raw": response[:500]}

        time.sleep(2)

    # Save expanded knowledge
    expanded_file = f"{KNOWLEDGE_DIR}/expanded_knowledge.json"
    with open(expanded_file, "w") as f:
        json.dump(expanded_knowledge, f, indent=2)
    print(f"\n  Saved expanded knowledge to {expanded_file}")

    return expanded_knowledge


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("="*60)
    print("DEEP SEARCH REFINEMENT PIPELINE")
    print(f"Portal: {STAGING_URL}")
    print(f"Model: {REFINEMENT_MODEL}")
    print("="*60)

    client = DeepSearchClient()

    # Step 1: Authenticate
    if not client.authenticate():
        print("FATAL: Cannot authenticate with staging portal")
        sys.exit(1)

    corpus_path = f"{CORPUS_DIR}/full_corpus.json"
    if not os.path.exists(corpus_path):
        print(f"FATAL: Corpus not found at {corpus_path}")
        print("Run youtube_extract.py first")
        sys.exit(1)

    # Step 2: Prepare corpus files for KB
    kb_files = prepare_corpus_files(corpus_path)

    # Step 3: Create knowledge base and upload
    kb_id = client.create_knowledge_base(
        name=KNOWLEDGE_BASE_NAME,
        description=f"YouTube corpus for {DOCUMENTARY_TOPIC}. "
                    f"Date range: {DATE_RANGE_START} to {DATE_RANGE_END}. "
                    f"Contains transcripts, comments, and metadata."
    )

    if kb_id and kb_files:
        print(f"\n  Uploading {len(kb_files)} files to knowledge base...")
        for i, fpath in enumerate(kb_files[:50]):  # Upload top 50 files
            print(f"  [{i+1}/{min(len(kb_files), 50)}]", end=" ")
            client.upload_file_to_kb(kb_id, fpath)
            time.sleep(0.5)

    # Step 4: Score and categorize
    scored = score_and_categorize(client, corpus_path)

    # Step 5: Build narrative clusters
    scored_path = f"{KNOWLEDGE_DIR}/scored_corpus.json"
    narrative = build_narrative_clusters(client, scored_path)

    # Step 6: Expand with deep search
    narrative_path = f"{KNOWLEDGE_DIR}/narrative_structure.json"
    expanded = expand_with_deep_search(client, narrative_path, scored_path)

    print("\n" + "="*60)
    print("REFINEMENT COMPLETE")
    print("="*60)
    print(f"  Knowledge base ID: {kb_id}")
    print(f"  Scored corpus: {scored_path}")
    print(f"  Narrative structure: {narrative_path}")
    print(f"  Expanded knowledge: {KNOWLEDGE_DIR}/expanded_knowledge.json")
    print(f"\n  Next: Use the narrative structure to write the SCENARIO.MD")


if __name__ == "__main__":
    main()
