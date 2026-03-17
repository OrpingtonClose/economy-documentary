#!/bin/bash
# Deploy and run setup on all VMs in parallel

HF_TOKEN="${HF_TOKEN}"

declare -A VMS
VMS[32971953]="ssh6.vast.ai:11952"
VMS[32971954]="ssh6.vast.ai:11954"
VMS[32971955]="ssh4.vast.ai:11954"
VMS[32971956]="ssh6.vast.ai:11956"
VMS[32971957]="ssh7.vast.ai:11956"
VMS[32971958]="ssh8.vast.ai:11958"

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=30 -o ServerAliveInterval=15 -o LogLevel=ERROR"

setup_vm() {
    local vm_id=$1
    local host_port=$2
    local host=$(echo $host_port | cut -d: -f1)
    local port=$(echo $host_port | cut -d: -f2)
    
    echo "[VM $vm_id] Starting setup on $host:$port..."
    
    # Copy setup script
    scp $SSH_OPTS -P $port /home/user/workspace/vm_setup.sh root@$host:/tmp/vm_setup.sh 2>/dev/null
    
    # Run setup with HF token
    ssh $SSH_OPTS -p $port root@$host "export HF_TOKEN=$HF_TOKEN; chmod +x /tmp/vm_setup.sh; nohup bash /tmp/vm_setup.sh > /tmp/setup.log 2>&1 &"
    
    echo "[VM $vm_id] Setup launched in background on $host:$port"
}

for vm_id in "${!VMS[@]}"; do
    setup_vm "$vm_id" "${VMS[$vm_id]}" &
done

wait
echo "All VMs setup launched."
