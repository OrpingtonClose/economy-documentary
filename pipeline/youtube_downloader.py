#!/usr/bin/env python3
"""
YouTube Downloader — Unified Content Acquisition Module
=========================================================
Orchestrates three backends for YouTube content acquisition:
  1. Tube Archivist — self-hosted media server (video + metadata + subs)
  2. Apify — cloud scraping (transcripts, metadata, comments, channel discovery)
  3. Bright Data — residential proxy network for yt-dlp direct downloads

Backend selection follows a priority chain with graceful fallthrough:
  Channel discovery : TA → Apify → yt-dlp --flat-playlist
  Video download    : TA → yt-dlp + Bright Data
  Transcripts       : Apify → yt-dlp subtitles + BD → WhisperX
  Metadata          : TA API → Apify → yt-dlp --dump-json + BD
  Comments          : YouTube Data API → TA → Apify

All downloaded assets are uploaded to B2 at:
  data-youtube/{video_id}/video.mp4
  data-youtube/{video_id}/transcript.json
  data-youtube/{video_id}/metadata.json
  data-youtube/{video_id}/comments.parquet
  data-youtube/{video_id}/thumbnail.jpg

Usage (standalone):
  python3 -m pipeline.youtube_downloader --test-ta
  python3 -m pipeline.youtube_downloader --test-proxy
  python3 -m pipeline.youtube_downloader --test-apify
  python3 -m pipeline.youtube_downloader --sync-channel UC-CHANNEL-ID
  python3 -m pipeline.youtube_downloader --download VIDEO_ID
  python3 -m pipeline.youtube_downloader --dry-run --channels-file channels.txt

Usage (from pipeline):
  from pipeline.youtube_downloader import YouTubeDownloader, DownloaderConfig
  config = DownloaderConfig.from_env()
  dl = YouTubeDownloader(config)
  report = dl.run_full_pipeline(channel_ids=["UC..."])
"""

import argparse
import enum
import io
import json
import logging
import os
import random
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger(__name__)

# Auto-discovery path for Tube Archivist connection details (from Vast.ai deployment)
TA_CONNECTION_FILE = Path(__file__).resolve().parent.parent / "scripts" / "tube-archivist" / "ta_connection.json"


# ===================================================================
# Configuration
# ===================================================================

@dataclass
class DownloaderConfig:
    """Configuration for all YouTube download backends."""

    # Tube Archivist
    tube_archivist_url: str | None = None
    tube_archivist_api_token: str | None = None

    # Apify
    apify_api_token: str | None = None

    # Bright Data
    brightdata_proxy_host: str = "brd.superproxy.io"
    brightdata_proxy_port: str = "33335"
    brightdata_proxy_user: str | None = None
    brightdata_proxy_pass: str | None = None

    # B2
    b2_bucket_name: str = "data-youtube"
    b2_key_id: str | None = None
    b2_app_key: str | None = None

    # Throttling
    sleep_between_downloads: tuple[int, int] = (5, 15)
    max_concurrent_downloads: int = 3
    max_retries: int = 3

    @classmethod
    def from_env(cls) -> "DownloaderConfig":
        """Load config from environment variables, with ta_connection.json fallback."""
        ta_url = os.getenv("TUBE_ARCHIVIST_URL")
        ta_token = os.getenv("TUBE_ARCHIVIST_API_TOKEN")

        # Auto-discover TA connection from Vast.ai deployment file
        if not ta_url and TA_CONNECTION_FILE.exists():
            try:
                conn = json.loads(TA_CONNECTION_FILE.read_text())
                ta_url = conn.get("ta_url", ta_url)
                ta_token = ta_token or conn.get("ta_api_token", "")
                log.info(f"Loaded TA connection from {TA_CONNECTION_FILE}")
            except Exception as e:
                log.warning(f"Failed to read {TA_CONNECTION_FILE}: {e}")

        return cls(
            tube_archivist_url=ta_url,
            tube_archivist_api_token=ta_token,
            apify_api_token=os.getenv("APIFY_API_TOKEN"),
            brightdata_proxy_host=os.getenv("BRIGHTDATA_PROXY_HOST", "brd.superproxy.io"),
            brightdata_proxy_port=os.getenv("BRIGHTDATA_PROXY_PORT", "33335"),
            brightdata_proxy_user=os.getenv("BRIGHTDATA_PROXY_USER"),
            brightdata_proxy_pass=os.getenv("BRIGHTDATA_PROXY_PASS"),
            b2_bucket_name=os.getenv("B2_BUCKET_NAME", "data-youtube"),
            b2_key_id=os.getenv("B2_KEY_ID"),
            b2_app_key=os.getenv("B2_APP_KEY"),
        )


# ===================================================================
# Report dataclasses
# ===================================================================

@dataclass
class SyncReport:
    channels_scanned: int = 0
    new_videos_found: int = 0
    already_cataloged: int = 0
    errors: list[str] = field(default_factory=list)
    backend_used: str = ""


@dataclass
class DownloadReport:
    requested: int = 0
    downloaded: int = 0
    skipped_existing: int = 0
    failed: int = 0
    bytes_transferred: int = 0
    errors: list[str] = field(default_factory=list)
    backend_used: str = ""


@dataclass
class TranscriptReport:
    requested: int = 0
    fetched: int = 0
    skipped_existing: int = 0
    failed: int = 0
    backend_used: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class MetadataReport:
    requested: int = 0
    fetched: int = 0
    skipped_existing: int = 0
    failed: int = 0
    backend_used: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class CommentReport:
    requested: int = 0
    fetched: int = 0
    total_comments: int = 0
    skipped_existing: int = 0
    failed: int = 0
    backend_used: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass
