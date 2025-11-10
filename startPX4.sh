#!/bin/bash


# start_px4_single.sh (quick-and-dirty)
NUM_PROCS=$(nproc)
gnome-terminal --working-directory="$(pwd)/PX4-Autopilot" -- /bin/bash -c "CC=clang CXX=clang++ PX4_ASAN=1 make px4_sitl gz_x500 -j$NUM_PROCS; exec bash"
#sleep 5
#PIDS=$(ps aux | grep "[/]usr/bin/cmake -E env PX4_SIM_MODEL=gz_x500" | awk '{print $2}' | head -n 2 | tr '\n' ' ')
#set -- $PIDS
#echo "{\"px4_pids\": [$1, $2]}" | tee /tmp/px4_pids.json