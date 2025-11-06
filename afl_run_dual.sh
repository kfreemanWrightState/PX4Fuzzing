#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'
SEEDS="${1:-seeds}"
OUTDIR="${2:-findings}"
FW_DIR="${3:-$PWD/Firmware}"
MODEL="${4:-iris}"
PORT1="${5:-14560}"
PORT2="${6:-14570}"

echo "[afl_run_dual] Starting dual SITL..."
bash "$(dirname "$0")/start_dual_px4.sh" --firmware "$FW_DIR" --model "$MODEL" --headless || {
  echo "[afl_run_dual] Failed to start PX4 instances."; exit 2;
}
echo "[afl_run_dual] PID file:"
cat /tmp/px4_pids.json || true

export SITL_HOST=127.0.0.1
export SITL_PORT1="$PORT1"
export SITL_PORT2="$PORT2"
export PID_FILE=/tmp/px4_pids.json
export AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1

mkdir -p "$OUTDIR"
echo "[afl_run_dual] Launching AFL++"
afl-fuzz -i "$SEEDS" -o "$OUTDIR" -- python3 "$(dirname "$0")/afl_pid_harness.py" @@
