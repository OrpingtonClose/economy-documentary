#!/usr/bin/env python3
"""Download clips from B2 and upload to Frame.io retroactively."""
import json, os, subprocess, sys, time, requests, tempfile
from pathlib import Path

# Frame.io config
tokens = json.load(open('/home/user/workspace/frameio_tokens.json'))
ACCESS_TOKEN = tokens['access_token']
ACCOUNT_ID = "${FRAMEIO_ACCOUNT_ID}"
CLIPS_FOLDER_ID = "06216ba5-fce7-47b3-b976-844f94aaf242"
API = "https://api.frame.io/v4"

# B2 config  
B2_BUCKET = "economy-vid-assets"
B2_PREFIX = "v5_clips_v2"
B2_DOWNLOAD = "https://f004.backblazeb2.com/file/economy-vid-assets"

CHUNK_SIZE = 25 * 1024 * 1024

def refresh_token():
    global ACCESS_TOKEN
    resp = requests.post('https://ims-na1.adobelogin.com/ims/token/v3', data={
        'grant_type': 'refresh_token',
        'client_id': '${FRAMEIO_CLIENT_ID}',
        'client_secret': '${FRAMEIO_CLIENT_SECRET}',
        'refresh_token': tokens['refresh_token'],
    })
    if resp.status_code == 200:
        new_tokens = resp.json()
        if 'refresh_token' not in new_tokens:
            new_tokens['refresh_token'] = tokens['refresh_token']
        new_tokens['refreshed_at'] = time.time()
        json.dump(new_tokens, open('/home/user/workspace/frameio_tokens.json','w'), indent=2)
        ACCESS_TOKEN = new_tokens['access_token']
        print("Token refreshed")

def upload_clip(local_path, clip_name):
    """Upload a single clip to Frame.io."""
    file_size = os.path.getsize(local_path)
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
    
    resp = requests.post(
        f"{API}/accounts/{ACCOUNT_ID}/folders/{CLIPS_FOLDER_ID}/files",
        headers=headers,
        json={"data": {"name": clip_name, "media_type": "video/mp4", "file_size": file_size}},
    )
    
    if resp.status_code == 401:
        refresh_token()
        headers["Authorization"] = f"Bearer {ACCESS_TOKEN}"
        resp = requests.post(
            f"{API}/accounts/{ACCOUNT_ID}/folders/{CLIPS_FOLDER_ID}/files",
            headers=headers,
            json={"data": {"name": clip_name, "media_type": "video/mp4", "file_size": file_size}},
        )
    
    if resp.status_code not in (200, 201):
        print(f"  FAIL create {clip_name}: {resp.status_code} {resp.text[:200]}")
        return False
    
    data = resp.json().get("data", {})
    upload_urls = data.get("upload_urls", [])
    asset_id = data.get("id", "?")
    
    with open(local_path, "rb") as f:
        for i, url_info in enumerate(upload_urls):
            url = url_info["url"] if isinstance(url_info, dict) else url_info
            chunk_size = url_info.get("size", CHUNK_SIZE) if isinstance(url_info, dict) else CHUNK_SIZE
            chunk = f.read(chunk_size)
            if not chunk:
                break
            put_resp = requests.put(url, data=chunk, headers={"Content-Type": "video/mp4", "x-amz-acl": "private"})
            if put_resp.status_code not in (200, 204):
                print(f"  FAIL chunk {i+1}: {put_resp.status_code}")
                return False
    
    print(f"  ✓ {clip_name} -> Frame.io ({asset_id})")
    return True

# Get list of clips from B2
clips = []
for i in range(1, 335):
    clip_name = f"clip{i:03d}.mp4"
    clips.append(clip_name)

# Known completed clips from B2
completed = [
    "clip001", "clip002", "clip003", "clip004", "clip005", "clip006", "clip007",
    "clip008", "clip009", "clip010", "clip011", "clip012", "clip013", "clip014",
    "clip015", "clip016", "clip017", "clip018", "clip019", "clip020", "clip021",
    "clip168", "clip169", "clip170", "clip171", "clip172", "clip173", "clip174", "clip175",
]

tmpdir = tempfile.mkdtemp(prefix="frameio_retro_")
print(f"Downloading to {tmpdir}")

for clip_id in completed:
    clip_name = f"{clip_id}.mp4"
    b2_url = f"{B2_DOWNLOAD}/{B2_PREFIX}/{clip_name}"
    local_path = os.path.join(tmpdir, clip_name)
    
    # Download from B2
    print(f"Downloading {clip_name}...")
    resp = requests.get(b2_url, stream=True)
    if resp.status_code != 200:
        # Try b2 CLI
        result = subprocess.run(["b2", "file", "download", f"b2://economy-vid-assets/{B2_PREFIX}/{clip_name}", local_path],
                                capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(f"  SKIP {clip_name}: download failed")
            continue
    else:
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024*1024):
                f.write(chunk)
    
    if not os.path.exists(local_path) or os.path.getsize(local_path) < 1000:
        print(f"  SKIP {clip_name}: too small or missing")
        continue
    
    # Upload to Frame.io
    upload_clip(local_path, clip_name)
    
    # Cleanup to save disk
    os.unlink(local_path)

print("\nDone retroactive upload!")
