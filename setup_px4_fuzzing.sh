#!/usr/bin/env bash
set -euo pipefail

#############################################
# PX4 AFL++ Persistent Fuzzing Setup Script
# Ubuntu 24.04 LTS
#############################################

echo "=============================================="
echo " PX4 AFL++ Persistent Fuzzing Environment Setup"
echo "=============================================="
echo

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
  valgrind afl++ \
  wget unzip tar lcov

echo "System packages installed"
echo

#############################################
# STEP 2: Install python-afl
#############################################
echo "[STEP 2] Installing python-afl"

sudo pip3 install --upgrade python-afl --break-system-packages

echo "python-afl installed"
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
# STEP 4: AFL++ Directories and Seeds
#############################################
echo "[STEP 4] Preparing AFL++ directories and seeds"

mkdir -p findings

if [[ -f make_seeds.py ]]; then
  python3 make_seeds.py
else
  echo "WARNING: make_seeds.py not found"
fi

ulimit -c unlimited

echo "AFL++ directories and seeds prepared"
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

grep -Rl 'O0-fprofile-arcs' . --exclude-dir=build \
  | xargs sed -i 's/O0-fprofile-arcs/O0 -fprofile-arcs/g'

echo "Compile flags fixed"
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
# STEP 9: Run AFL++ Persistent Fuzzing
#############################################
echo "[STEP 9] Ready to start AFL++ persistent fuzzing"
echo
echo "Setup complete. Navigate to the directory containing harnessPersistent.py and run the following command manually:"
echo
echo "AFL_FORKSRV_INIT_TMOUT=60000 AFL_DEBUG=1 \\"
echo "AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 \\"
echo "py-afl-fuzz -t 2000 -i seeds -o findings -- \\"
echo "python3 harnessPersistent.py @@"
echo

#############################################
# DONE
#############################################
echo "=============================================="
echo " PX4 AFL++ Setup Complete"
echo "=============================================="
