#!/bin/bash
HOST=${1:-127.0.0.1}
PORT=${2:-14560}
FILE=${3:-seeds/heartbeat.bin}
cat "$FILE" | nc -u -w1 "$HOST" "$PORT"

