import json
import os
from youtube_transcript_api import YouTubeTranscriptApi

with open('economy_videos.json') as f:
    videos = json.load(f)

os.makedirs('transcripts', exist_ok=True)

ytt_api = YouTubeTranscriptApi()
results = []
failed = []

for i, v in enumerate(videos):
    vid_id = v['video_id']
    channel = v['channel']
    title = v['title']
    
    print(f"[{i+1}/{len(videos)}] {channel}: {title[:60]}...", flush=True)
    
    try:
        # Try English first, then auto-generated, then any language
        transcript_list = ytt_api.list(vid_id)
        
        transcript = None
        # Prefer English
        for t in transcript_list:
            if t.language_code.startswith('en'):
                transcript = t.fetch()
                break
        
        # Fallback: any transcript, translate to English
        if not transcript:
            for t in transcript_list:
                if t.is_translatable:
                    try:
                        transcript = t.translate('en').fetch()
                        break
                    except:
                        pass
        
        # Last resort: first available
        if not transcript:
            for t in transcript_list:
                transcript = t.fetch()
                break
        
        if transcript:
            # Join text segments
            full_text = ' '.join([s.text for s in transcript])
            
            result = {
                'video_id': vid_id,
                'channel': channel,
                'title': title,
                'transcript': full_text[:50000]  # Cap at 50k chars
            }
            results.append(result)
            
            # Save individual transcript
            safe_name = vid_id
            with open(f'transcripts/{safe_name}.txt', 'w') as f:
                f.write(f"Channel: {channel}\nTitle: {title}\n\n{full_text}")
            
            print(f"  -> OK ({len(full_text)} chars)")
        else:
            failed.append({'video_id': vid_id, 'channel': channel, 'title': title, 'reason': 'no transcript found'})
            print(f"  -> No transcript available")
            
    except Exception as e:
        failed.append({'video_id': vid_id, 'channel': channel, 'title': title, 'reason': str(e)[:200]})
        print(f"  -> FAILED: {str(e)[:100]}")

print(f"\n\nDone! Extracted {len(results)} transcripts, {len(failed)} failed")

with open('all_transcripts.json', 'w') as f:
    json.dump(results, f, indent=2)

with open('failed_transcripts.json', 'w') as f:
    json.dump(failed, f, indent=2)
