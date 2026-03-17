#!/bin/bash
SSH_KEY=~/.ssh/vast_v3

declare -A vms
vms[vm01]="ssh6.vast.ai:13764"
vms[vm02]="ssh1.vast.ai:13766"
vms[vm03]="ssh4.vast.ai:13766"
vms[vm04]="ssh4.vast.ai:13768"
vms[vm06]="ssh3.vast.ai:13770"
vms[vm07]="ssh7.vast.ai:13770"
vms[vm08]="ssh7.vast.ai:13772"
vms[vmI]="ssh8.vast.ai:13640"
vms[vmJ]="ssh7.vast.ai:13640"

TOTAL_DONE=0
TOTAL_FAILED=0
RUNNING=0
STOPPED=0

for name in vm01 vm02 vm03 vm04 vm06 vm07 vm08 vmI vmJ; do
    IFS=':' read -r host port <<< "${vms[$name]}"
    result=$(ssh -i $SSH_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=10 -p $port root@$host "
        if pgrep -f v6_generate > /dev/null 2>&1; then echo 'S:RUNNING'; else echo 'S:STOPPED'; fi
        /root/LTX-2/.venv/bin/python3 -c '
import json
with open(\"/root/v6_progress.json\") as f:
    p = json.load(f)
done = len(p[\"completed\"]) - 312
if \"$name\" == \"vmJ\":
    done = len(p[\"completed\"]) - 305
print(f\"D:{done}\")
print(f\"F:{len(p[\"failed\"])}\")
' 2>/dev/null
        nvidia-smi --query-gpu=memory.used --format=csv,noheader 2>/dev/null | head -1
    " 2>&1 | grep -E '^(S:|D:|F:|[0-9]+ MiB)')
    
    status=$(echo "$result" | grep '^S:' | cut -d: -f2)
    done=$(echo "$result" | grep '^D:' | cut -d: -f2)
    failed=$(echo "$result" | grep '^F:' | cut -d: -f2)
    vram=$(echo "$result" | grep 'MiB' | tr -d ' ')
    
    echo "$name: $status | Done: ${done:-?} | Failed: ${failed:-?} | VRAM: ${vram:-?}"
done
