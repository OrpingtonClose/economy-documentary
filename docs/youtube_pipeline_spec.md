# YouTube Data Pipeline — Full Specification

## Purpose

Persistent, incrementally-updated YouTube data store for the economy news video pipeline. Extracts transcripts, comments, and metadata from 37 financial YouTube channels. Surfaces high-value comments with research-backed enrichment for script generation. Never re-downloads what it already has.

## Design Decisions

### Storage: B2 + Parquet, queried via DuckDB httpfs
- All data lives in the existing `economy-vid-assets` B2 bucket under `youtube-data/`
- B2 credentials: keyID `${B2_KEY_ID}`, appKey `${B2_APP_KEY}`
- S3 endpoint: `s3.us-west-004.backblazeb2.com`
- Parquet for structured data (catalog, comments), JSON for documents (transcripts, metadata, digests)
- Queryable from anywhere (Vast.ai VM, local machine, cron agent) via DuckDB S3/httpfs — no database server needed
- Estimated size: ~1.3 GB/year for 37 channels. Negligible cost at B2 rates ($5/TB/month)

### Orchestration: Plain Python, not LangGraph
- The pipeline is a linear sequence with conditional skips, not an agent loop with unpredictable control flow
- LangGraph adds framework dependency, serialization overhead, and debugging complexity for no benefit here
- Resume/checkpoint is handled by the B2 catalog itself — same pattern as the V4 video batch generator's `progress.json`
- If orchestration is needed later (retries, scheduling, dashboard), use Prefect (credentials already in pip-discovery)
- LangGraph is reserved for PIP Discovery's serendipity engine where genuine LLM-driven exploration branching exists

### Throttling: Three-layer strategy
- **YouTube Data API**: 10,000 units/day quota. Daily run uses ~4,000-6,000 units. commentThreads.list = 1 unit per 100 comments
- **yt-dlp** (transcript fallback only): 10-30s random sleep, client rotation (web/android/ios), exponential backoff, max 50/day
- **IP protection**: Proxy rotation for yt-dlp only. API calls use key, no IP risk
- Throttle state machine per video: NORMAL → CAUTIOUS → BACKING_OFF → DEFERRED (next day). State persists in catalog

### Comment intelligence: Filter → Score → Enrich → Cluster
- Stage 1 (heuristic filter, no LLM): keep comments with 10+ likes, 40+ words, 3+ replies, or domain expertise signals. Drops ~95% of noise
- Stage 2 (LLM scoring): batch 200-500 candidates, rate on expertise/novelty/contrarian value/quotability
- Stage 3 (research enrichment, selective — only 10-20 best comments):
  - Factual claims → verify via Perplexity Sonar or Exa search
  - Contrarian takes → find strongest counterargument via ad-hoc LLM call (Claude/DeepSeek)
  - Data references → Exa search for primary source, fetch and extract relevant passage
  - Expert opinions worth expanding → Perplexity Sonar deep research for broader context
  - Tool access: `perplexity_sonar(query)`, `exa_search(query)`, `llm_ask(model, question)`, `fetch_url(url)`
  - All API keys already available in pip-discovery project
  - Cost: ~$0.10-0.50/day, 2-5 min wall time
  - Max 2 hops deep per claim — simple loop with depth limit, not an agent graph
- Stage 4 (cluster + deduplicate): group by topic/stance, pick best representative per cluster

## Data Schema

### B2 Directory Structure

```
economy-vid-assets/
└── youtube-data/
    ├── catalog.parquet            # Master index — single source of truth
    ├── transcripts/
    │   └── {video_id}.json        # Timestamped transcript (immutable once fetched)
    ├── comments/
    │   └── {video_id}.parquet     # All comments for video (appendable)
    ├── metadata/
    │   └── {video_id}.json        # Title, description, stats, tags
    └── digests/
        └── {date}.json            # Daily LLM-extracted insights + notable comments
```

### catalog.parquet Columns

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
| fetch_state | string | "normal" / "cautious" / "backing_off" / "deferred" |
| defer_until | timestamp | When to retry if deferred |

### Transcript JSON

```json
{
  "video_id": "abc123",
  "method": "api_captions",
  "language": "en",
  "fetched_at": "2026-03-13T10:00:00Z",
  "segments": [
    {"start": 0.0, "end": 3.5, "text": "Welcome back to the show."}
  ],
  "full_text": "Welcome back to the show. ..."
}
```

### Comments Parquet Columns

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

### Daily Digest JSON

```json
{
  "date": "2026-03-13",
  "new_videos": 12,
  "new_comments_analyzed": 847,
  "top_stories": [
    {
      "headline": "...",
      "summary": "...",
      "sources": ["video_id_1", "video_id_2"],
      "key_data_points": ["Brent hit $127 on March 5th"]
    }
  ],
  "notable_comments": [
    {
      "video_id": "xyz",
      "video_title": "Oil Markets in Crisis",
      "comment_author": "FormerTrader_NYC",
      "comment_text": "I traded crude for 15 years...",
      "like_count": 234,
      "why_notable": "Domain expert contrarian take with high engagement",
      "enrichment": {
        "verification": "SPR claim verified — current US SPR at 372M barrels (DOE, Mar 2026)",
        "counterargument": "IEA disputes 90-day absorption timeline, cites refinery mismatch",
        "sources": ["https://www.eia.gov/...", "https://www.iea.org/..."]
      }
    }
  ],
  "sentiment_clusters": [
    {
      "topic": "oil_supply_disruption",
      "stances": {
        "bearish": {"count": 45, "representative": "..."},
        "cautiously_bullish": {"count": 12, "representative": "..."}
      }
    }
  ]
}
```

