#!/usr/bin/env python3
"""
Frame.io V4 uploader for generated clips.
Handles token refresh, file creation, chunked upload, metadata embedding, and comments.
Designed to run on the generation VMs alongside B2 uploads.
"""
import json
import os
import sys
import time
import logging
import subprocess
import requests
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [FRAMEIO] %(message)s")
log = logging.getLogger(__name__)

# Config
TOKENS_FILE = Path("/root/frameio_tokens.json")
CLIENT_ID = "${FRAMEIO_CLIENT_ID}"
CLIENT_SECRET = "${FRAMEIO_CLIENT_SECRET}"
ACCOUNT_ID = "${FRAMEIO_ACCOUNT_ID}"
CLIPS_FOLDER_ID = "06216ba5-fce7-47b3-b976-844f94aaf242"
IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
FRAMEIO_API = "https://api.frame.io/v4"

# Chunk size for uploads (25MB)
CHUNK_SIZE = 25 * 1024 * 1024


def load_tokens() -> dict:
    if TOKENS_FILE.exists():
        return json.loads(TOKENS_FILE.read_text())
    return {}


def save_tokens(tokens: dict):
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))


def refresh_token() -> str:
    """Refresh the access token using the refresh token."""
    tokens = load_tokens()
    rt = tokens.get("refresh_token")
    if not rt:
        log.error("No refresh token available!")
        return tokens.get("access_token", "")

    resp = requests.post(IMS_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": rt,
    })

    if resp.status_code == 200:
        new_tokens = resp.json()
        # Preserve refresh_token if not in response
        if "refresh_token" not in new_tokens and rt:
            new_tokens["refresh_token"] = rt
        new_tokens["refreshed_at"] = time.time()
        save_tokens(new_tokens)
        log.info("Token refreshed successfully")
        return new_tokens["access_token"]
    else:
        log.error(f"Token refresh failed: {resp.status_code} {resp.text[:200]}")
        return tokens.get("access_token", "")


def get_valid_token() -> str:
    """Get a valid access token, refreshing if needed."""
    tokens = load_tokens()
    if not tokens:
        log.error("No tokens file found!")
        return ""

    # Check if token is expired or about to expire (5 min buffer)
    refreshed_at = tokens.get("refreshed_at", tokens.get("created_at", 0))
    expires_in = tokens.get("expires_in", 3599)

    if isinstance(refreshed_at, str):
        refreshed_at = 0

    if time.time() - refreshed_at > expires_in - 300:
        log.info("Token expired or expiring soon, refreshing...")
        return refresh_token()

    return tokens["access_token"]


