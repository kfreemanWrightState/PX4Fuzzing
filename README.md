# PX4 Mission Fuzzing Environment Setup

This branch uses `customHarness.py`, a standalone PX4 mission fuzzer for SITL.
It no longer relies on `python-afl`, `py-afl-fuzz`, stdin-driven testcases, or
the `make_seeds.py` workflow.

Instead, the harness:

- keeps long-lived MAVLink connections open
- sends GCS heartbeats and offboard setpoints continuously
- generates mission inputs internally
- alternates between valid, invalid-prone, and recursive mission types
- grows mission size over time
- restarts PX4 and Gazebo when failures are detected
- writes mission previews, recent mission history, and potential crash reports

## Requirements

Your environment should meet the following requirements before building PX4 and
running the mission fuzzer.

### Operating System

- Ubuntu 24.04 LTS is the recommended and tested platform
- Other Linux distributions may work, but are not covered by this guide

### System Resources

- At least 4 CPU cores, 8 recommended
- At least 8 GB RAM, 16+ GB recommended
- At least 20 GB free disk space
- More disk space is helpful because PX4 builds, logs, and crash reports can grow quickly

### Internet Access

Internet access is required for:

- installing system packages
- downloading PX4 and its dependencies
- installing Python modules
- downloading QGroundControl if you want the optional ground station

### Software Requirements

This project uses:

- Python 3.10+
- pip
- `pymavlink`
- clang and lld
- Git
- PX4-Autopilot source code
- Gazebo
- QGroundControl, optional

## Setup Instructions

You can either run the provided setup script or follow the manual steps below.
The full setup can easily take an hour or more depending on network speed and
how much of PX4 needs to be built from scratch.

## Option 1: Run the Setup Script

If the script is not run as root, it will ask for sudo once and keep that
authorization alive while setup is running.

```bash
sudo apt update
sudo apt install git
git clone https://github.com/kfreemanWrightState/PX4Fuzzing.git
cd PX4Fuzzing
git checkout customFuzzer
./setup_px4_fuzzing.sh

python3 customHarness.py --initial-startup-wait
```

Useful variations:

```bash
# short smoke test
python3 customHarness.py --initial-startup-wait --iterations 100

# reproducible run
python3 customHarness.py --initial-startup-wait --iterations 1000 --seed 1234
```

## Option 2: Run the Setup Steps Manually

## 1. Install Required Packages

```bash
sudo apt update && sudo apt-get upgrade -y

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
```

## 2. Install Python Mission Harness Dependencies

`customHarness.py` requires `pymavlink`.

```bash
sudo pip3 install --upgrade pymavlink --break-system-packages
```

## 3. Clone Required Repositories

Clone this project:

```bash
git clone https://github.com/kfreemanWrightState/PX4Fuzzing.git
cd PX4Fuzzing
git checkout customFuzzer
```

Clone PX4:

```bash
git clone https://github.com/PX4/PX4-Autopilot.git --recursive
```

Optional QGroundControl source:

```bash
git clone https://github.com/mavlink/qgroundcontrol.git
```

Optional QGroundControl AppImage:

```bash
wget https://d176tv9ibo4jno.cloudfront.net/latest/QGroundControl-x86_64.AppImage
chmod +x QGroundControl*
```

## 4. Prepare Runtime Directories

There is no `make_seeds.py` step on this branch because the harness generates
its own mission inputs internally.

```bash
mkdir -p findings
ulimit -c unlimited
```

## 5. Install PX4 Dependencies

```bash
cd PX4-Autopilot/
./Tools/setup/ubuntu.sh
```

## 6. Fix the PX4 Compile Flag Bug

```bash
grep -Rl 'O0-fprofile-arcs' . --exclude-dir=build | xargs sed -i 's/O0-fprofile-arcs/O0 -fprofile-arcs/g'
```

## 7. Build PX4 SITL With AddressSanitizer

```bash
CC=clang CXX=clang++ PX4_ASAN=1 make px4_sitl -j"$(nproc)"
```

This builds:

- PX4 SITL
- Gazebo simulation plugins
- ASan-instrumented PX4 binaries

## 8. Optional CPU Performance Mode

If you are running on bare metal rather than a VM, you can optionally switch
CPU governors to performance mode before long fuzzing runs.

```bash
echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
```

## 9. Run the Mission Fuzzer

Return to the project root and start the harness:

```bash
cd ..
python3 customHarness.py --initial-startup-wait
```

Example bounded run:

```bash
python3 customHarness.py --initial-startup-wait --iterations 1000 --seed 1234
```

## Current Approach

`customHarness.py` is a mission-specific fuzzer. On this branch, the harness:

- starts PX4 automatically through `startPX4.sh` if PX4 is not already running
- maintains parent and mission MAVLink channels
- uploads missions using the normal MAVLink mission protocol
- alternates between:
  - valid missions
  - invalid-prone mutated missions
  - recursive `DO_JUMP` missions
- increases allowable mission size as the run progresses
- detects PX4 death or channel failure and restarts PX4 and Gazebo automatically

This is different from the earlier AFL-based approach:

- no external seed corpus is required
- no `py-afl-fuzz` wrapper is used
- no stdin testcase handoff is used
- mission structure is generated and mutated inside the harness itself

## Output Files

During a run, the harness writes useful artifacts under `findings/`.

Important files include:

- `findings/combined_fuzz.log`
- `findings/first_20_missions.txt`
- `findings/px4_terminal.log`
- `findings/potential_crash_reports/`

Potential crash reports include:

- the current mission
- recent mission history
- mission metadata
- PX4 terminal log excerpts
- ASan excerpts when available

## Notes and Tips

- QGroundControl is optional and mainly useful for manual observation
- If Gazebo lingers after a crash, stop it with `pkill -f gz`
- `customHarness.py --help` shows all runtime options
- `--seed` makes runs reproducible
- `--iterations 0` means run forever

## References

### PX4 Documentation

- https://docs.px4.io/main/en/
- https://docs.px4.io/main/en/simulation/
- https://docs.px4.io/main/en/sim_gazebo_gz/
- https://docs.px4.io/main/en/getting_started/px4_basic_concepts#ground-control-stations

### MAVLink Messaging

- https://mavlink.io/en/about/overview.html

### MAVLink Security Research

- https://arxiv.org/html/2501.18874v2?utm_source=chatgpt.com
- https://cosicdatabase.esat.kuleuven.be/backend/publications/files/conferencepaper/2667?utm_source=chatgpt.com

### Other Embedded System Fuzzing Research

- https://www.ndss-symposium.org/wp-content/uploads/2023/02/ndss2023_f217_paper.pdf
- https://www.ndss-symposium.org/ndss-paper/pgfuzz-policy-guided-fuzzing-for-robotic-vehicles/
