# YouTube Data Store Architecture

## Problem Statement

We monitor 37 financial YouTube channels daily. Each run currently re-downloads everything from scratch. We need:
1. **Persistent, internet-accessible data store** — no redundant downloads between sessions
2. **Intelligent throttling** — YouTube bans aggressive scrapers
3. **Comment extraction** — high-value comments surfaced for script generation
4. **Transcript storage** — deduplicated, never re-fetched

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     B2: economy-vid-assets                       │
│                                                                 │
│  youtube-data/                                                  │
│  ├── catalog.parquet           ← master index (all videos)      │
│  ├── transcripts/                                               │
│  │   ├── {video_id}.json       ← timestamped transcript         │
│  ├── comments/                                                  │
│  │   ├── {video_id}.parquet    ← all comments for video         │
│  ├── metadata/                                                  │
│  │   ├── {video_id}.json       ← title, desc, stats, tags       │
│  └── digests/                                                   │
│      ├── {date}.json           ← daily LLM-extracted insights   │
└─────────────────────────────────────────────────────────────────┘
         │
         │  S3-compatible API (DuckDB httpfs / direct HTTP)
         │
    ┌────┴────┐
    │ Runner  │  ← Vast.ai VM, local machine, or cron agent
    │         │
    │  1. Read catalog.parquet from B2
    │  2. List new videos via YouTube Data API (cheap: 1 unit/call)
    │  3. Diff: which videos are new? which need comment refresh?
    │  4. Fetch only what's missing (throttled)
    │  5. Write updated files back to B2
    │  6. Generate daily digest for script LLM
    └─────────┘
```

## Data Layers

### Layer 1: Video Catalog (`catalog.parquet`)

Single Parquet file, ~50KB for thousands of videos. Columns:

| Column | Type | Description |
|--------|------|-------------|
| video_id | string | YouTube video ID (primary key) |
| channel_id | string | Channel ID |
| channel_name | string | Human-readable |
| title | string | Video title |
| published_at | timestamp | Upload date |
| duration_seconds | int | Video length |
| view_count | int | Views at last check |
| comment_count | int | Comments at last check |
| transcript_fetched | bool | Do we have the transcript? |
| transcript_method | string | "api_captions" / "yt-dlp_auto" / "whisper" |
| comments_fetched_at | timestamp | Last comment fetch time |
| comments_count_stored | int | How many comments we have |
| metadata_fetched | bool | Full metadata stored? |
| tags | string[] | Video tags |
| relevance_score | float | LLM-assigned relevance (0-1) |

This is the **single source of truth**. Every run starts by downloading this one file (~50KB), diffing against YouTube API results, and deciding what to fetch.

### Layer 2: Transcripts (`transcripts/{video_id}.json`)

```json
{
  "video_id": "abc123",
  "method": "api_captions",
  "language": "en",
  "fetched_at": "2026-03-13T10:00:00Z",
  "segments": [
    {"start": 0.0, "end": 3.5, "text": "Welcome back to the show."},
    {"start": 3.5, "end": 7.2, "text": "Today we're looking at the oil crisis."}
  ],
  "full_text": "Welcome back to the show. Today we're looking at..."
}
```

**Fetch strategy**: Transcripts are immutable — once fetched, never re-fetched. Use YouTube's caption API first (free, fast), fall back to yt-dlp auto-subs, final fallback is Whisper on audio.

### Layer 3: Comments (`comments/{video_id}.parquet`)

Per-video Parquet file. Columns:

| Column | Type | Description |
|--------|------|-------------|
| comment_id | string | YouTube comment ID |
| parent_id | string | null for top-level, parent ID for replies |
| author | string | Display name |
| author_channel_id | string | For identifying recurring experts |
| text | string | Comment text |
| like_count | int | Likes |
| reply_count | int | Number of replies (top-level only) |
| published_at | timestamp | When posted |
| updated_at | timestamp | Last edit |
| is_pinned | bool | Pinned by creator? |

**Fetch strategy**: Comments grow over time. We fetch:
- **New videos**: All comments (initial fetch)
- **Recent videos (< 14 days)**: Incremental — fetch by `order=time`, stop when we hit a comment_id we already have
- **Old videos (> 14 days)**: Skip unless comment_count changed significantly (>20% increase)

### Layer 4: Daily Digests (`digests/{date}.json`)

LLM-generated summaries of new content. Pre-computed so the script generator doesn't need to re-process everything.

```json
{
  "date": "2026-03-13",
  "new_videos": 12,
  "new_comments_analyzed": 847,
  "top_stories": [...],
  "notable_comments": [
    {
      "video_id": "xyz",
      "video_title": "Oil Markets in Crisis",
      "comment_author": "FormerTrader_NYC",
      "comment_text": "I traded crude for 15 years. This isn't 2008...",
      "like_count": 234,
      "why_notable": "Domain expert contrarian take with high engagement"
    }
  ],
  "sentiment_clusters": [...]
}
```

## Intelligent Throttling

YouTube has three enforcement layers, each requiring different mitigation:

### 1. YouTube Data API Quota (10,000 units/day)

**Budget allocation per daily run:**

| Operation | Unit Cost | Budget | Capacity |
|-----------|-----------|--------|----------|
| channels.list (check for new videos) | 1 | 37 units | 37 channels |
| playlistItems.list (get recent uploads) | 1 | ~200 units | 37 channels × ~5 pages |
| commentThreads.list (maxResults=100) | 1 | ~5,000 units | 500 pages × 100 comments = 50,000 comments |
| comments.list (deep reply threads) | 1 | ~1,000 units | 1,000 reply pages |
| videos.list (metadata batch, 50/call) | 1 | ~50 units | ~2,500 videos checked |
| **Reserve** | | ~3,713 units | Safety margin |
| **Total** | | **~6,287 units** | Well under 10K |

With maxResults=100, one API call fetches 100 top-level comments (each with up to 5 inline replies). For 37 channels producing maybe 5-10 new videos/day, that's 50-100 videos needing comment extraction. At ~200 comments per video average, that's ~200 API calls — well within budget.

**Optimization**: Use multiple Google Cloud projects (each gets 10K/day). Or use OAuth user tokens (higher per-user quota of 50-100K/day).

### 2. yt-dlp Scraping Rate (for transcripts/audio)

YouTube aggressively rate-limits yt-dlp. Strategy:

```python
YT_DLP_OPTS = {
    # Transcript/subtitle download only — no video download
    'skip_download': True,
    'writeautomaticsub': True,
    'writesubtitles': True,
    'subtitleslangs': ['en'],
    'subtitlesformat': 'json3',
    
    # Throttling
    'sleep_interval': 10,          # 10s between requests
    'max_sleep_interval': 30,      # up to 30s random
    'sleep_interval_subtitles': 5, # 5s between subtitle fetches
    'sleep_requests': 1,           # 1s between HTTP requests
    
    # Anti-detection
    'extractor_args': {'youtube': {
        'player_client': ['web', 'android'],  # rotate clients
    }},
}
```

**Key insight**: We only need yt-dlp for transcripts that aren't available via the official captions API. Most financial channels have auto-generated English captions accessible via the API (free, no scraping). yt-dlp is the fallback.

**Daily yt-dlp budget**: Max 50 transcript fetches/day at 10-30s intervals = 8-25 minutes of wall time. Completely sustainable.

### 3. IP-level blocking

For when yt-dlp is needed:

- **Rotating exit nodes**: Use SOCKS5 proxies or Tor for yt-dlp requests (not API calls — those use the API key)
- **Time-of-day spreading**: Don't fetch all 50 transcripts in one burst. Spread across 6-hour windows
- **Exponential backoff**: On 429 responses, back off 2x (30s → 60s → 120s → 240s). After 3 failures for the same video, defer to next day
- **Client rotation**: Alternate between web, android, ios player clients

### Throttle State Machine

```
NORMAL (10-30s sleep) 
    → on 429: CAUTIOUS (60-120s sleep, halve batch size)
    → on 2nd 429: BACKING_OFF (5min pause, then 120-240s sleep)
    → on 3rd 429: DEFERRED (stop for today, queue for tomorrow)
    