class PipelineReport:
    sync: SyncReport | None = None
    metadata: MetadataReport | None = None
    transcripts: TranscriptReport | None = None
    comments: CommentReport | None = None
    downloads: DownloadReport | None = None
    catalog_updated: bool = False
    duration_seconds: float = 0

    def __str__(self) -> str:
        lines = ["=== YouTube Pipeline Report ==="]
        if self.sync:
            lines.append(
                f"  Sync: {self.sync.new_videos_found} new, "
                f"{self.sync.already_cataloged} existing, "
                f"{len(self.sync.errors)} errors [{self.sync.backend_used}]"
            )
        if self.metadata:
            lines.append(
                f"  Metadata: {self.metadata.fetched}/{self.metadata.requested} fetched, "
                f"{self.metadata.skipped_existing} skipped [{self.metadata.backend_used}]"
            )
        if self.transcripts:
            lines.append(
                f"  Transcripts: {self.transcripts.fetched}/{self.transcripts.requested} fetched, "
                f"{self.transcripts.skipped_existing} skipped [{self.transcripts.backend_used}]"
            )
        if self.comments:
            lines.append(
                f"  Comments: {self.comments.fetched}/{self.comments.requested} fetched, "
                f"{self.comments.total_comments} total [{self.comments.backend_used}]"
            )
        if self.downloads:
            lines.append(
                f"  Downloads: {self.downloads.downloaded}/{self.downloads.requested}, "
                f"{self.downloads.bytes_transferred / 1e6:.1f} MB [{self.downloads.backend_used}]"
            )
        lines.append(f"  Catalog updated: {self.catalog_updated}")
        lines.append(f"  Duration: {self.duration_seconds:.0f}s")
        return "\n".join(lines)


# ===================================================================
# Throttle Manager
# ===================================================================

class ThrottleState(enum.Enum):
    NORMAL = "normal"
    CAUTIOUS = "cautious"
    BACKING_OFF = "backing_off"
    DEFERRED = "deferred"


class ThrottleManager:
    """Intelligent throttling across backends."""

    TUBE_ARCHIVIST_CONCURRENT = 1
    APIFY_CONCURRENT = 5
    BRIGHTDATA_CONCURRENT = 3
    YTDLP_SLEEP_RANGE = (5, 15)

    BACKOFF_MULTIPLIERS = {
        ThrottleState.NORMAL: 1.0,
        ThrottleState.CAUTIOUS: 2.0,
        ThrottleState.BACKING_OFF: 5.0,
        ThrottleState.DEFERRED: 0,  # skip entirely
    }

    def __init__(self):
        self.states: dict[str, ThrottleState] = {}
        self.failure_counts: dict[str, int] = {}

    def get_state(self, video_id: str) -> ThrottleState:
        return self.states.get(video_id, ThrottleState.NORMAL)

    def record_failure(self, video_id: str) -> ThrottleState:
        """Advance throttle state on failure."""
        count = self.failure_counts.get(video_id, 0) + 1
        self.failure_counts[video_id] = count

        if count >= 5:
            state = ThrottleState.DEFERRED
        elif count >= 3:
            state = ThrottleState.BACKING_OFF
        elif count >= 1:
            state = ThrottleState.CAUTIOUS
        else:
            state = ThrottleState.NORMAL

        self.states[video_id] = state
        return state

    def record_success(self, video_id: str) -> None:
        self.states.pop(video_id, None)
        self.failure_counts.pop(video_id, None)

    def get_sleep_time(self, video_id: str, base_range: tuple[int, int] = (5, 15)) -> float:
        """Return sleep time adjusted for throttle state."""
        state = self.get_state(video_id)
        if state == ThrottleState.DEFERRED:
            return -1  # signal to skip
        base = random.uniform(*base_range)
        return base * self.BACKOFF_MULTIPLIERS[state]

    def should_skip(self, video_id: str) -> bool:
        return self.get_state(video_id) == ThrottleState.DEFERRED


# ===================================================================
# B2 Storage Helper
# ===================================================================

class B2Client:
    """Backblaze B2 upload/download helper for youtube data."""

    def __init__(self, config: DownloaderConfig):
        self.bucket_name = config.b2_bucket_name
        self.key_id = config.b2_key_id
        self.app_key = config.b2_app_key
        self._bucket = None

    def _get_bucket(self):
        """Lazy-init B2 bucket connection."""
        if self._bucket is None:
            from b2sdk.v2 import B2Api, InMemoryAccountInfo
            info = InMemoryAccountInfo()
            api = B2Api(info)
            api.authorize_account("production", self.key_id, self.app_key)
            self._bucket = api.get_bucket_by_name(self.bucket_name)
        return self._bucket

    @property
    def available(self) -> bool:
        return bool(self.key_id and self.app_key)

    def file_exists(self, key: str) -> bool:
        """Check if a file exists in B2."""
        if not self.available:
            return False
        try:
            bucket = self._get_bucket()
            bucket.get_file_info_by_name(key)
            return True
        except Exception:
            return False

    def upload_file(self, local_path: Path, remote_key: str) -> int:
        """Upload a local file to B2. Returns bytes uploaded."""
        bucket = self._get_bucket()
        file_info = bucket.upload_local_file(
            local_file=str(local_path),
            file_name=remote_key,
        )
        return file_info.size

    def upload_bytes(self, data: bytes, remote_key: str, content_type: str = "application/octet-stream") -> int:
        """Upload raw bytes to B2."""
        bucket = self._get_bucket()
        file_info = bucket.upload_bytes(
            data_bytes=data,
            file_name=remote_key,
            content_type=content_type,
        )
        return file_info.size

    def download_to_bytes(self, remote_key: str) -> bytes:
        """Download a file from B2 as bytes."""
        bucket = self._get_bucket()
        downloaded = bucket.download_file_by_name(remote_key)
        buf = io.BytesIO()
        downloaded.save(buf)
        return buf.getvalue()

    def download_to_file(self, remote_key: str, local_path: Path) -> Path:
        """Download a file from B2 to local path."""
        bucket = self._get_bucket()
        downloaded = bucket.download_file_by_name(remote_key)
        downloaded.save_to(str(local_path))
        return local_path


# ===================================================================
# Catalog Manager
# ===================================================================

