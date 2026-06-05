#!/usr/bin/env bash
# Pure Bash Agent Launcher with job control (separate process groups)
set -m

PYTHON_EXE=$1
ROLE=$2
PORT=$3
TEST_MODULE=$4
STDOUT_LOG=$5
STDERR_LOG=$6
DB_DIR=$7

echo "DEBUG_LAUNCH_AGENT_SH: ROLE=$ROLE PORT=$PORT TEST_MODULE=$TEST_MODULE STDOUT_LOG=$STDOUT_LOG STDERR_LOG=$STDERR_LOG DB_DIR=$DB_DIR" >&2

if [ "$ROLE" = "gsa" ]; then
    "$PYTHON_EXE" global_state_agent.py "$PORT" "$DB_DIR" > "$STDOUT_LOG" 2> "$STDERR_LOG" &
else
    "$PYTHON_EXE" agents/"$ROLE"/app.py "$PORT" "$TEST_MODULE" "$DB_DIR" > "$STDOUT_LOG" 2> "$STDERR_LOG" &
fi

echo $!
