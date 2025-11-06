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
pip3 install --upgrade python-afl --break-system-packages

wget https://d176tv9ibo4jno.cloudfront.net/latest/QGroundControl-x86_64.AppImage
chmod +x QGroundControl*
./QGroundControl-x86_64.AppImage 

# preferred: stop and mask the service
#sudo systemctl mask --now ModemManager.service
#sudo usermod -aG dialout "$(id -un)"

#In another terminal 
cd PX4-Autopilot/

./Tools/setup/ubuntu.sh

#***************************
# running procedure, each on separate terminals
#with ASAN 
rm -rf build/px4_sitl_default
#sim build
make clean
#export UBSAN_OPTIONS="print_stacktrace=1:halt_on_error=1"
#export ASAN_OPTIONS="verbosity=1:detect_leaks=1:abort_on_error=1"
#export ASAN_OPTIONS="abort_on_error=1"
#export ASAN_SYMBOLIZER_PATH="$(command -v llvm-symbolizer-18 || command -v llvm-symbolizer || echo /usr/bin/llvm-symbolizer)"
CC=clang CXX=clang++ PX4_ASAN=1 make px4_sitl gz_x500 -j"$(nproc)"

#run "simulator_sih start" in the console to get an error and a crash and see the sanitizer messages

#In another Terminal
mkdir findings
python3 make_seeds.py
AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 AFL_AUTORESUME=1 AFL_DEBUG=1 AFL_SKIP_BIN_CHECK=1 AFL_DUMB_FORKSRV=1 py-afl-fuzz -t 2000 -i seeds -o findings -- python3 harness3.py @@

#In another Terminal
python3 px4_lifeline.py








