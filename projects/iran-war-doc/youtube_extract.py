#!/usr/bin/env python3
"""
YouTube Extraction Module (SEPARATE from main pipeline)
========================================================
Handles all YouTube data collection via commercial APIs:
1. Channel discovery via YouTube Data API (search + channel listings)
2. Video discovery (recent uploads matching date range + search queries)
3. Transcript extraction (youtube-transcript-api, 3-tier fallback)
4. Comment extraction (Apify YouTube Comment Scraper)
5. Metadata assembly (video stats, channel info, engagement metrics)

All data is saved to /home/user/workspace/iran-war-doc/corpus/
"""

import json
import os
import sys
import time
import subprocess
import re
from datetime import datetime, timezone
from pathlib import Path

# Add parent dir
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import *

# Ensure output dirs exist
for d in [CORPUS_DIR, TRANSCRIPTS_DIR, COMMENTS_DIR, METADATA_DIR, KNOWLEDGE_DIR]:
    os.makedirs(d, exist_ok=True)


# ─────────────────────────────────────────────
# STEP 1: YouTube Data API Helper
# ─────────────────────────────────────────────

def call_youtube_api(tool_name, params):
    """Call YouTube Data API via the Pipedream connector using external-tool CLI."""
    tool_payload = {
        "source_id": "youtube_data_api__pipedream",
        "tool_name": tool_name,
        "arguments": params
    }
    result = subprocess.run(
        ["external-tool", "call", json.dumps(tool_payload)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  [ERROR] YouTube API call failed: {result.stderr[:200]}")
        return None
    try:
        data = json.loads(result.stdout)
        # The Pipedream connector returns results in various formats:
        # - Direct array of items (youtube search)
        # - Dict with 'items' key (standard YouTube API)
        # - Dict with nested JSON string in 'content'/'data'/'result'
        if isinstance(data, list):
            # Direct array — wrap in items format for consistency
            return {"items": data}
        if isinstance(data, dict):
            # Check if the response has a nested JSON string
            for key in ['content', 'data', 'result']:
                if key in data and isinstance(data[key], str):
                    try:
                        parsed = json.loads(data[key])
                        if isinstance(parsed, list):
                            return {"items": parsed}
                        return parsed
                    except json.JSONDecodeError:
                        pass
            # If already has 'items', return as-is
            if 'items' in data:
                return data
            return data
        return data
    except json.JSONDecodeError:
        print(f"  [ERROR] Invalid JSON response: {result.stdout[:200]}")
        return None


def call_apify(actor_id, run_input, max_items=500):
    """Call Apify actor via the Pipedream connector."""
    tool_payload = {
        "source_id": "apify__pipedream",
        "tool_name": "apify-run-actor",
        "arguments": {
            "actorSource": "store",
            "actorId": actor_id,
            "runAsynchronously": False,
            "maxItems": str(max_items),
        }
    }
    result = subprocess.run(
        ["external-tool", "call", json.dumps(tool_payload)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  [ERROR] Apify call failed: {result.stderr[:200]}")
        return None
    try:
        data = json.loads(result.stdout)
        if isinstance(data, dict):
            # May need to fetch dataset items separately
            dataset_id = data.get('defaultDatasetId') or data.get('data', {}).get('defaultDatasetId')
            if dataset_id:
                return fetch_apify_dataset(dataset_id)
            for key in ['content', 'data', 'result']:
                if key in data and isinstance(data[key], str):
                    try:
                        return json.loads(data[key])
                    except json.JSONDecodeError:
                        pass
        return data
    except json.JSONDecodeError:
        print(f"  [ERROR] Invalid JSON from Apify: {result.stdout[:200]}")
        return None


def fetch_apify_dataset(dataset_id, limit=500):
    """Fetch items from an Apify dataset."""
    tool_payload = {
        "source_id": "apify__pipedream",
        "tool_name": "apify-get-dataset-items",
        "arguments": {
            "datasetId": dataset_id,
            "clean": True,
            "limit": limit,
        }
    }
    result = subprocess.run(
        ["external-tool", "call", json.dumps(tool_payload)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  [ERROR] Apify dataset fetch failed: {result.stderr[:200]}")
        return None
    try:
        data = json.loads(result.stdout)
        if isinstance(data, dict) and 'content' in data:
            try:
                return json.loads(data['content'])
            except (json.JSONDecodeError, TypeError):
                pass
        return data
    except json.JSONDecodeError:
        return None


# ─────────────────────────────────────────────
# STEP 2: Channel Discovery
# ─────────────────────────────────────────────

def discover_channels():
    """
    Discover relevant YouTube channels by:
    1. Starting with seed channels from config
    2. Searching YouTube for additional channels covering the topic
    """
    print("\n" + "="*60)
    print("STEP 1: CHANNEL DISCOVERY")
    print("="*60)

    all_channels = dict(SEED_CHANNELS)
    print(f"  Starting with {len(all_channels)} seed channels")

    # Search for additional channels via key queries
    discovery_queries = [
        "US Iran war analysis 2026",
        "Strait of Hormuz crisis commentary",
        "Middle East war geopolitics",
        "Iran war economic impact analysis",
        "military analysis Iran 2026",
    ]

    for query in discovery_queries:
        print(f"  Searching: '{query}'...")
        result = call_youtube_api("youtube_data_api-search-videos", {
            "q": query,
            "maxResults": 10,
            "sortOrder": "relevance"
        })
        if result and "items" in result:
            for item in result["items"]:
                ch_id = item.get("snippet", {}).get("channelId") or item.get("id", {}).get("channelId")
                ch_name = item.get("snippet", {}).get("channelTitle", "Unknown")
                if ch_id and ch_id not in all_channels:
                    all_channels[ch_id] = ch_name
                    print(f"    + Discovered: {ch_name} ({ch_id})")
        time.sleep(0.5)

    print(f"\n  Total channels: {len(all_channels)}")

    # Save channel list
    channels_file = f"{METADATA_DIR}/channels.json"
    with open(channels_file, "w") as f:
        json.dump(all_channels, f, indent=2)
    print(f"  Saved to {channels_file}")

    return all_channels


# ─────────────────────────────────────────────
# STEP 3: Video Discovery
# ─────────────────────────────────────────────

def discover_videos(channels):
    """
    Find relevant videos from:
    1. Recent uploads from each channel (within date range)
    2. Search results for topic-specific queries
    Deduplicates by video_id.
    """
    print("\n" + "="*60)
    print("STEP 2: VIDEO DISCOVERY")
    print("="*60)

    all_videos = {}  # video_id -> metadata dict

    # A) Search-based discovery (main approach)
    for i, query in enumerate(SEARCH_QUERIES):
        print(f"  [{i+1}/{len(SEARCH_QUERIES)}] Searching: '{query}'...")
        result = call_youtube_api("youtube_data_api-search-videos", {
            "q": query,
            "maxResults": 50,
            "sortOrder": "relevance",
            "videoDuration": "medium",  # 4-20 minutes
        })

        if result and "items" in result:
            for item in result["items"]:
                vid_id = item.get("id", {}).get("videoId")
                if not vid_id:
                    continue
                snippet = item.get("snippet", {})
                vid_meta = {
                    "video_id": vid_id,
                    "title": snippet.get("title", ""),
                    "description": snippet.get("description", ""),
                    "channel_id": snippet.get("channelId", ""),
                    "channel_name": snippet.get("channelTitle", ""),
                    "published_at": snippet.get("publishedAt", ""),
                    "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                    "search_query": query,
                }
                all_videos[vid_id] = vid_meta
            print(f"    Found {len(result['items'])} videos (total unique: {len(all_videos)})")
        time.sleep(0.3)

    # B) Also search for long-form content
    long_form_queries = [
        "Iran war documentary 2026",
        "Strait of Hormuz full analysis",
        "US Iran war explained",
        "Middle East crisis March 2026 full",
    ]
    for query in long_form_queries:
        print(f"  [LONG] Searching: '{query}'...")
        result = call_youtube_api("youtube_data_api-search-videos", {
            "q": query,
            "maxResults": 25,
            "sortOrder": "relevance",
            "videoDuration": "long",  # >20 minutes
        })
        if result and "items" in result:
            for item in result["items"]:
                vid_id = item.get("id", {}).get("videoId")
                if not vid_id:
                    continue
                snippet = item.get("snippet", {})
                all_videos[vid_id] = {
                    "video_id": vid_id,
                    "title": snippet.get("title", ""),
                    "description": snippet.get("description", ""),
                    "channel_id": snippet.get("channelId", ""),
                    "channel_name": snippet.get("channelTitle", ""),
                    "published_at": snippet.get("publishedAt", ""),
                    "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                    "search_query": query,
                }
        time.sleep(0.3)

    print(f"\n  Total unique videos discovered: {len(all_videos)}")

    # Save all videos
    videos_file = f"{METADATA_DIR}/all_videos.json"
    with open(videos_file, "w") as f:
        json.dump(list(all_videos.values()), f, indent=2)
    print(f"  Saved to {videos_file}")

    return all_videos


# ─────────────────────────────────────────────
# STEP 4: Enrich with Statistics
# ─────────────────────────────────────────────

def enrich_video_stats(videos):
    """
    Fetch view count, like count, comment count, and duration
    for all discovered videos using YouTube Data API videos.list.
    """
    print("\n" + "="*60)
    print("STEP 3: ENRICHING VIDEO STATISTICS")
    print("="*60)

    video_ids = list(videos.keys())
    # YouTube API allows up to 50 IDs per request
    batch_size = 50
    enriched = 0

    for i in range(0, len(video_ids), batch_size):
        batch = video_ids[i:i+batch_size]
        ids_str = ",".join(batch)
        print(f"  Fetching stats for batch {i//batch_size + 1} ({len(batch)} videos)...")

        result = call_youtube_api("youtube_data_api-list-videos", {
            "useCase": "id",
            "id": ids_str,
        })

        if result and "items" in result:
            for item in result["items"]:
                vid_id = item["id"]
                if vid_id in videos:
                    stats = item.get("statistics", {})
                    content = item.get("contentDetails", {})
                    videos[vid_id]["view_count"] = int(stats.get("viewCount", 0))
                    videos[vid_id]["like_count"] = int(stats.get("likeCount", 0))
                    videos[vid_id]["comment_count"] = int(stats.get("commentCount", 0))
                    videos[vid_id]["duration"] = content.get("duration", "")
                    videos[vid_id]["tags"] = item.get("snippet", {}).get("tags", [])
                    enriched += 1
        time.sleep(0.3)

    print(f"  Enriched {enriched}/{len(video_ids)} videos with statistics")

    # Re-save with stats
    videos_file = f"{METADATA_DIR}/all_videos_enriched.json"
    with open(videos_file, "w") as f:
        json.dump(list(videos.values()), f, indent=2)
    print(f"  Saved to {videos_file}")

    return videos


# ─────────────────────────────────────────────
# STEP 5: Transcript Extraction (3-tier)
# ─────────────────────────────────────────────

def extract_transcripts(videos):
    """
    Three-tier transcript extraction:
    1. youtube-transcript-api (free, fastest) — English > auto-generated > translated
    2. Apify YouTube Transcript Scraper (fallback for restricted videos)
    3. yt-dlp subtitle download (last resort)
    """
    print("\n" + "="*60)
    print("STEP 4: TRANSCRIPT EXTRACTION")
    print("="*60)

    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        ytt_api = YouTubeTranscriptApi()
        has_ytt = True
    except ImportError:
        print("  [WARN] youtube-transcript-api not installed, will use Apify only")
        has_ytt = False

    results = {"success": [], "failed": []}
    video_list = list(videos.values())

    for i, video in enumerate(video_list):
        vid_id = video["video_id"]
        title = video.get("title", "?")[:60]
        channel = video.get("channel_name", "?")

        print(f"  [{i+1}/{len(video_list)}] {channel}: {title}...")

        transcript_text = None

        # TIER 1: youtube-transcript-api
        if has_ytt:
            try:
                transcript_list = ytt_api.list(vid_id)
                transcript = None

                # Prefer manual English
                for t in transcript_list:
                    if t.language_code.startswith("en") and not getattr(t, "is_generated", False):
                        transcript = t.fetch()
                        break

                # Fallback: auto-generated English
                if not transcript:
                    for t in transcript_list:
                        if t.language_code.startswith("en"):
                            transcript = t.fetch()
                            break

                # Fallback: any language, translate to English
                if not transcript:
                    for t in transcript_list:
                        if t.is_translatable:
                            try:
                                transcript = t.translate("en").fetch()
                                break
                            except Exception:
                                pass

                if transcript:
                    transcript_text = " ".join([s.text for s in transcript])
                    video["transcript_source"] = "youtube-transcript-api"

            except Exception as e:
                err = str(e)[:100]
                if "disabled" not in err.lower():
                    print(f"    Tier 1 failed: {err}")

        # TIER 2: yt-dlp subtitle download
        if not transcript_text:
            try:
                sub_file = f"/tmp/sub_{vid_id}"
                cmd = [
                    "yt-dlp", "--skip-download",
                    "--write-auto-sub", "--write-sub",
                    "--sub-lang", "en",
                    "--sub-format", "vtt",
                    "-o", sub_file,
                    f"https://www.youtube.com/watch?v={vid_id}"
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

                # Find the subtitle file
                for ext in [".en.vtt", ".en.auto.vtt"]:
                    path = f"{sub_file}{ext}"
                    if os.path.exists(path):
                        with open(path) as f:
                            raw = f.read()
                        # Strip VTT formatting
                        lines = []
                        for line in raw.split("\n"):
                            line = line.strip()
                            if not line or line.startswith("WEBVTT") or "-->" in line or re.match(r"^\d+$", line):
                                continue
                            # Remove HTML tags
                            line = re.sub(r"<[^>]+>", "", line)
                            if line:
                                lines.append(line)
                        transcript_text = " ".join(lines)
                        video["transcript_source"] = "yt-dlp"
                        # Cleanup
                        os.remove(path)
                        break
            except Exception as e:
                print(f"    Tier 2 (yt-dlp) failed: {str(e)[:80]}")

        # TIER 3: Apify (expensive, last resort)
        if not transcript_text:
            try:
                apify_result = call_apify(
                    "bernardo~youtube-transcript-scraper",
                    {"videoUrls": [f"https://www.youtube.com/watch?v={vid_id}"]}
                )
                if apify_result and isinstance(apify_result, list) and len(apify_result) > 0:
                    transcript_text = apify_result[0].get("transcript", "")
                    video["transcript_source"] = "apify"
            except Exception as e:
                print(f"    Tier 3 (Apify) failed: {str(e)[:80]}")

        # Save result
        if transcript_text and len(transcript_text) > 100:
            # Cap at 100k chars
            transcript_text = transcript_text[:100000]
            video["transcript_length"] = len(transcript_text)

            # Save individual transcript
            with open(f"{TRANSCRIPTS_DIR}/{vid_id}.txt", "w") as f:
                f.write(f"Channel: {channel}\nTitle: {video.get('title', '')}\n"
                        f"Published: {video.get('published_at', '')}\n\n{transcript_text}")

            results["success"].append(vid_id)
            print(f"    OK ({len(transcript_text)} chars via {video.get('transcript_source', '?')})")
        else:
            results["failed"].append({"video_id": vid_id, "title": title, "channel": channel})
            print(f"    FAILED - no transcript available")

    print(f"\n  Transcripts: {len(results['success'])} success, {len(results['failed'])} failed")

    # Save results summary
    with open(f"{METADATA_DIR}/transcript_results.json", "w") as f:
        json.dump(results, f, indent=2)

    return results


# ─────────────────────────────────────────────
# STEP 6: Comment Extraction
# ─────────────────────────────────────────────

def extract_comments(videos, max_per_video=100):
    """
    Extract top comments for each video using Apify YouTube Comment Scraper.
    Focus on high-engagement comments (>5 likes or >30 words).
    """
    print("\n" + "="*60)
    print("STEP 5: COMMENT EXTRACTION")
    print("="*60)

    # Sort videos by engagement (view_count) and take top ones
    sorted_vids = sorted(
        videos.values(),
        key=lambda v: v.get("view_count", 0),
        reverse=True
    )

    # Extract comments for top 100 most viewed videos
    target_vids = sorted_vids[:100]
    print(f"  Extracting comments for top {len(target_vids)} videos by view count")

    total_comments = 0
    for i, video in enumerate(target_vids):
        vid_id = video["video_id"]
        title = video.get("title", "?")[:50]
        views = video.get("view_count", 0)

        print(f"  [{i+1}/{len(target_vids)}] {title}... ({views:,} views)")

        try:
            result = call_apify(
                "bernardo~youtube-comment-scraper",
                {
                    "videoUrls": [f"https://www.youtube.com/watch?v={vid_id}"],
                    "maxComments": max_per_video,
                    "sortBy": "top"
                }
            )

            if result and isinstance(result, list):
                # Filter: >5 likes OR >30 words
                filtered = []
                for c in result:
                    text = c.get("text", "")
                    likes = c.get("likeCount", 0) or c.get("like_count", 0) or 0
                    word_count = len(text.split())

                    if word_count > 30 or likes > 5:
                        filtered.append({
                            "text": text,
                            "like_count": likes,
                            "author": c.get("author", ""),
                            "timestamp": c.get("publishedAt", ""),
                            "is_reply": c.get("isReply", False),
                        })

                # Sort by likes
                filtered.sort(key=lambda x: x["like_count"], reverse=True)
                filtered = filtered[:50]  # max 50 per video

                if filtered:
                    with open(f"{COMMENTS_DIR}/{vid_id}.json", "w") as f:
                        json.dump(filtered, f, indent=2)
                    total_comments += len(filtered)
                    print(f"    Saved {len(filtered)} quality comments")
                else:
                    print(f"    No quality comments found")
            else:
                print(f"    No comments returned")

        except Exception as e:
            print(f"    Error: {str(e)[:80]}")

        time.sleep(1)  # Rate limiting

    print(f"\n  Total quality comments extracted: {total_comments}")
    return total_comments


# ─────────────────────────────────────────────
# STEP 7: Assembly
# ─────────────────────────────────────────────

def assemble_corpus(videos, transcript_results):
    """
    Assemble the complete corpus: video metadata + transcripts + comments
    into a single knowledge base JSON ready for Open WebUI upload.
    """
    print("\n" + "="*60)
    print("STEP 6: CORPUS ASSEMBLY")
    print("="*60)

    corpus = []
    success_ids = set(transcript_results.get("success", []))

    for vid_id, video in videos.items():
        entry = {
            "video_id": vid_id,
            "title": video.get("title", ""),
            "channel_name": video.get("channel_name", ""),
            "published_at": video.get("published_at", ""),
            "view_count": video.get("view_count", 0),
            "like_count": video.get("like_count", 0),
            "comment_count": video.get("comment_count", 0),
            "duration": video.get("duration", ""),
            "description": video.get("description", ""),
            "tags": video.get("tags", []),
            "has_transcript": vid_id in success_ids,
            "transcript_source": video.get("transcript_source", ""),
        }

        # Load transcript if available
        transcript_path = f"{TRANSCRIPTS_DIR}/{vid_id}.txt"
        if os.path.exists(transcript_path):
            with open(transcript_path) as f:
                entry["transcript"] = f.read()

        # Load comments if available
        comments_path = f"{COMMENTS_DIR}/{vid_id}.json"
        if os.path.exists(comments_path):
            with open(comments_path) as f:
                entry["comments"] = json.load(f)

        corpus.append(entry)

    # Sort by view count
    corpus.sort(key=lambda x: x.get("view_count", 0), reverse=True)

    # Save full corpus
    corpus_file = f"{CORPUS_DIR}/full_corpus.json"
    with open(corpus_file, "w") as f:
        json.dump(corpus, f, indent=2)

    # Stats
    with_transcript = sum(1 for c in corpus if c.get("has_transcript"))
    with_comments = sum(1 for c in corpus if c.get("comments"))
    total_transcript_chars = sum(len(c.get("transcript", "")) for c in corpus)
    total_comments = sum(len(c.get("comments", [])) for c in corpus)

    print(f"  Corpus assembled:")
    print(f"    Videos: {len(corpus)}")
    print(f"    With transcripts: {with_transcript}")
    print(f"    With comments: {with_comments}")
    print(f"    Total transcript text: {total_transcript_chars:,} chars")
    print(f"    Total quality comments: {total_comments}")
    print(f"  Saved to {corpus_file}")

    # Also create a summary file
    summary = {
        "topic": DOCUMENTARY_TOPIC,
        "subtitle": DOCUMENTARY_SUBTITLE,
        "date_range": f"{DATE_RANGE_START} to {DATE_RANGE_END}",
        "extraction_date": datetime.now(timezone.utc).isoformat(),
        "total_videos": len(corpus),
        "videos_with_transcripts": with_transcript,
        "videos_with_comments": with_comments,
        "total_transcript_chars": total_transcript_chars,
        "total_quality_comments": total_comments,
        "top_channels": {},
    }

    # Count videos per channel
    channel_counts = {}
    for c in corpus:
        ch = c.get("channel_name", "Unknown")
        channel_counts[ch] = channel_counts.get(ch, 0) + 1
    summary["top_channels"] = dict(sorted(channel_counts.items(), key=lambda x: x[1], reverse=True)[:20])

    with open(f"{METADATA_DIR}/extraction_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    return corpus


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("="*60)
    print(f"YOUTUBE EXTRACTION PIPELINE")
    print(f"Topic: {DOCUMENTARY_TOPIC}")
    print(f"Date Range: {DATE_RANGE_START} to {DATE_RANGE_END}")
    print(f"Output: {OUTPUT_DIR}")
    print("="*60)

    # Step 1: Discover channels
    channels = discover_channels()

    # Step 2: Find videos
    videos = discover_videos(channels)

    # Step 3: Enrich with stats
    videos = enrich_video_stats(videos)

    # Step 4: Extract transcripts
    transcript_results = extract_transcripts(videos)

    # Step 5: Extract comments (top videos only)
    extract_comments(videos)

    # Step 6: Assemble corpus
    corpus = assemble_corpus(videos, transcript_results)

    print("\n" + "="*60)
    print("EXTRACTION COMPLETE")
    print("="*60)
    print(f"  Corpus ready at: {CORPUS_DIR}/full_corpus.json")
    print(f"  Next: Run deep_search_refine.py to upload to Open WebUI and refine")

    return corpus


if __name__ == "__main__":
    main()
