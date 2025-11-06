git clone https://github.com/PX4/PX4-Autopilot.git
git clone https://github.com/PX4/PX4-Autopilot.git --recursive
git clone https://github.com/mavlink/qgroundcontrol.git

sudo apt install gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-gl -y
sudo apt install libfuse2 -y
sudo apt install libxcb-xinerama0 libxkbcommon-x11-0 libxcb-cursor-dev -y
sudo apt-get install qemu-system-arm  

sudo apt update
sudo apt install -y \
  build-essential cmake ninja-build ccache \
  clang lld \
  libc6-dev libstdc++-14-dev \
  libc++-dev libc++abi-dev \
  python3 python3-pip git valgrind afl++

sudo apt update && sudo apt install -y afl++ python3 python3-pip




wget https://d176tv9ibo4jno.cloudfront.net/latest/QGroundControl-x86_64.AppImage
chmod +x QGroundControl*
./QGroundControl-x86_64.AppImage 



# preferred: stop and mask the service
sudo systemctl mask --now ModemManager.service
sudo usermod -aG dialout "$(id -un)"

cd qgroundcontrol


#In another terminal 
cd PX4-Autopilot/

./Tools/setup/ubuntu.sh



#***************************
# running procedure, each on seperate terminals


#with ASAN 
rm -rf build/px4_sitl_default
#sim build
make clean
export ASAN_OPTIONS=verbosity=1:detect_leaks=1:abort_on_error=1
export UBSAN_OPTIONS="print_stacktrace=1:halt_on_error=1"
export ASAN_SYMBOLIZER_PATH="$(command -v llvm-symbolizer-18 || command -v llvm-symbolizer)"


run "simulator_sih start" in the console to get an error and a crash

#without ASAN (new terminal) 
rm -rf build/px4_sitl_default
make clean
make px4_sitl gz_x500 -j"$(nproc)"


#Proves that sanatizer is running in PX4
strings build/px4_sitl_default/bin/px4 | grep -i asan | head

#can get rid of some of the gazeebo errors
rm -f build/px4_sitl_default/src/modules/simulation/gz_plugins/libGstCameraSystem.so
rm -f build/px4_sitl_default/src/modules/simulation/gz_plugins/libOpticalFlowSystem.so

ASAN_OPTIONS=verbosity=1:detect_leaks=1 \
CC=clang CXX=clang++ PX4_ASAN=1 make px4_sitl gz_x500

AFL_AUTORESUME=1 AFL_DEBUG=1 AFL_SKIP_BIN_CHECK=1 AFL_DUMB_FORKSRV=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 afl-fuzz -t 2000 -i seeds -o findings -- python3 harness3.py @@

python3 px4_lifeline.py
#********************************
export ASAN_OPTIONS=verbosity=1:detect_leaks=1:abort_on_error=1
export UBSAN_OPTIONS="print_stacktrace=1:halt_on_error=1:log_path=ubsan"
export ASAN_SYMBOLIZER_PATH="$(command -v llvm-symbolizer-18 || command -v llvm-symbolizer)"

CC=clang CXX=clang++ PX4_ASAN=1 make px4_sitl gz_x500 -j"$(nproc)"
CC=clang CXX=clang++ PX4_ASAN=1 make px4_sitl gz_x500___valgrind -j"$(nproc)"


make clean

#with ASAN 
rm -rf build/px4_sitl_default
# 1) Make sure the symbolizer is available (Ubuntu 24.04 uses versioned names)
export ASAN_SYMBOLIZER_PATH="$(command -v llvm-symbolizer-18 || command -v llvm-symbolizer || echo /usr/bin/llvm-symbolizer)"

# 2) Strong ASan settings for good stacks and logs
export ASAN_OPTIONS="symbolize=1:fast_unwind_on_malloc=0:malloc_context_size=50:detect_leaks=1:handle_segv=1:handle_abort=1:use_sigaltstack=1:detect_stack_use_after_return=1:verbosity=1"
#:log_path=/tmp/ubsan

# (optional but useful for integer UB reports)
export UBSAN_OPTIONS="print_stacktrace=1"
#:log_path=/tmp/ubsan

# 3) Allow core dumps if you want to inspect with gdb later
ulimit -c unlimited
CC=clang CXX=clang++ PX4_ASAN=1 make px4_sitl gz_x500 -j"$(nproc)"



#qemu build
make px4_fmu-v5
cd px4_fmu-v5_default

qemu-system-arm -nographic \
-machine virt,virtualization=off,gic-version=2 \
-kernel build/px4_fmu-v5_default/px4_fmu-v5_default.elf

# save current pattern (so you can restore it later)
orig=$(cat /proc/sys/kernel/core_pattern)
printf '%s\n' "$orig" > core_pattern.backup

# set to plain 'core' for this session
echo core | sudo tee /proc/sys/kernel/core_pattern >/dev/null

# run AFL++
afl-fuzz -i seeds -o findings -- /usr/bin/python3 ./harness.py @@

# restore original pattern when done
sudo tee /proc/sys/kernel/core_pattern >/dev/null < core_pattern.backup

afl-fuzz AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 -i seeds -o findings -- python3 harness.py @@ 

pip3 install --upgrade pymavlink --break-system-packages
pip3 install --upgrade python-afl --break-system-packages
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pymavlink









