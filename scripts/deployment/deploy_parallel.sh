#!/bin/bash
# Deploy bootstrap to all running VMs in parallel
# Each VM runs bootstrap in background, we can check later

SSH_KEY=~/.ssh/vast_v3
WORKSPACE=/home/user/workspace

# Files to upload
FILES="bootstrap.sh v6_generate_v3.py v6_encode_prompts.py frameio_upload.py frameio_tokens.json v5_clip_plan.json"

deploy_vm() {
    local HOST=$1
    local PORT=$2
    local LABEL=$3
    
    echo "[$LABEL] Uploading files..."
    for f in $FILES; do
        scp -P $PORT -o StrictHostKeyChecking=no -o ConnectTimeout=15 -i $SSH_KEY \
            $WORKSPACE/$f root@$HOST:/root/ 2>/dev/null
    done
    
    echo "[$LABEL] Starting bootstrap in background..."
    ssh -p $PORT -o StrictHostKeyChecking=no -o ConnectTimeout=15 -i $SSH_KEY \
        root@$HOST "nohup bash /root/bootstrap.sh > /root/bootstrap.log 2>&1 &"
    
    echo "[$LABEL] Bootstrap launched"
}

# Read VM info and deploy
python3 -c "
import json
with open('$WORKSPACE/vm_info.json') as f:
    vms = json.load(f)
for vm in vms:
    if vm['status'] == 'running':
        print(f\"{vm['ssh_host']} {vm['ssh_port']} {vm['label']}\")
" | while read HOST PORT LABEL; do
    deploy_vm "$HOST" "$PORT" "$LABEL" &
done

wait
echo "=== All deployments launched ==="