def embed_metadata_in_mp4(input_path: str, metadata: dict) -> str:
    """Embed production metadata as MP4 tags using ffmpeg.
    
    Frame.io auto-reads standard MP4 metadata fields:
    - title, description, comment, artist, genre, copyright
    
    Args:
        input_path: Path to the source MP4
        metadata: Production metadata dict
        
    Returns:
        Path to the metadata-embedded MP4 (overwrites input)
    """
    path = Path(input_path)
    if not path.exists():
        log.warning(f"Cannot embed metadata - file not found: {input_path}")
        return input_path
    
    tmp_path = str(path.with_suffix('.tmp.mp4'))
    
    # Build human-readable description
    desc_parts = []
    if metadata.get("act"):
        desc_parts.append(f"Act: {metadata['act']}")
    if metadata.get("narration"):
        narr = metadata["narration"]
        if len(narr) > 200:
            narr = narr[:197] + "..."
        desc_parts.append(f"Narration: {narr}")
    if metadata.get("narr_start") is not None and metadata.get("narr_end") is not None:
        desc_parts.append(f"Timeline: {metadata['narr_start']:.1f}s - {metadata['narr_end']:.1f}s")
    description = " | ".join(desc_parts) if desc_parts else ""
    
    # Build generation info for comment field
    gen = metadata.get("generation", {})
    comment_data = {
        "model": gen.get("model", "LTX-2.3-22B-dev"),
        "checkpoint": gen.get("checkpoint", "ltx-2.3-22b-dev.safetensors"),
        "resolution": gen.get("resolution", "768x512"),
        "fps": gen.get("fps", 24),
        "steps": gen.get("denoising_steps", 30),
        "cfg_video": gen.get("cfg_scale_video", 3.0),
        "cfg_audio": gen.get("cfg_scale_audio", 7.0),
        "stg_scale": gen.get("stg_scale", 1.0),
        "rescale": gen.get("rescale", 0.7),
        "scheduler": gen.get("scheduler", "LTX2Scheduler"),
        "seed": gen.get("seed", ""),
        "sub_clips": gen.get("sub_clips", ""),
        "dtype": gen.get("dtype", "bfloat16"),
        "image_conditioning": gen.get("image_conditioning", True),
    }
    
    # Standard MP4 metadata tags
    tags = {
        "title": f"{metadata.get('clip_id', 'clip')} - Economy Documentary v6",
        "artist": gen.get("model", "LTX-2.3-22B-dev (bf16)"),
        "description": description,
        "comment": json.dumps(comment_data),
        "genre": "AI Generated Documentary",
        "copyright": f"Pipeline v6 | {gen.get('dtype', 'bf16')} | {gen.get('resolution', '768x512')}",
    }
    
    # Also embed the prompt as 'synopsis' tag if available
    if metadata.get("prompt"):
        tags["synopsis"] = metadata["prompt"]
    
    # Build ffmpeg command
    cmd = ["ffmpeg", "-y", "-i", input_path]
    for k, v in tags.items():
        if v:
            cmd.extend(["-metadata", f"{k}={v}"])
    cmd.extend(["-c", "copy", tmp_path])
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            # Replace original with metadata-embedded version
            os.replace(tmp_path, input_path)
            log.info(f"  ✓ Embedded metadata in {path.name}")
            return input_path
        else:
            log.warning(f"  ffmpeg metadata embed failed: {result.stderr[-200:]}")
            # Clean up temp file
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            return input_path
    except Exception as e:
        log.warning(f"  ffmpeg metadata embed error: {e}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return input_path


def post_comment(asset_id: str, metadata: dict) -> bool:
    """Post a formatted production metadata comment on a Frame.io asset.
    
    Args:
        asset_id: Frame.io asset ID
        metadata: Production metadata dict
        
    Returns:
        True if comment was posted successfully
    """
    token = get_valid_token()
    if not token:
        return False
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    
    # Build formatted comment text
    gen = metadata.get("generation", {})
    lines = [
        f"📋 **Production Metadata** — {metadata.get('clip_id', '?')}",
        "",
        f"🎬 **Model**: {gen.get('model', '?')}",
        f"📐 **Resolution**: {gen.get('resolution', '?')} @ {gen.get('fps', 24)}fps",
        f"🔧 **Steps**: {gen.get('denoising_steps', 30)} | **CFG**: {gen.get('cfg_scale_video', 3.0)} | **STG**: {gen.get('stg_scale', 1.0)}",
        f"🎲 **Seed**: {gen.get('seed', '?')}",
        f"📊 **Dtype**: {gen.get('dtype', 'bf16')} | **Quantization**: {gen.get('quantization', 'none')}",
        f"🔗 **Sub-clips**: {gen.get('sub_clips', '?')} | **Image conditioning**: {gen.get('image_conditioning', True)}",
        f"📏 **Scheduler**: {gen.get('scheduler', '?')} | **Rescale**: {gen.get('rescale', 0.7)}",
        "",
        f"🎭 **Act**: {metadata.get('act', '?')}",
        f"⏱️ **Timeline**: {metadata.get('narr_start', 0):.1f}s – {metadata.get('narr_end', 0):.1f}s ({metadata.get('narr_duration', 0):.1f}s narration)",
        f"📏 **Required duration**: {metadata.get('required_duration', 0):.1f}s | **Actual**: {metadata.get('actual_duration', 0):.1f}s",
    ]
    
    if metadata.get("prompt"):
        prompt = metadata["prompt"]
        if len(prompt) > 500:
            prompt = prompt[:497] + "..."
        lines.extend(["", f"🎥 **Prompt**: {prompt}"])
    
    if metadata.get("narration"):
        narr = metadata["narration"]
        if len(narr) > 300:
            narr = narr[:297] + "..."
        lines.extend(["", f"🗣️ **Narration**: {narr}"])
    
    # Add B2 link
    clip_id = metadata.get("clip_id", "")
    if clip_id:
        b2_url = f"https://f004.backblazeb2.com/file/economy-vid-assets/v5_clips_v2/{clip_id}.mp4"
        lines.extend(["", f"☁️ **B2**: {b2_url}"])
    
    comment_text = "\n".join(lines)
    
    try:
        resp = requests.post(
            f"{FRAMEIO_API}/accounts/{ACCOUNT_ID}/files/{asset_id}/comments",
            headers=headers,
            json={"data": {"text": comment_text}},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            log.info(f"  ✓ Frame.io comment posted on {asset_id}")
            return True
        else:
            log.warning(f"  Frame.io comment failed: {resp.status_code} {resp.text[:200]}")
            return False
    except Exception as e:
        log.warning(f"  Frame.io comment error: {e}")
        return False


def upload_to_frameio(local_path: str, clip_name: str, metadata: dict = None) -> str:
    """Upload a video file to Frame.io V4 API.
    
    If metadata is provided:
    1. Embeds metadata tags in the MP4 before upload
    2. Posts a formatted comment on the asset after upload

    Args:
        local_path: Path to the local video file
        clip_name: Name for the file in Frame.io (e.g., "clip001.mp4")
        metadata: Optional production metadata dict

    Returns:
        Asset ID if upload succeeded, empty string otherwise
    """
    path = Path(local_path)
    if not path.exists():
        log.error(f"File not found: {local_path}")
        return ""

    # Embed metadata in MP4 if provided and it's a video file
    if metadata and clip_name.endswith(".mp4"):
        embed_metadata_in_mp4(local_path, metadata)

    file_size = path.stat().st_size
    token = get_valid_token()
    if not token:
        log.error("No valid token")
        return ""

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Step 1: Create file asset in Frame.io
    create_resp = requests.post(
        f"{FRAMEIO_API}/accounts/{ACCOUNT_ID}/folders/{CLIPS_FOLDER_ID}/files",
        headers=headers,
        json={
            "data": {
                "name": clip_name,
                "media_type": "video/mp4" if clip_name.endswith(".mp4") else "application/json",
                "file_size": file_size,
            }
        },
    )

    if create_resp.status_code not in (200, 201):
        log.error(f"Failed to create asset: {create_resp.status_code} {create_resp.text[:200]}")
        return ""

    asset_data = create_resp.json().get("data", {})
    upload_urls = asset_data.get("upload_urls", [])
    asset_id = asset_data.get("id", "")

    if not upload_urls:
        log.error(f"No upload URLs returned for {clip_name}")
        return ""

    # Step 2: Upload file chunks to S3
    content_type = "video/mp4" if clip_name.endswith(".mp4") else "application/json"
    with open(path, "rb") as f:
        for i, url_info in enumerate(upload_urls):
            url = url_info["url"] if isinstance(url_info, dict) else url_info
            chunk_size = url_info.get("size", CHUNK_SIZE) if isinstance(url_info, dict) else CHUNK_SIZE
            chunk = f.read(chunk_size)

            if not chunk:
                break

            put_resp = requests.put(
                url,
                data=chunk,
                headers={
                    "Content-Type": content_type,
                    "x-amz-acl": "private",
                },
            )

            if put_resp.status_code not in (200, 204):
                log.error(f"Chunk {i+1} upload failed: {put_resp.status_code}")
                return ""

    log.info(f"  ✓ Frame.io: {clip_name} (asset {asset_id})")
    
    # Step 3: Post production metadata as comment (for video assets only)
    if metadata and clip_name.endswith(".mp4") and asset_id:
        post_comment(asset_id, metadata)
    
    return asset_id


if __name__ == "__main__":
    # CLI usage: python frameio_upload.py <file_path> <clip_name> [metadata_json_path]
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <file_path> <clip_name> [metadata_json_path]")
        sys.exit(1)

    meta = None
    if len(sys.argv) >= 4:
        try:
            meta = json.load(open(sys.argv[3]))
        except Exception as e:
            print(f"Warning: could not load metadata: {e}")

    asset_id = upload_to_frameio(sys.argv[1], sys.argv[2], meta)
    sys.exit(0 if asset_id else 1)
