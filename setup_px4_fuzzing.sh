#!/usr/bin/env bash
set -euo pipefail

#############################################
# PX4 Mission Fuzzing Setup Script
# Ubuntu 24.04 LTS
#############################################

echo "=============================================="
echo " PX4 Mission Fuzzing Environment Setup"
echo "=============================================="
echo
#############################################
# STEP -1: Acquire sudo once
#############################################
echo "[INIT] Requesting sudo privileges (only once) for the script"

sudo -v

while true; do
  sudo -n true
  sleep 60
  kill -0 "$$" || exit
done 2>/dev/null &

SUDO_KEEPALIVE_PID=$!

trap 'kill $SUDO_KEEPALIVE_PID' EXIT

#############################################
# STEP 0: Sanity Checks
#############################################
echo "[STEP 0] Sanity checks"

if [[ "$(lsb_release -rs)" != "24.04" ]]; then
  echo "WARNING: This script was tested on Ubuntu 24.04 LTS"
fi

command -v clang >/dev/null || {
  echo "clang not found; will be installed later"
}

echo "Sanity checks complete"
echo

#############################################
# STEP 1: System Update & Package Installation
#############################################
echo "[STEP 1] Installing system packages"

sudo apt update
sudo apt upgrade -y

sudo apt install -y \
  gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-gl \
  libfuse2t64 \
  libxcb-xinerama0 libxkbcommon-x11-0 libxcb-cursor-dev \
  build-essential cmake ninja-build ccache \
  clang lld \
  libc6-dev libstdc++-14-dev \
  libc++-dev libc++abi-dev \
  python3 python3-pip git \
  valgrind \
  wget unzip tar lcov

echo "System packages installed"
echo

#############################################
# STEP 2: Install mission harness Python dependency
#############################################
echo "[STEP 2] Installing pymavlink"

sudo pip3 install --upgrade pymavlink --break-system-packages

echo "pymavlink installed"
echo

#############################################
# STEP 3: Clone Repositories
#############################################
echo "[STEP 3] Cloning repositories"
#REPO_URL="https://github.com/kfreemanWrightState/PX4Fuzzing.git"
#DIR="PX4Fuzzing"

#if [[ -d "$DIR/.git" ]]; then
#    echo "PX4Fuzzing repo already exists"

#    CURRENT_REMOTE=$(git -C "$DIR" config --get remote.origin.url)

#    if [[ "$CURRENT_REMOTE" == "$REPO_URL" ]]; then
#        echo "Correct repo found, fetching all branches..."
#        git -C "$DIR" fetch --all --prune
#    else
#        echo "Directory exists but is not the correct repo!"
#        exit 1
#    fi

#else
#    echo "Cloning repository..."
#    git clone "$REPO_URL" "$DIR"
#fi

# change into repo directory
#cd "$DIR" || { echo "Failed to enter repo directory"; exit 1; }

#echo "Now in repo directory: $(pwd)"


if [[ ! -d PX4-Autopilot ]]; then
  git clone https://github.com/PX4/PX4-Autopilot.git --recursive
else
  echo "PX4-Autopilot already exists, skipping clone"
fi

#if [[ ! -d qgroundcontrol ]]; then
#  git clone https://github.com/mavlink/qgroundcontrol.git
#else
#  echo "QGroundControl source already exists, skipping clone"
#fi

#if [[ ! -f QGroundControl-x86_64.AppImage ]]; then
#  wget https://d176tv9ibo4jno.cloudfront.net/latest/QGroundControl-x86_64.AppImage
#  chmod +x QGroundControl-x86_64.AppImage
#else
#  echo "QGroundControl AppImage already downloaded"
#fi

echo "Repositories ready"
echo

#############################################
# STEP 4: Runtime directories
#############################################
echo "[STEP 4] Preparing runtime directories"

mkdir -p findings findings/potential_crash_reports

ulimit -c unlimited

echo "Runtime directories prepared"
echo

#############################################
# STEP 5: PX4 Dependency Setup
#############################################
echo "[STEP 5] Installing PX4 dependencies"

cd PX4-Autopilot

./Tools/setup/ubuntu.sh

echo "PX4 dependencies installed"
echo

#############################################
# STEP 6: Fix PX4 Compile Flag Bug
#############################################
echo "[STEP 6] Fixing PX4 compile flag issue"

if grep -RIlq --exclude-dir=build 'O0-fprofile-arcs' .; then
  grep -RIlZ --exclude-dir=build 'O0-fprofile-arcs' . \
    | xargs -0 -r sed -i 's/O0-fprofile-arcs/O0 -fprofile-arcs/g'
  echo "Compile flags fixed"
else
  echo "No compile flag fixes needed"
fi
echo

#############################################
# STEP 7: Build PX4 SITL with ASan
#############################################
echo "[STEP 7] Building PX4 SITL with AddressSanitizer"

CC=clang CXX=clang++ PX4_ASAN=1 make px4_sitl -j"$(nproc)"

echo "PX4 SITL build complete"
echo

#############################################
# STEP 8: CPU Performance Mode (Bare Metal Only)
#############################################
echo "[STEP 8] Optional CPU performance mode"

if [[ -d /sys/devices/system/cpu/cpu0/cpufreq ]]; then
  echo "Setting CPU governor to performance"
  echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
else
  echo "CPU frequency scaling not available (likely VM)"
fi

echo

#############################################
# STEP 9: Run mission fuzzing
#############################################
echo "[STEP 9] Ready to start mission fuzzing"
echo
echo "Setup complete. From the repository root, run the mission harness manually:"
echo
echo "python3 customHarness.py --initial-startup-wait"
echo
echo "Optional examples:"
echo "python3 customHarness.py --initial-startup-wait --iterations 100"
echo "python3 customHarness.py --initial-startup-wait --iterations 1000 --seed 1234"
echo

#############################################
# DONE
#############################################
echo "=============================================="
echo " PX4 Mission Fuzzing Setup Complete"
echo "=============================================="