## Daily Run Sequence

```
1. CATALOG SYNC (2 min, ~250 API units)
   - Download catalog.parquet from B2
   - For each of 37 channels: list recent uploads via YouTube Data API
   - Diff against catalog: identify new videos
   - Update catalog with new entries

2. METADATA FETCH (1 min, ~50 API units)
   - Batch fetch metadata for new videos (videos.list, 50/call)
   - Store in metadata/{video_id}.json on B2

3. TRANSCRIPT FETCH (10-25 min, 0 API units)
   - For each video where transcript_fetched=false:
     a. Try official captions API (free, fast)
     b. Fall back to yt-dlp with throttling (10-30s sleep, client rotation)
     c. Mark success/failure in catalog
   - Upload transcripts to B2
   - Transcripts are immutable — never re-fetched

4. COMMENT FETCH (5-15 min, ~3,000-5,000 API units)
   - New videos: full comment pull (commentThreads.list, maxResults=100)
   - Recent videos (<14 days): incremental (newest first, stop at known comment_id)
   - Old videos (>14 days): skip unless comment_count changed >20%
   - Upload comment parquet files to B2

5. COMMENT INTELLIGENCE (5-10 min, LLM + research APIs)
   - Heuristic filter: 10K comments → 200-500 candidates
   - LLM scoring: batch score on expertise/novelty/contrarian/quotability
   - Research enrichment (top 10-20 only): verify claims via Sonar/Exa, find counterarguments, fetch primary sources
   - Cluster by topic+stance, pick best representatives
   - Generate daily digest JSON
   - Upload digest to B2

6. CATALOG FINALIZE (10 sec)
   - Upload updated catalog.parquet to B2

Total: ~25-45 min, ~4,000-6,000 API units/day
```

## Monitored Channels (37)

Adam Taggart | Thoughtful Money, Altcoin Daily, Anna Bocca, Azul, Bankless, Benjamin Cowen, Bram Kanstein, Coin Bureau Finance, Commodity Culture, Conor Harris, David Lin, Econ Lessons, Ed Yardeni, EllioTrades, Eurodollar University, Fundamental Investing Institute, Heresy Financial, ITM TRADING INC., Joe Blogs, Josh Olszewicz, Ken McElroy Podcast, Maggie Lake Talking Markets, Market Insider, Nobel Fest, Oxbow Advisors, Polityka Zagraniczna | Marcin Kuśmierczyk, Projekt: 100X, Rosenberg Research, Soar Financially, Stoic Finance, The Ezra Klein Show, The Mark Thompson Show, The Meb Faber Show, The Monetary Matters Network, WEALTHTRACK, We Study Billionaires, Wealthion

Channel IDs stored in: `/home/user/workspace/all_channels.json`

## Integration Points

### Into the video script generator
The script LLM receives the daily digest JSON (from `digests/{date}.json`) which replaces the current `all_knowledge.json`. It contains pre-scored notable comments with enrichment, ready for narration like: "one commenter on David Lin's channel — a former crude trader — put it bluntly: [paraphrase]. And the data backs this up: according to [source], [verified fact]."

### Into PIP Discovery (future)
The serendipity engine can use the catalog and digests as seed material for lateral exploration. Notable comments mentioning specific companies, regions, or data sources become PIP seeds.

### DuckDB access pattern
```python
import duckdb
con = duckdb.connect()
con.execute("""
    INSTALL httpfs; LOAD httpfs;
    CREATE SECRET (TYPE s3,
        KEY_ID '${B2_KEY_ID}',
        SECRET '${B2_APP_KEY}',
        REGION 'us-west-004',
        ENDPOINT 's3.us-west-004.backblazeb2.com');
""")
# Query catalog
con.sql("SELECT * FROM read_parquet('s3://economy-vid-assets/youtube-data/catalog.parquet') WHERE published_at > '2026-03-10'")
# Query comments
con.sql("SELECT * FROM read_parquet('s3://economy-vid-assets/youtube-data/comments/VIDEO_ID.parquet') WHERE like_count >= 10")
```

## Implementation Notes

- YouTube Data API connector is already CONNECTED via Pipedream (source_id: youtube_data_api__pipedream)
- YouTube channel: "Orpington Close" (UC9lThgvtncw_0adJe8eTI2A)
- Existing videos: V4 (mV-otwDVjnU, unlisted), V3 (rqksBG3EZUo), V2 (DTIb6jJbOwI)
- All pip-discovery API keys available for enrichment stage (Exa, Perplexity, Anthropic, OpenAI, Gemini, DeepSeek, etc.)
- SSH key for Vast.ai VMs: `~/.ssh/vast_v3`
- Vast.ai API key: `${VAST_API_KEY}`