CAUTIOUS → 10 successful fetches → NORMAL
BACKING_OFF → 5 successful fetches → CAUTIOUS
DEFERRED → next calendar day → NORMAL
```

State persists in `catalog.parquet` (per-video `fetch_state` and `defer_until` columns).

## Comment Intelligence Pipeline

Raw comments are useless for script generation. The pipeline:

### Stage 1: Filter (no LLM needed)

```python
def is_potentially_valuable(comment):
    """Fast heuristic filter — runs on all comments"""
    if comment.like_count >= 10:
        return True  # Community endorsement
    if len(comment.text.split()) >= 40:
        return True  # Substantial analysis
    if comment.reply_count >= 3:
        return True  # Sparked discussion
    if any(kw in comment.text.lower() for kw in [
        'i work in', 'i trade', 'years of experience',
        'here in poland', 'in europe', 'actually',
        'the data shows', 'bloomberg', 'reuters'
    ]):
        return True  # Domain expertise signals
    return False
```

This filters ~95% of "great video!" and "first!" comments. Typically reduces 10,000 comments to 200-500 candidates.

### Stage 2: LLM Scoring (on filtered candidates)

Batch the 200-500 candidates into a single LLM call:

```
For each comment, rate 1-10 on:
- Domain expertise (does the commenter demonstrate professional knowledge?)
- Insight novelty (does this add information not in the video itself?)
- Contrarian value (does it challenge the prevailing narrative with evidence?)
- Quotability (could this be paraphrased in a documentary narration?)

