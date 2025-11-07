#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'
# start_px4_single.sh - launch one PX4 SITL+Gazebo and record its PID
# Usage: ./start_px4_single.sh [--firmware DIR] [--model iris] [--headless]

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
  echo "[start_px4_single] Firmware dir not found: $FW_DIR"; exit 2
fi
cd "$FW_DIR"

if [[ $HEADLESS -eq 1 ]]; then export HEADLESS=1; fi
nohup make px4_sitl_default gazebo >/tmp/px4_single.log 2>&1 &
sleep 5

PIDS=$(ps -u $(whoami) -o pid,cmd | awk '/px4_sitl_default.*\/px4/ {print $1}')
if [[ -z "$PIDS" ]]; then
  PIDS=$(ps -u $(whoami) -o pid,cmd | awk '/\/px4(\s|$)/ {print $1}')
fi
PX4_PID=$(ps -o pid,lstart,cmd -p $(echo $PIDS) 2>/dev/null | sort -k2,8 | awk '{print $1}' | tail -n 1)
if [[ -z "$PX4_PID" ]]; then echo "[start_px4_single] Could not resolve px4 PID."; exit 3; fi

echo "{\"px4_pid\": $PX4_PID}" > /tmp/px4_pid.json
echo "[start_px4_single] Wrote /tmp/px4_pid.json with PID: $PX4_PID"