class CatalogManager:
    """Reads/writes catalog.parquet from B2."""

    CATALOG_KEY = "catalog.parquet"

    SCHEMA_COLUMNS = [
        "video_id", "channel_id", "title",
        "has_video", "has_transcript", "has_metadata", "has_comments",
        "video_source", "download_date", "file_size_bytes",
    ]

    def __init__(self, b2: B2Client):
        self.b2 = b2
        self._df = None

    def load(self):
        """Load catalog from B2 or create empty."""
        import pyarrow.parquet as pq
        import pyarrow as pa

        if self.b2.available and self.b2.file_exists(self.CATALOG_KEY):
            log.info("Loading catalog.parquet from B2...")
            data = self.b2.download_to_bytes(self.CATALOG_KEY)
            table = pq.read_table(io.BytesIO(data))
            self._df = table.to_pandas()
            # Ensure new columns exist
            for col in self.SCHEMA_COLUMNS:
                if col not in self._df.columns:
                    if col.startswith("has_"):
                        self._df[col] = False
                    elif col == "file_size_bytes":
                        self._df[col] = 0
                    else:
                        self._df[col] = ""
            log.info(f"Catalog loaded: {len(self._df)} entries")
        else:
            import pandas as pd
            self._df = pd.DataFrame(columns=self.SCHEMA_COLUMNS)
            log.info("Created empty catalog")

    @property
    def df(self):
        if self._df is None:
            self.load()
        return self._df

    def has_video(self, video_id: str) -> bool:
        row = self.df[self.df["video_id"] == video_id]
        return len(row) > 0 and bool(row.iloc[0].get("has_video", False))

    def has_transcript(self, video_id: str) -> bool:
        row = self.df[self.df["video_id"] == video_id]
        return len(row) > 0 and bool(row.iloc[0].get("has_transcript", False))

    def has_metadata(self, video_id: str) -> bool:
        row = self.df[self.df["video_id"] == video_id]
        return len(row) > 0 and bool(row.iloc[0].get("has_metadata", False))

    def has_comments(self, video_id: str) -> bool:
        row = self.df[self.df["video_id"] == video_id]
        return len(row) > 0 and bool(row.iloc[0].get("has_comments", False))

    def is_cataloged(self, video_id: str) -> bool:
        return len(self.df[self.df["video_id"] == video_id]) > 0

    def upsert(self, video_id: str, **kwargs) -> None:
        """Insert or update a catalog entry."""
        import pandas as pd

        mask = self.df["video_id"] == video_id
        if mask.any():
            for key, val in kwargs.items():
                self._df.loc[mask, key] = val
        else:
            row = {"video_id": video_id}
            for col in self.SCHEMA_COLUMNS:
                if col == "video_id":
                    continue
                if col.startswith("has_"):
                    row[col] = False
                elif col == "file_size_bytes":
                    row[col] = 0
                else:
                    row[col] = ""
            row.update(kwargs)
            self._df = pd.concat([self._df, pd.DataFrame([row])], ignore_index=True)

    def save(self) -> None:
        """Upload catalog.parquet back to B2."""
        if not self.b2.available:
            log.warning("B2 not configured — catalog not saved")
            return
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.Table.from_pandas(self.df)
        buf = io.BytesIO()
        pq.write_table(table, buf)
        self.b2.upload_bytes(buf.getvalue(), self.CATALOG_KEY, content_type="application/octet-stream")
        log.info(f"Catalog saved to B2: {len(self.df)} entries")


# ===================================================================
# Tube Archivist Backend
# ===================================================================