Return only comments scoring 7+ on any dimension.
```

### Stage 3: Cluster and Deduplicate

Group high-scoring comments by topic/stance. Many comments will say the same thing. Pick the best representative from each cluster.

### Output: Notable Comments for Script Generation

```json
{
  "topic": "oil_supply_disruption",
  "stance": "contrarian_bullish",
  "best_comment": {
    "author": "FormerTrader_NYC",
    "text": "I traded crude for 15 years. The physical market...",
    "video": "Oil Markets in Crisis (David Lin)",
    "engagement": {"likes": 234, "replies": 12}
  },
  "cluster_size": 7,
  "summary": "Multiple commenters with trading experience argue the physical oil market has enough strategic reserve capacity to absorb the Hormuz disruption within 90 days"
}
```

## Daily Run Sequence

```
1. CATALOG SYNC (2 min, ~250 API units)
   - Download catalog.parquet from B2
   - For each of 37 channels: list recent uploads via API
   - Diff against catalog: identify new videos
   - Update catalog with new entries (transcript_fetched=false, etc.)

2. METADATA FETCH (1 min, ~50 API units)
   - Batch fetch metadata for new videos (videos.list, 50 per call)
   - Store in metadata/{video_id}.json on B2

3. TRANSCRIPT FETCH (10-25 min, 0 API units — uses captions API or yt-dlp)
   - For each video where transcript_fetched=false:
     a. Try official captions API (free, fast)
     b. Fall back to yt-dlp with throttling
     c. Mark success/failure in catalog
   - Upload transcripts to B2

4. COMMENT FETCH (5-15 min, ~3,000-5,000 API units)
   - New videos: full comment pull
   - Recent videos (<14 days): incremental (newest first, stop at known ID)
   - Old videos: skip unless comment_count changed >20%
   - Upload comment parquet files to B2

5. COMMENT INTELLIGENCE (5 min, uses LLM)
   - Filter → Score → Cluster → Extract notable comments
   - Generate daily digest
   - Upload digest to B2

6. CATALOG FINALIZE (10 sec)
   - Upload updated catalog.parquet to B2

Total: ~25-45 min, ~4,000-6,000 API units (well under 10K daily limit)
```

## Accessing the Data Store

### From a Vast.ai VM (pip-discovery integration)

```python
import duckdb

con = duckdb.connect()
con.execute("""
    INSTALL httpfs; LOAD httpfs;
    CREATE SECRET (
        TYPE s3,
        KEY_ID '${B2_KEY_ID}',
        SECRET '${B2_APP_KEY}',
        REGION 'us-west-004',
        ENDPOINT 's3.us-west-004.backblazeb2.com'
    );
""")

# Query the catalog
df = con.execute("""
    SELECT video_id, title, channel_name, published_at, comment_count
    FROM read_parquet('s3://economy-vid-assets/youtube-data/catalog.parquet')
    WHERE published_at > '2026-03-10'
    ORDER BY comment_count DESC
""").fetchdf()

# Read comments for a specific video
comments = con.execute("""
    SELECT text, like_count, author
    FROM read_parquet('s3://economy-vid-assets/youtube-data/comments/dQw4w9WgXcQ.parquet')
    WHERE like_count >= 10
    ORDER BY like_count DESC
    LIMIT 50
""").fetchdf()
```

### From the script generation LLM

The LLM doesn't query B2 directly. Instead, it receives the daily digest JSON which contains:
- New video summaries (from transcripts)
- Notable comments (pre-scored and clustered)
- Sentiment overview
- Contrarian takes

This is the same pattern as the current `all_knowledge.json` but with richer, higher-quality inputs.

## File Size Estimates

| Component | Per Video | 37 Channels × 1 Year |
|-----------|-----------|----------------------|
| Catalog entry | ~500 bytes | ~2.5 MB (for ~5,000 videos) |
| Transcript | ~50 KB | ~250 MB |
| Comments (parquet) | ~200 KB avg | ~1 GB |
| Metadata | ~2 KB | ~10 MB |
| Daily digests | ~50 KB | ~18 MB |
| **Total** | | **~1.3 GB/year** |

At B2 pricing ($5/TB/month), this costs essentially nothing. Even at 10x this scale (370 channels, 10 years of backfill), it's ~13 GB — still negligible.

## When This Becomes Terabyte-Scale

The architecture above handles the YouTube comment + transcript use case easily. But when you add:
- FRED (800K time series, decades of history)
- World Bank / IMF datasets
- Commodity futures tick data
- AIS shipping data
- Full-text economic reports (IMF WEO, Fed minutes, central bank speeches)

...then you hit terabytes. The same B2 + Parquet + DuckDB stack scales:

- Partition by source and date: `economic-data/fred/{series_id}/{year}.parquet`
- DuckDB's httpfs reads only the Parquet column chunks it needs (columnar + range requests = minimal transfer)
- For vector search over text (Fed minutes, IMF reports): embed and store in a separate vector index, also on B2
- The script LLM never touches the raw data — it uses tool calls through an intermediary that queries the right partition

The catalog pattern scales too: one master Parquet file per data domain listing what exists, what's been updated, what's stale. The runner reads the catalog, diffs against source, fetches only deltas.
