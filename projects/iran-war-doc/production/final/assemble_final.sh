#!/bin/bash
set -e

echo "=== Assembling acts ==="

echo "Act 1: THE PRICE TAG"
ffmpeg -y -f concat -safe 0 -i /home/user/workspace/iran-war-doc/production/final/act_01_concat.txt -c copy /home/user/workspace/iran-war-doc/production/final/act_01_the_price_tag.mp4

echo "Act 2: THE SMART MONEY"
ffmpeg -y -f concat -safe 0 -i /home/user/workspace/iran-war-doc/production/final/act_02_concat.txt -c copy /home/user/workspace/iran-war-doc/production/final/act_02_the_smart_money.mp4

echo "Act 3: THE CRYPTO PIPELINE"
ffmpeg -y -f concat -safe 0 -i /home/user/workspace/iran-war-doc/production/final/act_03_concat.txt -c copy /home/user/workspace/iran-war-doc/production/final/act_03_the_crypto_pipeline.mp4

echo "Act 4: THE RUSSIA DEAL"
ffmpeg -y -f concat -safe 0 -i /home/user/workspace/iran-war-doc/production/final/act_04_concat.txt -c copy /home/user/workspace/iran-war-doc/production/final/act_04_the_russia_deal.mp4

echo "Act 5: BILLIONAIRES ROW"
ffmpeg -y -f concat -safe 0 -i /home/user/workspace/iran-war-doc/production/final/act_05_concat.txt -c copy /home/user/workspace/iran-war-doc/production/final/act_05_billionaires_row.mp4

echo "Act 6: THE DEFENSE PAYDAY"
ffmpeg -y -f concat -safe 0 -i /home/user/workspace/iran-war-doc/production/final/act_06_concat.txt -c copy /home/user/workspace/iran-war-doc/production/final/act_06_the_defense_payday.mp4

echo "Act 7: THE BILL"
ffmpeg -y -f concat -safe 0 -i /home/user/workspace/iran-war-doc/production/final/act_07_concat.txt -c copy /home/user/workspace/iran-war-doc/production/final/act_07_the_bill.mp4

echo "=== Concatenating final ==="
ffmpeg -y -f concat -safe 0 -i /home/user/workspace/iran-war-doc/production/final/final_concat.txt -c copy /home/user/workspace/iran-war-doc/production/final/THE_WAR_ECONOMY_final.mp4

echo "=== Final documentary ==="
ffprobe -v quiet -show_entries format=duration,format_name,size -of default=noprint_wrappers=1 /home/user/workspace/iran-war-doc/production/final/THE_WAR_ECONOMY_final.mp4
echo "Output: /home/user/workspace/iran-war-doc/production/final/THE_WAR_ECONOMY_final.mp4"

# Generate production metadata
python3 -c "
import json, subprocess
dur = subprocess.check_output(['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration', '-of', 'csv=p=0', '/home/user/workspace/iran-war-doc/production/final/THE_WAR_ECONOMY_final.mp4']).decode().strip()
meta = {
    'title': 'THE WAR ECONOMY: Who Profits When Missiles Fly',
    'duration_sec': float(dur),
    'duration_min': round(float(dur)/60, 1),
    'fps': 24,
    'resolution': '1280x720',
    'production_date': '2026-03-18',
    'video_model': 'LTX-2.3 (bf16, 50 steps, no distillation)',
    'audio_model': 'Qwen3-TTS',
    'scenes': 26,
    'acts': 7
}
print(json.dumps(meta, indent=2))
with open('/home/user/workspace/iran-war-doc/production/final/metadata.json', 'w') as f:
    json.dump(meta, f, indent=2)
"
