#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'
# afl_run_single.sh - Start one PX4, then run AFL++ with single-PID harness.
# Usage: ./afl_run_single.sh <seeds_dir> <findings_dir> <Firmware_dir> [model] [port]

SEEDS="${1:-seeds}"
OUTDIR="${2:-findings}"
FW_DIR="${3:-$PWD/Firmware}"
MODEL="${4:-iris}"
PORT="${5:-14560}"

echo "[afl_run_single] Starting PX4 SITL..."
bash "$(dirname "$0")/start_px4_single.sh" --firmware "$FW_DIR" --model "$MODEL"

echo "[afl_run_single] PID file:"
cat /tmp/px4_pid.json || true

export SITL_HOST=127.0.0.1
export SITL_PORT="$PORT"
export PID_FILE=/tmp/px4_pid.json
export AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1

mkdir -p "$OUTDIR"
echo "[afl_run_single] Launching AFL++"
afl-fuzz -i "$SEEDS" -o "$OUTDIR" -- python3 "$(dirname "$0")/afl_pid_harness_single.py" @@
