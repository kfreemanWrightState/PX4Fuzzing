#!/usr/bin/env bash
# start_dual_px4.sh
# Start two PX4 SITL instances with Gazebo, record their PX4 PIDs to a JSON file.
# Best-effort: uses PX4 multi-run if available, else manual fallback.
#
# Outputs:
#   /tmp/px4_pids.json   => {"px4_pids":[1234,5678]}
#
# Usage:
#   ./start_dual_px4.sh [--firmware DIR] [--model iris] [--headless]
set -euo pipefail
IFS=$'\n\t'

FW_DIR="${PWD}/Firmware"
MODEL="iris"
HEADLESS=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --firmware) FW_DIR="$2"; shift 2;;
    --model) MODEL="$2"; shift 2;;
    --headless) HEADLESS=1; shift 1;;
    -h|--help) echo "Usage: $0 [--firmware DIR] [--model iris] [--headless]"; exit 0;;
    *) echo "Unknown arg: $1"; exit 2;;
  esac
done

if [[ ! -d "$FW_DIR" ]]; then
  echo "[start_dual_px4] Firmware dir not found: $FW_DIR"; exit 2
fi
cd "$FW_DIR"

run() { echo "+ $*"; bash -lc "$*"; }

MULTI_RUN=""
if [[ -x Tools/simulation/gazebo-classic/sitl_multiple_run.sh ]]; then
  MULTI_RUN="Tools/simulation/gazebo-classic/sitl_multiple_run.sh"
elif [[ -x Tools/simulation/gazebo/sitl_multiple_run.sh ]]; then
  MULTI_RUN="Tools/simulation/gazebo/sitl_multiple_run.sh"
fi

if [[ -n "$MULTI_RUN" ]]; then
  echo "[start_dual_px4] Using: $MULTI_RUN"
  if [[ $HEADLESS -eq 1 ]]; then export HEADLESS=1; fi
  run "$MULTI_RUN -n 2 -m $MODEL &"
  sleep 6
else
  echo "[start_dual_px4] Fallback: launching two instances via make."
  if [[ $HEADLESS -eq 1 ]]; then export HEADLESS=1; fi
  PX4_INSTANCE=0 PX4_SIM_MODEL="$MODEL" nohup make px4_sitl_default gazebo >/tmp/px4_0.log 2>&1 &
  sleep 3
  PX4_INSTANCE=1 PX4_SIM_MODEL="$MODEL" nohup make px4_sitl_default gazebo >/tmp/px4_1.log 2>&1 &
  sleep 4
fi

sleep 3
PIDS=$(ps -u $(whoami) -o pid,cmd | awk '/px4_sitl_default.*\/px4/ {print $1}')
if [[ -z "$PIDS" ]]; then
  PIDS=$(ps -u $(whoami) -o pid,cmd | awk '/\/px4(\s|$)/ {print $1}')
fi

COUNT=$(echo "$PIDS" | wc -w | tr -d ' ')
if [[ "$COUNT" -lt 2 ]]; then
  echo "[start_dual_px4] Warning: found fewer than 2 px4 PIDs: $PIDS"
fi

PX4_PIDS=$(ps -o pid,lstart,cmd -p $(echo $PIDS) 2>/dev/null | sort -k2,8 | awk '{print $1}' | tail -n 2)

if [[ -z "$PX4_PIDS" ]]; then
  echo "[start_dual_px4] Could not resolve px4 PIDs."; exit 3
fi

JSON='{"px4_pids":['
first=1
for p in $PX4_PIDS; do
  if [[ $first -eq 1 ]]; then JSON="${JSON}${p}"; first=0; else JSON="${JSON},${p}"; fi
done
JSON="${JSON}]}"

echo "$JSON" > /tmp/px4_pids.json
echo "[start_dual_px4] Wrote /tmp/px4_pids.json -> $JSON"
echo "[start_dual_px4] Active px4 PIDs:"
for p in $PX4_PIDS; do echo "  - $p"; done
