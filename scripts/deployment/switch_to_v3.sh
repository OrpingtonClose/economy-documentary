#!/bin/bash
# Kill v2
pkill -f "v6_generate_v2.py" 2>/dev/null
sleep 3
pkill -9 -f "v6_generate_v2.py" 2>/dev/null
sleep 1
echo "v2 processes killed"

# Check no python running
pgrep -f "v6_generate" && echo "WARNING: still running" || echo "All clear"

# Start v3
cd /root
nohup /root/LTX-2/.venv/bin/python v6_generate_v3.py --start $1 --end $2 --resume --no-compile > /root/$3 2>&1 &
echo "v3 started with PID $! (range $1-$2, log: $3)"
sleep 3
ps aux | grep v6_generate | grep -v grep
