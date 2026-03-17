#!/bin/bash
# Collect all final mixed scenes from all 6 VMs and stitch into final documentary
# Run from the sandbox workspace

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o LogLevel=ERROR -i ~/.ssh/id_ed25519"

COLLECT_DIR="/home/user/workspace/final_scenes"
mkdir -p "$COLLECT_DIR"

# VM -> scene assignments
declare -A VM_SCENES
VM_SCENES["ssh6.vast.ai:11952"]="01 02 03 04 05 06 07"
VM_SCENES["ssh6.vast.ai:11954"]="08 09 10 11 12 13 14"
VM_SCENES["ssh4.vast.ai:11954"]="15 16 17 18 19 20 21"
VM_SCENES["ssh6.vast.ai:11956"]="22 23 24 25 26 27 28"
VM_SCENES["ssh7.vast.ai:11956"]="29 30 31 32 33 34 35"
VM_SCENES["ssh8.vast.ai:11958"]="36 37 38 39 40 41 42"

echo "=== Collecting final scenes from all VMs ==="
for hostport in "${!VM_SCENES[@]}"; do
    host=$(echo $hostport | cut -d: -f1)
    port=$(echo $hostport | cut -d: -f2)
    scenes="${VM_SCENES[$hostport]}"
    
    echo "Collecting from $host:$port..."
    for s in $scenes; do
        src="/workspace/final_output/scene_${s}_final.mp4"
        dst="$COLLECT_DIR/scene_${s}_final.mp4"
        if [ ! -f "$dst" ]; then
            scp $SSH_OPTS -P $port root@$host:$src "$dst" 2>/dev/null
            if [ $? -eq 0 ]; then
                echo "  Scene $s: collected"
            else
                echo "  Scene $s: MISSING"
            fi
        else
            echo "  Scene $s: already have it"
        fi
    done
done

# Count collected
collected=$(ls "$COLLECT_DIR"/scene_*_final.mp4 2>/dev/null | wc -l)
echo ""
echo "Collected $collected/42 scenes"

if [ "$collected" -lt 42 ]; then
    echo "WARNING: Not all scenes collected!"
    echo "Missing:"
    for i in $(seq -w 1 42); do
        if [ ! -f "$COLLECT_DIR/scene_${i}_final.mp4" ]; then
            echo "  Scene $i"
        fi
    done
    exit 1
fi

# Stitch into final documentary
echo ""
echo "=== Stitching final documentary ==="
LIST_FILE="$COLLECT_DIR/stitch_list.txt"
> "$LIST_FILE"
for i in $(seq -w 1 42); do
    echo "file '$COLLECT_DIR/scene_${i}_final.mp4'" >> "$LIST_FILE"
done

OUTPUT="/home/user/workspace/final_documentary.mp4"
ffmpeg -y -f concat -safe 0 -i "$LIST_FILE" \
    -c:v libx264 -preset medium -crf 18 \
    -c:a aac -b:a 192k \
    "$OUTPUT"

if [ -f "$OUTPUT" ]; then
    dur=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$OUTPUT")
    size=$(du -h "$OUTPUT" | cut -f1)
    echo "Final documentary: $dur seconds, $size"
fi
