# PX4 Fuzzing Environment Setup (AFL++ Persistent Mode)

This README provides a complete guide for installing all required packages, building PX4 SITL with sanitizers, preparing AFL++ seeds, and running persistent-mode fuzzing against PX4 using a MAVLink harness.

---

## 1. Install All Required Packages (One Command Block)

Run this entire block at once:

```bash
sudo apt update && sudo apt-get upgrade -y

sudo apt install -y \
  gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-gl \
  libfuse2 \
  libxcb-xinerama0 libxkbcommon-x11-0 libxcb-cursor-dev \
  build-essential cmake ninja-build ccache \
  clang lld \
  libc6-dev libstdc++-14-dev \
  libc++-dev libc++abi-dev \
  python3 python3-pip git \
  valgrind afl++ \
  wget unzip tar
```

Install python-afl:

```bash
pip3 install --upgrade python-afl --break-system-packages
```

---

## 2. Clone Required Repositories

Clone your project:

```bash
git clone https://github.com/kfreemanWrightState/PX4Fuzzing.git
cd PX4Fuzzing
```

Clone PX4:

```bash
git clone https://github.com/PX4/PX4-Autopilot.git --recursive
```

Clone QGroundControl source (optional):

```bash
git clone https://github.com/mavlink/qgroundcontrol.git
```

Download the prebuilt QGroundControl AppImage:

```bash
wget https://d176tv9ibo4jno.cloudfront.net/latest/QGroundControl-x86_64.AppImage
chmod +x QGroundControl*
```

---

## 3. Prepare AFL++ Directory and Seeds

Create AFL findings directory:

```bash
mkdir findings
```

Generate seeds (basic MAVLink templates):

```bash
python3 make_seeds.py
```

Allow large sanitizer core dumps:

```bash
ulimit -c unlimited
```

---

## 4. Build PX4 With Sanitizers Enabled

Enter PX4:

```bash
cd PX4-Autopilot/
```

Install PX4-required dependencies:

```bash
./Tools/setup/ubuntu.sh
```

Build PX4 SITL with AddressSanitizer:

```bash
CC=clang CXX=clang++ PX4_ASAN=1 make px4_sitl px4_sitl_default -j"$(nproc)"
```

This builds:

- PX4 SITL  
- Gazebo simulation plugins  
- ASan/UBSan-instrumented PX4 binaries  

---

## 5. Run AFL++ Persistent Fuzzing

Run the harness in a second terminal:

```bash
AFL_FORKSRV_INIT_TMOUT=60000 AFL_DEBUG=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 \
py-afl-fuzz -t 2000 -i seeds -o findings -- python3 harnessPersistent.py @@
```

AFL++ will:

- start a forkserver  
- reuse the same Python process (persistent mode)  
- inject fuzz inputs into MAVLink messages  
- restart PX4 after crashes  
- log crashes into `findings/`  

---

## Notes and Tips

- Gazebo often lingers after crashes. Remove it with:

```bash
pkill -f gz
```

- QGroundControl is optional, but useful for observing PX4 behavior.
- PX4 sanitizer output appears directly in the PX4 terminal window.
- Core dumps (if enabled) are stored in the current directory.

---

