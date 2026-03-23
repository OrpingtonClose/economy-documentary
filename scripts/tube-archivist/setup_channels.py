#!/usr/bin/env python3
"""
Subscribe Tube Archivist to channels and configure download settings.

Reads channel IDs/URLs from a file and subscribes via the TA REST API.
Optionally triggers rescan and download.

Usage:
    python3 scripts/tube-archivist/setup_channels.py --channels channels.txt
    python3 scripts/tube-archivist/setup_channels.py --channels channels.txt --start-download
    python3 scripts/tube-archivist/setup_channels.py --ta-url http://host:8000 --ta-token TOKEN --channels channels.txt
"""

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONNECTION_FILE = SCRIPT_DIR / "ta_connection.json"


class TubeArchivistAPI:
    """Minimal client for the Tube Archivist REST API."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(self, method: str, path: str, data: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        body = json.dumps(data).encode() if data else None
        req = Request(url, data=body, method=method)
        req.add_header("Authorization", f"Token {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            error_body = e.read().decode() if e.fp else ""
            raise RuntimeError(
                f"{method} {path} → {e.code}: {error_body}"
            ) from e
        except URLError as e:
            raise RuntimeError(f"Connection error: {e.reason}") from e

    def get(self, path: str) -> dict:
        return self._request("GET", path)

    def post(self, path: str, data: dict) -> dict:
        return self._request("POST", path, data)

    def subscribe_channel(self, channel_id: str) -> dict:
        """Subscribe to a YouTube channel."""
        return self.post("/api/channel/", {"data": [{"channel_id": channel_id, "channel_subscribed": True}]})

    def trigger_rescan(self) -> dict:
        """Trigger a rescan of subscribed channels."""
        return self.post("/api/task/", {"rescan_subscriptions": True})

    def trigger_download(self) -> dict:
        """Start downloading pending videos."""
        return self.post("/api/task/", {"download_pending": True})

    def configure_settings(self, settings: dict) -> dict:
        """Update TA download settings."""
        return self.post("/api/config/", settings)

    def ping(self) -> bool:
        """Check if TA is reachable."""
        try:
            self.get("/api/ping/")
            return True
        except Exception:
            return False


def load_channels(path: str) -> list[str]:
    """Load channel IDs from a file (one per line, comments allowed)."""
    channels = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Handle full URLs: extract channel ID
            if "youtube.com/channel/" in line:
                channel_id = line.split("youtube.com/channel/")[1].split("/")[0].split("?")[0]
            elif "youtube.com/@" in line:
                # @handle — pass through, TA can resolve these
                channel_id = line.split("youtube.com/")[1].split("/")[0].split("?")[0]
            else:
                channel_id = line
            channels.append(channel_id)
    return channels


def load_connection(path: Path) -> tuple[str, str]:
    """Load TA URL and token from connection file."""
    data = json.loads(path.read_text())
    url = data.get("ta_url")
    token = data.get("ta_api_token")
    if not url or not token:
        print(f"Connection file missing ta_url or ta_api_token: {path}", file=sys.stderr)
        sys.exit(1)
    return url, token


def subscribe_channels(api: TubeArchivistAPI, channels: list[str]) -> None:
    """Subscribe to all channels, reporting progress."""
    total = len(channels)
    success = 0
    failed = []

    for i, channel_id in enumerate(channels, 1):
        try:
            api.subscribe_channel(channel_id)
            print(f"  [{i}/{total}] Subscribed: {channel_id}")
            success += 1
        except Exception as e:
            print(f"  [{i}/{total}] FAILED: {channel_id} — {e}")
            failed.append(channel_id)
        # Brief pause to avoid hammering the API
        if i < total:
            time.sleep(0.5)

    print(f"\nResults: {success}/{total} subscribed, {len(failed)} failed")
    if failed:
        print("Failed channels:")
        for ch in failed:
            print(f"  - {ch}")


def configure_ta(api: TubeArchivistAPI) -> None:
    """Configure TA download settings for best quality."""
    print("Configuring download settings...")
    settings = {
        "downloads": {
            "sleep_interval": 10,
            "auto_start": True,
            "format": "bestvideo+bestaudio/best",
        }
    }
    try:
        api.configure_settings(settings)
        print("  Settings applied.")
    except Exception as e:
        print(f"  Warning: could not apply settings: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Subscribe Tube Archivist to YouTube channels",
    )
    parser.add_argument("--channels", required=True,
                        help="Path to file with channel IDs (one per line)")
    parser.add_argument("--ta-url",
                        help="Tube Archivist URL (default: from ta_connection.json)")
    parser.add_argument("--ta-token",
                        help="Tube Archivist API token (default: from ta_connection.json)")
    parser.add_argument("--connection-file", type=Path, default=DEFAULT_CONNECTION_FILE,
                        help="Path to connection JSON file")
    parser.add_argument("--start-download", action="store_true",
                        help="Trigger rescan + download after subscribing")
    parser.add_argument("--rescan-only", action="store_true",
                        help="Only trigger rescan, no download")
    args = parser.parse_args()

    # Resolve TA connection
    if args.ta_url and args.ta_token:
        ta_url, ta_token = args.ta_url, args.ta_token
    else:
        ta_url, ta_token = load_connection(args.connection_file)

    api = TubeArchivistAPI(ta_url, ta_token)

    # Verify connectivity
    if not api.ping():
        print(f"Cannot reach Tube Archivist at {ta_url}", file=sys.stderr)
        sys.exit(1)
    print(f"Connected to Tube Archivist at {ta_url}")

    # Load channels
    channels = load_channels(args.channels)
    if not channels:
        print("No channels found in the input file.", file=sys.stderr)
        sys.exit(1)
    print(f"Found {len(channels)} channels to subscribe")

    # Configure settings
    configure_ta(api)

    # Subscribe
    subscribe_channels(api, channels)

    # Trigger tasks
    if args.start_download or args.rescan_only:
        print("\nTriggering rescan of subscribed channels...")
        try:
            api.trigger_rescan()
            print("  Rescan started.")
        except Exception as e:
            print(f"  Rescan failed: {e}")

    if args.start_download:
        print("Triggering download of pending videos...")
        try:
            api.trigger_download()
            print("  Download started.")
        except Exception as e:
            print(f"  Download trigger failed: {e}")

    print("\nDone.")


if __name__ == "__main__":
    main()
