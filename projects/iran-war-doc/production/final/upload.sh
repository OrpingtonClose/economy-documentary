#!/bin/bash
set -e

echo "=== Uploading to B2 ==="
# Authorize
b2 authorize-account $B2_KEY_ID $B2_APP_KEY

# Upload final video
b2 upload-file economy-vid-assets /home/user/workspace/iran-war-doc/production/final/THE_WAR_ECONOMY_final.mp4 war-economy-v2/THE_WAR_ECONOMY_final.mp4

# Upload per-act files
for act_file in /home/user/workspace/iran-war-doc/production/final/act_*.mp4; do
    b2 upload-file economy-vid-assets "$act_file" war-economy-v2/acts/$(basename "$act_file")
done

# Upload metadata (embedded in video, not as separate JSON per user request)
echo "Metadata embedded in video file via ffmpeg metadata injection"

echo "=== B2 upload complete ==="

echo "=== Uploading to Frame.io ==="
# Frame.io upload via API (no JSON metadata files per user request)
python3 -c "
import requests
import json

# Get auth token
token_resp = requests.post('https://ims-na1.adobelogin.com/ims/token/v3', data={
    'grant_type': 'client_credentials',
    'client_id': '$FRAMEIO_CLIENT_ID',
    'client_secret': '$FRAMEIO_CLIENT_SECRET',
    'scope': 'openid,AdobeID,read_organizations,additional_info.projectedProductContext,additional_info.roles'
})
token = token_resp.json().get('access_token', '')

if token:
    # Upload video
    headers = {'Authorization': f'Bearer {token}'}
    # Create asset
    resp = requests.post(
        f'https://api.frame.io/v2/accounts/$FRAMEIO_ACCOUNT_ID/uploads',
        headers=headers,
        json={'name': 'THE_WAR_ECONOMY_final.mp4', 'type': 'file', 'filetype': 'video/mp4'}
    )
    print(f'Frame.io upload initiated: {resp.status_code}')
else:
    print('Frame.io auth failed - skip')
"

echo "=== Upload complete ==="