class TubeArchivistClient:
    """
    REST client for Tube Archivist API.

    API Authentication: Token-based
      Authorization: Token {api_token}
      Content-Type: application/json

    Key API Endpoints:
      GET  /api/video/{video_id}/          → video metadata
      GET  /api/channel/{channel_id}/      → channel info
      POST /api/channel/                   → subscribe to channel
      GET  /api/download/                  → list download queue
      POST /api/download/                  → add to download queue
      POST /api/task/                      → trigger task (rescan, download)
      GET  /api/task/                      → check task status
    """

    def __init__(self, config: DownloaderConfig):
        self.base_url = config.tube_archivist_url.rstrip("/")
        self.token = config.tube_archivist_api_token
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Token {self.token}",
            "Content-Type": "application/json",
        })

    @classmethod
    def from_connection_file(cls, path: str | Path = None) -> "TubeArchivistClient":
        """Create client from a ta_connection.json file (e.g. from Vast.ai deployment)."""
        path = Path(path) if path else TA_CONNECTION_FILE
        data = json.loads(path.read_text())
        config = DownloaderConfig(
            tube_archivist_url=data["ta_url"],
            tube_archivist_api_token=data["ta_api_token"],
        )
        return cls(config)

    def _get(self, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        resp = self.session.get(url, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp

    def _post(self, path: str, payload: dict = None, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        resp = self.session.post(url, json=payload, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp

    def health_check(self) -> bool:
        """Check if TA is reachable and authenticated."""
        try:
            resp = self._get("/api/ping/")
            return resp.status_code == 200
        except Exception as e:
            log.debug(f"TA health check failed: {e}")
            return False

    def subscribe_channel(self, channel_id: str) -> dict:
        """POST /api/channel/ — subscribe to a channel."""
        payload = {"data": [{"channel_id": channel_id, "channel_subscribed": True}]}
        resp = self._post("/api/channel/", payload)
        return resp.json()

    def get_channel_videos(self, channel_id: str) -> list[str]:
        """Get all video IDs for a channel from TA index."""
        video_ids = []
        page = 1
        while True:
            resp = self._get(f"/api/video/", params={"channel": channel_id, "page": page})
            data = resp.json()
            if not data.get("data"):
                break
            for vid in data["data"]:
                video_ids.append(vid["youtube_id"])
            if not data.get("paginate", {}).get("next"):
                break
            page += 1
        return video_ids

    def add_to_download_queue(self, video_ids: list[str]) -> dict:
        """POST /api/download/ — add video IDs to download queue."""
        payload = {"data": [{"youtube_id": vid, "status": "pending"} for vid in video_ids]}
        resp = self._post("/api/download/", payload)
        return resp.json()

    def start_download(self) -> dict:
        """POST /api/task/ — trigger download of queued items."""
        payload = {"create": {"type": "download"}}
        resp = self._post("/api/task/", payload)
        return resp.json()

    def get_download_status(self) -> dict:
        """GET /api/download/ — check queue status."""
        resp = self._get("/api/download/")
        return resp.json()

    def get_video(self, video_id: str) -> dict | None:
        """GET /api/video/{video_id}/ — get video metadata from TA index."""
        try:
            resp = self._get(f"/api/video/{video_id}/")
            return resp.json()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise

    def poll_until_complete(self, video_ids: list[str], timeout: int = 3600) -> dict:
        """Poll download queue until specified videos are done or timeout."""
        start = time.time()
        completed = set()
        failed = set()

        while time.time() - start < timeout:
            status = self.get_download_status()
            queue = status.get("data", [])

            for item in queue:
                vid = item.get("youtube_id", "")
                if vid in video_ids:
                    if item.get("status") == "downloaded":
                        completed.add(vid)
                    elif item.get("status") == "failed":
                        failed.add(vid)

            # Check videos no longer in queue (already indexed)
            for vid in video_ids:
                if vid not in completed and vid not in failed:
                    info = self.get_video(vid)
                    if info and info.get("data", {}).get("media_url"):
                        completed.add(vid)

            if len(completed) + len(failed) >= len(video_ids):
                break

            time.sleep(15)

        return {"completed": list(completed), "failed": list(failed)}


# ===================================================================
# Apify Backend
# ===================================================================

class ApifyClient:
    """
    Apify integration for transcript/metadata/comment extraction.

    Key Actors:
      bernhardkast/youtube-transcript-scraper  → transcripts
      streamers/youtube-scraper                → metadata + comments
      apify/youtube-scraper                    → channel video discovery
    """

    def __init__(self, config: DownloaderConfig):
        self.api_token = config.apify_api_token
        self.base_url = "https://api.apify.com/v2"

    def _run_actor(self, actor_id: str, run_input: dict, timeout: int = 600) -> list[dict]:
        """Run an Apify actor and return dataset items."""
        # Start the run
        resp = requests.post(
            f"{self.base_url}/acts/{actor_id}/runs",
            params={"token": self.api_token},
            json=run_input,
            timeout=60,
        )
        resp.raise_for_status()
        run_data = resp.json().get("data", {})
        run_id = run_data["id"]
        log.info(f"Apify run started: {actor_id} → {run_id}")

        # Poll until complete
        result = self.poll_run(run_id, timeout=timeout)
        if result.get("status") != "SUCCEEDED":
            raise RuntimeError(f"Apify run {run_id} failed with status: {result.get('status')}")

        # Fetch dataset items
        dataset_id = result.get("defaultDatasetId")
        items_resp = requests.get(
            f"{self.base_url}/datasets/{dataset_id}/items",
            params={"token": self.api_token, "format": "json"},
            timeout=60,
        )
        items_resp.raise_for_status()
        return items_resp.json()

    def poll_run(self, run_id: str, timeout: int = 600) -> dict:
        """Poll actor run until finished."""
        start = time.time()
        while time.time() - start < timeout:
            resp = requests.get(
                f"{self.base_url}/actor-runs/{run_id}",
                params={"token": self.api_token},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            status = data.get("status")
            if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                return data
            time.sleep(10)

        return {"status": "TIMED-OUT", "run_id": run_id}

    def health_check(self) -> bool:
        """Verify Apify API token is valid."""
        try:
            resp = requests.get(
                f"{self.base_url}/users/me",
                params={"token": self.api_token},
                timeout=15,
            )
            return resp.status_code == 200
        except Exception as e:
            log.debug(f"Apify health check failed: {e}")
            return False

    def run_transcript_extractor(self, video_urls: list[str]) -> list[dict]:
        """Run YouTube Transcript Extractor actor."""
        return self._run_actor(
            "bernhardkast~youtube-transcript-scraper",
            {"urls": video_urls},
            timeout=600,
        )

    def run_metadata_scraper(self, video_urls: list[str]) -> list[dict]:
        """Run YouTube Scraper for metadata."""
        return self._run_actor(
            "streamers~youtube-scraper",
            {"startUrls": [{"url": u} for u in video_urls], "maxResults": len(video_urls)},
            timeout=300,
        )

    def run_comment_scraper(self, video_urls: list[str], max_comments: int = 500) -> list[dict]:
        """Run YouTube Comment Scraper."""
        return self._run_actor(
            "streamers~youtube-scraper",
            {
                "startUrls": [{"url": u} for u in video_urls],
                "maxResults": len(video_urls),
                "scrapeComments": True,
                "maxComments": max_comments,
            },
            timeout=600,
        )

    def run_channel_scraper(self, channel_urls: list[str]) -> list[dict]:
        """Discover all videos from channels."""
        return self._run_actor(
            "apify~youtube-scraper",
            {"startUrls": [{"url": u} for u in channel_urls], "maxResults": 9999},
            timeout=900,
        )


# ===================================================================
# Bright Data Backend
# ===================================================================

class BrightDataProxy:
    """
    Bright Data residential proxy integration for yt-dlp.

    Proxy format: http://{user}:{pass}@{host}:{port}
    Features: IP rotation, sticky sessions, country targeting.
    """

    def __init__(self, config: DownloaderConfig):
        self.host = config.brightdata_proxy_host
        self.port = config.brightdata_proxy_port
        self.user = config.brightdata_proxy_user
        self.password = config.brightdata_proxy_pass

    def _build_proxy_url(self, country: str = None, session_id: str = None) -> str:
        """Build proxy URL with optional country targeting and session stickiness."""
        user = self.user
        if session_id:
            user = f"{user}-session-{session_id}"
        if country:
            user = f"{user}-country-{country}"
        return f"http://{user}:{self.password}@{self.host}:{self.port}"

    def get_proxy_dict(self, country: str = None, session_id: str = None) -> dict:
        """Return proxy dict for requests: {'http': url, 'https': url}."""
        url = self._build_proxy_url(country=country, session_id=session_id)
        return {"http": url, "https": url}

    def get_ytdlp_proxy(self, country: str = None, session_id: str = None) -> str:
        """Return proxy string for yt-dlp --proxy flag."""
        return self._build_proxy_url(country=country, session_id=session_id)

    def health_check(self) -> bool:
        """Test proxy connectivity via a lightweight request."""
        try:
            resp = requests.get(
                "https://lumtest.com/myip.json",
                proxies=self.get_proxy_dict(),
                timeout=30,
            )
            data = resp.json()
            log.info(f"Bright Data proxy OK — IP: {data.get('ip')}, Country: {data.get('country')}")
            return resp.status_code == 200
        except Exception as e:
            log.debug(f"Bright Data proxy check failed: {e}")
            return False

    def download_video(self, video_id: str, output_path: Path, format_spec: str = "best") -> Path:
        """Download video via yt-dlp with Bright Data proxy rotation."""
        import yt_dlp

        session_id = f"dl-{video_id}-{random.randint(1000, 9999)}"
        ydl_opts = {
            "proxy": self.get_ytdlp_proxy(session_id=session_id),
            "format": format_spec,
            "outtmpl": str(output_path / f"{video_id}.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
            "fragment_retries": 3,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

        # Find the downloaded file
        for f in output_path.iterdir():
            if f.stem == video_id and f.suffix in (".mp4", ".webm", ".mkv"):
                return f

        raise FileNotFoundError(f"Downloaded file not found for {video_id} in {output_path}")

    def extract_metadata(self, video_id: str) -> dict:
        """Extract metadata via yt-dlp --dump-json with proxy."""
        import yt_dlp

        session_id = f"meta-{video_id}-{random.randint(1000, 9999)}"
        ydl_opts = {
            "proxy": self.get_ytdlp_proxy(session_id=session_id),
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            return info

    def extract_subtitles(self, video_id: str, output_path: Path) -> Path | None:
        """Extract subtitles via yt-dlp with proxy."""
        import yt_dlp

        session_id = f"sub-{video_id}-{random.randint(1000, 9999)}"
        sub_file = output_path / f"{video_id}.en.vtt"
        ydl_opts = {
            "proxy": self.get_ytdlp_proxy(session_id=session_id),
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en"],
            "subtitlesformat": "vtt",
            "skip_download": True,
            "outtmpl": str(output_path / f"{video_id}"),
            "quiet": True,
            "no_warnings": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

        if sub_file.exists():
            return sub_file
        # Check for auto-generated subs
        auto_sub = output_path / f"{video_id}.en.vtt"
        return auto_sub if auto_sub.exists() else None


# ===================================================================
# Helper: yt-dlp flat playlist (no proxy)
# ===================================================================

def ytdlp_flat_playlist(channel_url: str) -> list[str]:
    """Use yt-dlp to list all video IDs from a channel (flat playlist, no download)."""
    import yt_dlp

    video_ids = []
    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)
        entries = info.get("entries", []) if info else []
        for entry in entries:
            vid = entry.get("id") or entry.get("url", "").split("=")[-1]
            if vid:
                video_ids.append(vid)

    return video_ids


# ===================================================================
# Helper: retry with exponential backoff
# ===================================================================

def _retry(fn, max_retries: int = 3, base_delay: float = 2.0, label: str = ""):
    """Execute fn with exponential backoff retries."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            log.warning(f"[{label}] Attempt {attempt + 1}/{max_retries} failed: {e} — retrying in {delay:.1f}s")
            time.sleep(delay)
    raise last_exc


# ===================================================================
# YouTubeDownloader Facade
# ===================================================================

class YouTubeDownloader:
    """Unified YouTube content acquisition with three backends."""

    def __init__(self, config: DownloaderConfig):
        self.config = config
        self.b2 = B2Client(config)
        self.catalog = CatalogManager(self.b2)
        self.throttle = ThrottleManager()

        # Initialize available backends
        self.ta_client = TubeArchivistClient(config) if config.tube_archivist_url else None
        self.apify_client = ApifyClient(config) if config.apify_api_token else None
        self.brightdata = BrightDataProxy(config) if config.brightdata_proxy_user else None

        # Verify backends on init
        self._ta_available = False
        self._apify_available = False
        self._bd_available = False

    def _check_backends(self) -> None:
        """Probe each backend for availability."""
        if self.ta_client:
            self._ta_available = self.ta_client.health_check()
            log.info(f"Tube Archivist: {'available' if self._ta_available else 'unavailable'}")

        if self.apify_client:
            self._apify_available = self.apify_client.health_check()
            log.info(f"Apify: {'available' if self._apify_available else 'unavailable'}")

        if self.brightdata:
            self._bd_available = self.brightdata.health_check()
            log.info(f"Bright Data: {'available' if self._bd_available else 'unavailable'}")

    def _video_url(self, video_id: str) -> str:
        return f"https://www.youtube.com/watch?v={video_id}"

    def _channel_url(self, channel_id: str) -> str:
        return f"https://www.youtube.com/channel/{channel_id}"

    # === HIGH-LEVEL PIPELINE METHODS ===

    def sync_channels(self, channel_ids: list[str]) -> SyncReport:
        """
        Discover new videos across all channels.
        Priority: TA → Apify → yt-dlp --flat-playlist
        """
        report = SyncReport()

        self._check_backends()

        all_video_ids = []

        for ch_id in channel_ids:
            report.channels_scanned += 1
            try:
                video_ids = self._sync_one_channel(ch_id)
                all_video_ids.extend(video_ids)
            except Exception as e:
                report.errors.append(f"channel {ch_id}: {e}")
                log.error(f"Failed to sync channel {ch_id}: {e}")

        # De-duplicate and check catalog
        seen = set()
        new_ids = []
        for vid in all_video_ids:
            if vid in seen:
                continue
            seen.add(vid)
            if self.catalog.is_cataloged(vid):
                report.already_cataloged += 1
            else:
                new_ids.append(vid)
                self.catalog.upsert(vid, channel_id="", has_video=False)

        report.new_videos_found = len(new_ids)
        log.info(f"Sync complete: {report.new_videos_found} new, {report.already_cataloged} existing")
        return report

    def _sync_one_channel(self, channel_id: str) -> list[str]:
        """Sync a single channel using best available backend."""
        # Priority 1: Tube Archivist
        if self._ta_available:
            try:
                log.info(f"Syncing channel {channel_id} via Tube Archivist")
                self.ta_client.subscribe_channel(channel_id)
                video_ids = self.ta_client.get_channel_videos(channel_id)
                if video_ids:
                    return video_ids
            except Exception as e:
                log.warning(f"TA sync failed for {channel_id}: {e}")

        # Priority 2: Apify
        if self._apify_available:
            try:
                log.info(f"Syncing channel {channel_id} via Apify")
                items = self.apify_client.run_channel_scraper([self._channel_url(channel_id)])
                video_ids = [item.get("id") or item.get("videoId", "") for item in items if item.get("id") or item.get("videoId")]
                if video_ids:
                    return video_ids
            except Exception as e:
                log.warning(f"Apify sync failed for {channel_id}: {e}")

        # Priority 3: yt-dlp flat playlist
        log.info(f"Syncing channel {channel_id} via yt-dlp flat playlist")
        return _retry(
            lambda: ytdlp_flat_playlist(self._channel_url(channel_id)),
            max_retries=self.config.max_retries,
            label=f"yt-dlp-flat/{channel_id}",
        )

    def download_videos(self, video_ids: list[str], priority: str = "normal") -> DownloadReport:
        """
        Download actual video files.
        Priority: TA → yt-dlp + Bright Data
        Uploads to B2: data-youtube/{video_id}/video.mp4
        """
        report = DownloadReport(requested=len(video_ids))

        self._check_backends()

        for video_id in video_ids:
            if self.throttle.should_skip(video_id):
                report.failed += 1
                report.errors.append(f"{video_id}: deferred (too many failures)")
                continue

            # Check if already downloaded
            b2_key = f"{video_id}/video.mp4"
            if self.catalog.has_video(video_id) or (self.b2.available and self.b2.file_exists(b2_key)):
                report.skipped_existing += 1
                log.info(f"Skipping {video_id} — already downloaded")
                continue

            try:
                nbytes, source = self._download_one_video(video_id)
                report.downloaded += 1
                report.bytes_transferred += nbytes
                report.backend_used = source
                self.throttle.record_success(video_id)
                self.catalog.upsert(
                    video_id,
                    has_video=True,
                    video_source=source,
                    download_date=time.strftime("%Y-%m-%d"),
                    file_size_bytes=nbytes,
                )
            except Exception as e:
                report.failed += 1
                report.errors.append(f"{video_id}: {e}")
                self.throttle.record_failure(video_id)
                log.error(f"Failed to download {video_id}: {e}")

            # Throttle between downloads
            sleep_time = self.throttle.get_sleep_time(video_id, self.config.sleep_between_downloads)
            if sleep_time > 0:
                time.sleep(sleep_time)

        log.info(f"Download complete: {report.downloaded}/{report.requested}")
        return report

    def _download_one_video(self, video_id: str) -> tuple[int, str]:
        """Download a single video using best available backend. Returns (bytes, source)."""
        # Priority 1: Tube Archivist
        if self._ta_available:
            try:
                log.info(f"Downloading {video_id} via Tube Archivist")
                self.ta_client.add_to_download_queue([video_id])
                self.ta_client.start_download()
                result = self.ta_client.poll_until_complete([video_id], timeout=1800)

                if video_id in result.get("completed", []):
                    # Get video info from TA to find local file
                    info = self.ta_client.get_video(video_id)
                    if info and info.get("data", {}).get("media_url"):
                        # TA stores files locally — upload to B2
                        media_url = f"{self.ta_client.base_url}{info['data']['media_url']}"
                        resp = self.ta_client.session.get(media_url, stream=True, timeout=300)
                        resp.raise_for_status()

                        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                            for chunk in resp.iter_content(chunk_size=8192):
                                tmp.write(chunk)
                            tmp_path = Path(tmp.name)

                        nbytes = tmp_path.stat().st_size
                        if self.b2.available:
                            self.b2.upload_file(tmp_path, f"{video_id}/video.mp4")
                        tmp_path.unlink(missing_ok=True)
                        return nbytes, "tube_archivist"
            except Exception as e:
                log.warning(f"TA download failed for {video_id}: {e}")

        # Priority 2: yt-dlp + Bright Data
        if self._bd_available:
            log.info(f"Downloading {video_id} via yt-dlp + Bright Data")
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir)

                def _do_download():
                    return self.brightdata.download_video(video_id, tmp_path)

                video_file = _retry(
                    _do_download,
                    max_retries=self.config.max_retries,
                    label=f"bd-dl/{video_id}",
                )

                nbytes = video_file.stat().st_size
                if self.b2.available:
                    self.b2.upload_file(video_file, f"{video_id}/video.mp4")
                return nbytes, "brightdata"

        # Priority 3: yt-dlp without proxy (last resort)
        log.info(f"Downloading {video_id} via yt-dlp (no proxy)")
        import yt_dlp

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            ydl_opts = {
                "format": "best",
                "outtmpl": str(tmp_path / f"{video_id}.%(ext)s"),
                "quiet": True,
                "no_warnings": True,
                "retries": 3,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([self._video_url(video_id)])

            for f in tmp_path.iterdir():
                if f.stem == video_id:
                    nbytes = f.stat().st_size
                    if self.b2.available:
                        self.b2.upload_file(f, f"{video_id}/video.mp4")
                    return nbytes, "yt-dlp"

        raise RuntimeError(f"No backend could download {video_id}")

    def fetch_transcripts(self, video_ids: list[str]) -> TranscriptReport:
        """
        Get transcripts for videos.
        Priority: Apify → yt-dlp subtitles + BD → WhisperX
        """
        report = TranscriptReport(requested=len(video_ids))

        self._check_backends()

        # Filter out already-fetched
        to_fetch = []
        for vid in video_ids:
            b2_key = f"{vid}/transcript.json"
            if self.catalog.has_transcript(vid) or (self.b2.available and self.b2.file_exists(b2_key)):
                report.skipped_existing += 1
            else:
                to_fetch.append(vid)

        if not to_fetch:
            log.info("All transcripts already fetched")
            return report

        # Priority 1: Apify batch
        if self._apify_available:
            try:
                log.info(f"Fetching {len(to_fetch)} transcripts via Apify")
                urls = [self._video_url(vid) for vid in to_fetch]
                items = self.apify_client.run_transcript_extractor(urls)
                report.backend_used = "apify"

                fetched_ids = set()
                for item in items:
                    vid = item.get("videoId") or item.get("id", "")
                    if not vid:
                        continue
                    transcript_data = json.dumps(item, ensure_ascii=False)
                    if self.b2.available:
                        self.b2.upload_bytes(
                            transcript_data.encode("utf-8"),
                            f"{vid}/transcript.json",
                            content_type="application/json",
                        )
                    self.catalog.upsert(vid, has_transcript=True)
                    fetched_ids.add(vid)
                    report.fetched += 1

                to_fetch = [v for v in to_fetch if v not in fetched_ids]
            except Exception as e:
                log.warning(f"Apify transcript batch failed: {e}")
                report.errors.append(f"apify_batch: {e}")

        # Priority 2: yt-dlp subtitles + Bright Data (for remaining)
        if to_fetch and self._bd_available:
            for vid in list(to_fetch):
                try:
                    with tempfile.TemporaryDirectory() as tmpdir:
                        sub_path = self.brightdata.extract_subtitles(vid, Path(tmpdir))
                        if sub_path and sub_path.exists():
                            transcript_data = json.dumps({
                                "videoId": vid,
                                "source": "yt-dlp-subtitles",
                                "text": sub_path.read_text(encoding="utf-8"),
                            })
                            if self.b2.available:
                                self.b2.upload_bytes(
                                    transcript_data.encode("utf-8"),
                                    f"{vid}/transcript.json",
                                    content_type="application/json",
                                )
                            self.catalog.upsert(vid, has_transcript=True)
                            report.fetched += 1
                            to_fetch.remove(vid)
                            if not report.backend_used:
                                report.backend_used = "yt-dlp+brightdata"
                except Exception as e:
                    log.warning(f"yt-dlp subtitle extraction failed for {vid}: {e}")
                    report.errors.append(f"{vid}/yt-dlp-sub: {e}")

        # Remaining failures
        report.failed = len(to_fetch)
        if to_fetch:
            log.warning(f"{len(to_fetch)} transcripts could not be fetched (WhisperX would require video files)")
            for vid in to_fetch:
                report.errors.append(f"{vid}: no transcript source available")

        log.info(f"Transcripts: {report.fetched}/{report.requested} fetched")
        return report

    def fetch_metadata(self, video_ids: list[str]) -> MetadataReport:
        """
        Get video metadata.
        Priority: TA API → Apify → yt-dlp --dump-json + BD
        """
        report = MetadataReport(requested=len(video_ids))

        self._check_backends()

        to_fetch = []
        for vid in video_ids:
            b2_key = f"{vid}/metadata.json"
            if self.catalog.has_metadata(vid) or (self.b2.available and self.b2.file_exists(b2_key)):
                report.skipped_existing += 1
            else:
                to_fetch.append(vid)

        if not to_fetch:
            log.info("All metadata already fetched")
            return report

        # Priority 1: Tube Archivist API
        if self._ta_available:
            for vid in list(to_fetch):
                try:
                    info = self.ta_client.get_video(vid)
                    if info and info.get("data"):
                        metadata = json.dumps(info["data"], ensure_ascii=False)
                        if self.b2.available:
                            self.b2.upload_bytes(
                                metadata.encode("utf-8"),
                                f"{vid}/metadata.json",
                                content_type="application/json",
                            )
                        self.catalog.upsert(vid, has_metadata=True, title=info["data"].get("title", ""))
                        report.fetched += 1
                        report.backend_used = "tube_archivist"
                        to_fetch.remove(vid)
                except Exception as e:
                    log.warning(f"TA metadata failed for {vid}: {e}")

        # Priority 2: Apify metadata scraper (batch)
        if to_fetch and self._apify_available:
            try:
                log.info(f"Fetching {len(to_fetch)} metadata via Apify")
                urls = [self._video_url(vid) for vid in to_fetch]
                items = self.apify_client.run_metadata_scraper(urls)

                fetched_ids = set()
                for item in items:
                    vid = item.get("id") or item.get("videoId", "")
                    if not vid:
                        continue
                    metadata = json.dumps(item, ensure_ascii=False)
                    if self.b2.available:
                        self.b2.upload_bytes(
                            metadata.encode("utf-8"),
                            f"{vid}/metadata.json",
                            content_type="application/json",
                        )
                    self.catalog.upsert(vid, has_metadata=True, title=item.get("title", ""))
                    fetched_ids.add(vid)
                    report.fetched += 1

                to_fetch = [v for v in to_fetch if v not in fetched_ids]
                if not report.backend_used:
                    report.backend_used = "apify"
            except Exception as e:
                log.warning(f"Apify metadata batch failed: {e}")
                report.errors.append(f"apify_batch: {e}")

        # Priority 3: yt-dlp --dump-json + Bright Data
        if to_fetch and self._bd_available:
            for vid in list(to_fetch):
                try:
                    info = self.brightdata.extract_metadata(vid)
                    if info:
                        metadata = json.dumps(info, ensure_ascii=False, default=str)
                        if self.b2.available:
                            self.b2.upload_bytes(
                                metadata.encode("utf-8"),
                                f"{vid}/metadata.json",
                                content_type="application/json",
                            )
                        self.catalog.upsert(vid, has_metadata=True, title=info.get("title", ""))
                        report.fetched += 1
                        to_fetch.remove(vid)
                        if not report.backend_used:
                            report.backend_used = "yt-dlp+brightdata"
                except Exception as e:
                    log.warning(f"yt-dlp metadata failed for {vid}: {e}")
                    report.errors.append(f"{vid}/yt-dlp: {e}")

        report.failed = len(to_fetch)
        log.info(f"Metadata: {report.fetched}/{report.requested} fetched")
        return report

    def fetch_comments(self, video_ids: list[str], max_per_video: int = 500) -> CommentReport:
        """
        Get comments for videos.
        Priority: TA → Apify Comment Scraper
        """
        report = CommentReport(requested=len(video_ids))

        self._check_backends()

        to_fetch = []
        for vid in video_ids:
            b2_key = f"{vid}/comments.parquet"
            if self.catalog.has_comments(vid) or (self.b2.available and self.b2.file_exists(b2_key)):
                report.skipped_existing += 1
            else:
                to_fetch.append(vid)

        if not to_fetch:
            log.info("All comments already fetched")
            return report

        # Priority 1: Tube Archivist (if it has the video indexed with comments)
        if self._ta_available:
            for vid in list(to_fetch):
                try:
                    info = self.ta_client.get_video(vid)
                    if info and info.get("data", {}).get("comments"):
                        comments = info["data"]["comments"]
                        self._upload_comments(vid, comments)
                        report.fetched += 1
                        report.total_comments += len(comments)
                        report.backend_used = "tube_archivist"
                        to_fetch.remove(vid)
                except Exception as e:
                    log.debug(f"TA comments not available for {vid}: {e}")

        # Priority 2: Apify Comment Scraper (batch)
        if to_fetch and self._apify_available:
            try:
                log.info(f"Fetching comments for {len(to_fetch)} videos via Apify")
                urls = [self._video_url(vid) for vid in to_fetch]
                items = self.apify_client.run_comment_scraper(urls, max_comments=max_per_video)

                # Group comments by video
                by_video: dict[str, list] = {}
                for item in items:
                    vid = item.get("videoId") or item.get("id", "")
                    if vid:
                        by_video.setdefault(vid, []).append(item)

                for vid, comments in by_video.items():
                    self._upload_comments(vid, comments)
                    report.fetched += 1
                    report.total_comments += len(comments)
                    if vid in to_fetch:
                        to_fetch.remove(vid)

                if not report.backend_used:
                    report.backend_used = "apify"
            except Exception as e:
                log.warning(f"Apify comment batch failed: {e}")
                report.errors.append(f"apify_batch: {e}")

        report.failed = len(to_fetch)
        log.info(f"Comments: {report.fetched}/{report.requested} fetched, {report.total_comments} total")
        return report

    def _upload_comments(self, video_id: str, comments: list[dict]) -> None:
        """Convert comments list to parquet and upload to B2."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        if not comments:
            return

        table = pa.Table.from_pylist(comments)
        buf = io.BytesIO()
        pq.write_table(table, buf)

        if self.b2.available:
            self.b2.upload_bytes(buf.getvalue(), f"{video_id}/comments.parquet")
        self.catalog.upsert(video_id, has_comments=True)

    def run_full_pipeline(self, channel_ids: list[str] = None) -> PipelineReport:
        """
        Full acquisition pipeline:
        1. sync_channels → discover new videos
        2. fetch_metadata → for all new videos
        3. fetch_transcripts → for all new videos
        4. fetch_comments → for all new videos
        5. download_videos → only if video files needed
        6. Update catalog.parquet on B2
        """
        start = time.time()
        report = PipelineReport()

        log.info("=" * 60)
        log.info("YouTube Full Pipeline — starting")
        log.info("=" * 60)

        # Determine which videos to process
        new_video_ids = []

        if channel_ids:
            # Step 1: Sync channels
            report.sync = self.sync_channels(channel_ids)
            # Collect all uncatalogued video IDs
            for vid_row in self.catalog.df.itertuples():
                if not getattr(vid_row, "has_metadata", False):
                    new_video_ids.append(vid_row.video_id)
        else:
            # No channels specified — process all videos missing data
            for vid_row in self.catalog.df.itertuples():
                if not (
                    getattr(vid_row, "has_metadata", False)
                    and getattr(vid_row, "has_transcript", False)
                ):
                    new_video_ids.append(vid_row.video_id)

        if new_video_ids:
            log.info(f"Processing {len(new_video_ids)} videos")

            # Step 2: Metadata
            report.metadata = self.fetch_metadata(new_video_ids)

            # Step 3: Transcripts
            report.transcripts = self.fetch_transcripts(new_video_ids)

            # Step 4: Comments
            report.comments = self.fetch_comments(new_video_ids)

            # Step 5: Video downloads (only for videos needing files)
            need_video = [
                vid for vid in new_video_ids
                if not self.catalog.has_video(vid)
            ]
            if need_video:
                report.downloads = self.download_videos(need_video)
        else:
            log.info("No new videos to process")

        # Step 6: Save catalog
        try:
            self.catalog.save()
            report.catalog_updated = True
        except Exception as e:
            log.error(f"Failed to save catalog: {e}")

        report.duration_seconds = time.time() - start
        log.info(f"\n{report}")
        return report


# ===================================================================
# Standalone CLI
# ===================================================================

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="YouTube Downloader — standalone CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 -m pipeline.youtube_downloader --test-ta
  python3 -m pipeline.youtube_downloader --test-proxy
  python3 -m pipeline.youtube_downloader --test-apify
  python3 -m pipeline.youtube_downloader --sync-channel CHANNEL_ID
  python3 -m pipeline.youtube_downloader --download VIDEO_ID
  python3 -m pipeline.youtube_downloader --dry-run --channels-file channels.txt
        """,
    )

    # Test flags
    parser.add_argument("--test-ta", action="store_true", help="Test Tube Archivist connection")
    parser.add_argument("--test-proxy", action="store_true", help="Test Bright Data proxy")
    parser.add_argument("--test-apify", action="store_true", help="Test Apify connection")

    # Operations
    parser.add_argument("--sync-channel", metavar="CHANNEL_ID", help="Sync a single channel")
    parser.add_argument("--download", metavar="VIDEO_ID", help="Download a specific video")
    parser.add_argument("--channels-file", metavar="FILE", help="File with channel IDs (one per line)")
    parser.add_argument("--video-ids", metavar="IDS", help="Comma-separated video IDs")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without downloading")

    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()

    config = DownloaderConfig.from_env()

    # --- Test commands ---
    if args.test_ta:
        if not config.tube_archivist_url:
            log.error("TUBE_ARCHIVIST_URL not set")
            return
        client = TubeArchivistClient(config)
        ok = client.health_check()
        print(f"Tube Archivist: {'OK' if ok else 'FAILED'}")
        return

    if args.test_proxy:
        if not config.brightdata_proxy_user:
            log.error("BRIGHTDATA_PROXY_USER not set")
            return
        proxy = BrightDataProxy(config)
        ok = proxy.health_check()
        print(f"Bright Data proxy: {'OK' if ok else 'FAILED'}")
        return

    if args.test_apify:
        if not config.apify_api_token:
            log.error("APIFY_API_TOKEN not set")
            return
        client = ApifyClient(config)
        ok = client.health_check()
        print(f"Apify: {'OK' if ok else 'FAILED'}")
        return

    # --- Operations ---
    dl = YouTubeDownloader(config)

    if args.sync_channel:
        if args.dry_run:
            log.info(f"[DRY RUN] Would sync channel: {args.sync_channel}")
            return
        report = dl.sync_channels([args.sync_channel])
        print(f"Sync: {report.new_videos_found} new videos, {report.already_cataloged} existing")
        return

    if args.download:
        if args.dry_run:
            log.info(f"[DRY RUN] Would download video: {args.download}")
            return
        report = dl.download_videos([args.download])
        print(f"Download: {report.downloaded} completed, {report.failed} failed")
        return

    if args.video_ids:
        vids = [v.strip() for v in args.video_ids.split(",") if v.strip()]
        if args.dry_run:
            log.info(f"[DRY RUN] Would download {len(vids)} videos: {vids}")
            return
        report = dl.download_videos(vids)
        print(f"Download: {report.downloaded}/{report.requested}")
        return

    if args.channels_file:
        channels = Path(args.channels_file).read_text().strip().splitlines()
        channels = [ch.strip() for ch in channels if ch.strip() and not ch.strip().startswith("#")]
        if args.dry_run:
            log.info(f"[DRY RUN] Would run full pipeline for {len(channels)} channels:")
            for ch in channels:
                log.info(f"  - {ch}")
            return
        report = dl.run_full_pipeline(channel_ids=channels)
        print(report)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
