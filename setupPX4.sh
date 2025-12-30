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


#unlimited size core dumps 
ulimit -c unlimited 

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
#CC=clang CXX=clang++ PX4_ASAN=1 make px4_sitl gz_x500 -j"$(nproc)"

CC=clang CXX=clang++ PX4_ASAN=1 make px4_sitl gz_x500 -j"$(nproc)"

PX4_CMAKE_BUILD_TYPE=Coverage CC=clang CXX=clang++ PX4_ASAN=1 make px4_sitl gz_x500 -j"$(nproc)"

#run "simulator_sih start" in the console to get an error and a crash and see the sanitizer messages



#In another Terminal
mkdir findings
python3 make_seeds.py
#AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 AFL_AUTORESUME=1 AFL_DEBUG=1 AFL_SKIP_BIN_CHECK=1 AFL_DUMB_FORKSRV=1 py-afl-fuzz -t 2000 -i seeds -o findings -- python3 harness3.py @@
#AFL_DEBUG=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 py-afl-fuzz -t 2000 -i seeds -o findings -- python3 harness3.py @@
AFL_FORKSRV_INIT_TMOUT=60000 AFL_DEBUG=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 py-afl-fuzz -t 2000 -i seeds -o findings -- python3 harnessPersistent.py @@
#In another Terminal
python3 px4_lifeline.py


#Code Coverage Instructions

PX4_CMAKE_BUILD_TYPE=Coverage CC=clang CXX=clang++ PX4_ASAN=1 PX4_MSAN=1 PX4_UBSAN=1 PX4_TSAN=1 make px4_sitl gz_x500 -j"$(nproc)"

cd ~/Documents/px4/PX4-Autopilot/build/px4_sitl_default

cat >/tmp/llvm-gcov <<'EOF'
#!/usr/bin/env bash
exec llvm-cov-18 gcov "$@"
EOF

chmod +x /tmp/llvm-gcov

lcov \
  --gcov-tool /tmp/llvm-gcov \
  --directory . \
  --capture \
  --output-file coverage.info \
  --ignore-errors version,gcov,source,inconsistent

ls -lh coverage.info

genhtml coverage.info  --ignore-errors inconsistent  --output-directory coverage_html   --legend   --demangle-cpp

xdg-open coverage_html/index.html



script to capture periodic code coverage
# 1) Build a special PX4 binary with coverage flags (you already did this).

# 2) At time T, grab the current AFL queue (corpus):
CORPUS=/path/to/afl/queue

# 3) Clean old .gcda
cd ~/Documents/px4/PX4-Autopilot/build/px4_sitl_default
find . -name '*.gcda' -delete

# 4) Replay corpus into PX4 using a small script/harness
# (e.g. loop over files in $CORPUS and send them via your MAVLink harness)

# 5) After replay finishes, collect coverage:
lcov --gcov-tool /tmp/llvm-gcov \
     --directory . \
     --capture \
     --output-file coverage_T.info \
     --ignore-errors version,gcov,source,inconsistent

# Optional: get summary numbers
lcov --summary coverage_T.info > coverage_T.summary.txt


make clean

PX4_CMAKE_BUILD_TYPE=Coverage \
CC=clang CXX=clang++ \
CMAKE_ARGS="-DCMAKE_INSTALL_PREFIX=$HOME/.local \
-DCMAKE_C_FLAGS='-fsanitize=address' \
-DCMAKE_CXX_FLAGS='-fsanitize=address' \
-DCMAKE_EXE_LINKER_FLAGS='-fsanitize=address'" \
make px4_sitl gz_x500 -j"$(nproc)"

cat > /tmp/llvm-gcov.sh <<'EOF'
#!/usr/bin/env bash
exec llvm-cov gcov "$@"
EOF
chmod +x /tmp/llvm-gcov.sh


mkdir -p coverage

ts=$(date +%Y%m%d_%H%M%S)
lcov \
  --directory build/px4_sitl_default \
  --base-directory . \
  --capture \
  --gcov-tool /tmp/llvm-gcov.sh \
  --ignore-errors mismatch,version,gcov,source,inconsistent  \
  -o "coverage/lcov_${ts}.info"

genhtml --ignore-errors empty,inconsistent,source --synthesize-missing "coverage/lcov_${ts}.info"  -o "coverage/html_${ts}" && firefox "$(realpath coverage/html_${ts}/index.html)" &