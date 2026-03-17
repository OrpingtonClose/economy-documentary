#!/bin/bash
# Deploy fix to all VMs and get them ready
set -uo pipefail

SSH_KEY=~/.ssh/vast_v3
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=20 -o LogLevel=ERROR -i $SSH_KEY"
FIX_SCRIPT=/home/user/workspace/fix_and_continue.sh

# VMs that need text encoder only (have model + venv)
ENCODER_ONLY=(
    "vm01:ssh6.vast.ai:13764"
    "vm02:ssh1.vast.ai:13766"
    "vm04:ssh4.vast.ai:13768"
    "vm06:ssh3.vast.ai:13770"
    "vm07:ssh7.vast.ai:13770"
    "vmI:ssh8.vast.ai:13640"
    "vmJ:ssh7.vast.ai:13640"
)

# VMs that need full bootstrap (no model, no venv)
FULL_BOOTSTRAP=(
    "vm03:ssh4.vast.ai:13766"
    "vm08:ssh7.vast.ai:13772"
)

deploy_encoder_only() {
    local label=$(echo $1 | cut -d: -f1)
    local host=$(echo $1 | cut -d: -f2)
    local port=$(echo $1 | cut -d: -f3)
    
    echo "[$label] Downloading text encoder..."
    ssh $SSH_OPTS -p $port root@$host "source /root/LTX-2/.venv/bin/activate && python3 -c \"
from huggingface_hub import snapshot_download
import shutil, os
if not os.path.exists('/root/models/text_encoder/config.json'):
    snapshot_download('Lightricks/LTX-2.3',
                      local_dir='/root/models/ltx23_full',
                      allow_patterns=['text_encoder/*'],
                      token='${HF_TOKEN}')
    src = '/root/models/ltx23_full/text_encoder'
    if os.path.exists(src):
        os.makedirs('/root/models/text_encoder', exist_ok=True)
        shutil.rmtree('/root/models/text_encoder', ignore_errors=True)
        shutil.move(src, '/root/models/text_encoder')
        shutil.rmtree('/root/models/ltx23_full', ignore_errors=True)
    print('OK: text encoder downloaded')
else:
    print('OK: text encoder already exists')
\"" 2>&1 | sed "s/^/[$label] /"
    echo "[$label] DONE"
}

deploy_full() {
    local label=$(echo $1 | cut -d: -f1)
    local host=$(echo $1 | cut -d: -f2)
    local port=$(echo $1 | cut -d: -f3)
    
    echo "[$label] Uploading fix script..."
    scp $SSH_OPTS -P $port $FIX_SCRIPT root@$host:/root/fix_and_continue.sh 2>&1 | sed "s/^/[$label] /"
    
    echo "[$label] Running full bootstrap..."
    ssh $SSH_OPTS -p $port root@$host "bash /root/fix_and_continue.sh" 2>&1 | sed "s/^/[$label] /"
    echo "[$label] DONE"
}

# Run all encoder-only downloads in parallel
echo "=== Starting text encoder downloads on 7 VMs ==="
for vm in "${ENCODER_ONLY[@]}"; do
    deploy_encoder_only "$vm" &
done

# Run full bootstraps in parallel
echo "=== Starting full bootstraps on 2 VMs ==="
for vm in "${FULL_BOOTSTRAP[@]}"; do
    deploy_full "$vm" &
done

echo "=== Waiting for all deployments... ==="
wait
echo "=== ALL DEPLOYMENTS COMPLETE ==="
