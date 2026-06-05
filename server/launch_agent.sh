#!/usr/bin/env bash
# Pure Bash Agent Launcher with job control (separate process groups)
set -m

PYTHON_EXE=$1
ROLE=$2
PORT=$3
TEST_MODULE=$4
STDOUT_LOG=$5
STDERR_LOG=$6

if [ "$ROLE" = "gsa" ]; then
    "$PYTHON_EXE" global_state_agent.py "$PORT" > "$STDOUT_LOG" 2> "$STDERR_LOG" &
else
    "$PYTHON_EXE" agents/"$ROLE"/app.py "$PORT" "$TEST_MODULE" > "$STDOUT_LOG" 2> "$STDERR_LOG" &
fi

echo $!
