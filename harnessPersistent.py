
#!/usr/bin/env python3
"""
combined_fuzz_lifeline.py

Single-script workflow:
 - optionally start PX4 (via START_CMD),
 - create a persistent MAVLink 'udpout:' connection so we don't reconnect each testcase,
 - run lifeline threads: GCS heartbeat (2 Hz) and Offboard position setpoints (20 Hz),
 - run python-afl persistent loop: read testcase from stdin, send to PX4 quickly,
 - monitor PX4 PIDs (zombie or missing) and restart PX4 if needed (uses flock to avoid races).

Notes:
 - For reliability, make your START_CMD spawn px4 directly (avoid gnome-terminal parent).
 - Environment overrides (defaults below) are clearly documented.
 - Requires: python-afl, pymavlink (pip install python-afl pymavlink)
"""
import os
import sys
import time
import json
import threading
import subprocess
import signal
import socket
import select
import pdb
from pathlib import Path

# third-party
from pymavlink import mavutil
import afl  # python-afl - provides afl.loop()/afl.init()

# --------------------- Configuration (env overrides) ---------------------
MAVLINK_HOST = os.getenv("MAVLINK_HOST", "127.0.0.1")   # PX4 IP for UDP
MAVLINK_PORT = int(os.getenv("MAVLINK_PORT", "14540")) # PX4 RX port (we send to this)
START_CMD = os.getenv("PX4_START_CMD", "./startPX4.sh")  # script/command to start PX4

# Path fragment that uniquely identifies the *real* px4 binary (not the cmake wrapper)
PX4_DIR = os.getenv("PX4_DIR", os.path.join(os.getcwd(), "PX4-Autopilot"))
PX4_BIN_MATCH = os.getenv(
    "PX4_BIN_MATCH",
    os.path.join(PX4_DIR, "build/px4_sitl_default/bin/px4")
)

PX4_PID_GREP = os.getenv("PX4_PID_GREP",
    "/usr/bin/cmake -E env PX4_SIM_MODEL=gz_x500")  # grep pattern to find px4 wrapper PIDs
PX4_PID_GREP_LIST = [
    "/usr/bin/cmake -E env PX4_SIM_MODEL=gz_x500",
    "/build/px4_sitl_default/bin/px4"
]
PID_FILE = os.getenv("PID_FILE", "/tmp/px4_pids.json")
HEARTBEAT_MS = int(os.getenv("HEARTBEAT_MS", "500"))    # keepalive heartbeat interval (ms)
SETPOINT_HZ = int(os.getenv("SETPOINT_HZ", "20"))      # setpoint stream rate
TARGET_X = float(os.getenv("TARGET_X", "0"))
TARGET_Y = float(os.getenv("TARGET_Y", "0"))
TARGET_Z = float(os.getenv("TARGET_Z", "-5.0"))        # LOCAL_NED negative = up
CONFIRM_ALT_M = float(os.getenv("CONFIRM_ALT_M", "2.0"))
POST_SEND_MS = int(os.getenv("POST_SEND_MS", "30"))    # dwell after send to let crash manifest
MAX_INPUT = int(os.getenv("MAX_INPUT", "8192"))       # max bytes read from AFL
LOGFILE = os.getenv("HARNESS_LOG", "/tmp/combined_fuzz.log")
RESTART_LOCK = os.getenv("PX4_RESTART_LOCK", "/tmp/px4_restart.lock")

UNSUPPORTED_COMMAND_ID = int(os.getenv("UNSUPPORTED_COMMAND_ID", "5000"))

AFL_ITERATIONS = 1000

vehicle_ready = False
# -------------------------------------------------------------------------

# simple logging helper (nonblocking to not slow AFL)
_log_fh = open(LOGFILE, "a", buffering=1) if LOGFILE else None
def log(msg):
    if _log_fh:
        _log_fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")

# -------------------- PX4 start / PID management -------------------------
def ensure_px4_running():
    """
    Check if PX4 processes (as listed in PID_FILE) are alive.
    If none or any are missing/zombie, start PX4 via start_px4().
    Otherwise, skip restart and log that PX4 is already running.
    """
    pids = read_px4_pids()
    if not pids:
        log("[ensure_px4_running] no pids found; starting PX4")
        start_px4()
        return

    alive = [is_pid_alive_not_zombie(p) for p in pids]
    if all(alive):
        log(f"[ensure_px4_running] PX4 appears already running (pids={pids})")
        return

    log(f"[ensure_px4_running] some PX4 pids dead/zombie ({pids}); restarting")
    start_px4()

def start_px4():
    """
    Launch PX4 SITL in a new GNOME Terminal (interactive output).
    - Kills any existing px4 binary and Gazebo first (clean reset).
    - Serialized by RESTART_LOCK to avoid multiple terminals.
    - Snapshots only the real px4 PID(s).
    - Waits ~15s for full boot/settle.
    """
    px4_dir = PX4_DIR
    num_procs = os.getenv("NUM_PROCS", str(os.cpu_count() or 4))
    make_cmd = f"CC=clang CXX=clang++ PX4_ASAN=1 make px4_sitl gz_x500 -j{num_procs}"

    # Clean slate
    kill_existing_px4()
    kill_gazebo()

    term_under_flock = [
        "bash", "-lc",
        f"flock -n {RESTART_LOCK} -c "
        f"'gnome-terminal --working-directory \"{px4_dir}\" -- "
        f"bash -lc \"{make_cmd}; exec bash\"'"
    ]

    log(f"[start_px4] launching GNOME terminal in {px4_dir} with: {make_cmd}")
    try:
        subprocess.Popen(term_under_flock,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception as e:
        log(f"[start_px4] error launching terminal: {e}")
        raise

    # Let processes appear, then record *only* real px4 PID(s)
    time.sleep(5.0)
    snap_px4_pids()

    log("[start_px4] waiting 15s to let PX4 and simulator fully settle")
    time.sleep(15.0)
    log("[start_px4] start complete")

def snap_px4_pids():
    """
    Find PIDs of the actual px4 runtime binary only (not the cmake wrapper),
    and write JSON: {"px4_pids": [pid1, ...]} to PID_FILE.
    """
    try:
        # Use the bracket-trick so grep doesn't match itself
        pat = PX4_BIN_MATCH
        if not pat:
            log("[snap_px4_pids] PX4_BIN_MATCH empty")
            pids = []
        else:
            out = subprocess.check_output(
                ["bash", "-lc",
                 f"ps aux | grep '[{pat[0]}]{pat[1:]}' | awk '{{print $2}}' || true"],
                stderr=subprocess.DEVNULL, universal_newlines=True, timeout=2.0)
            pids = []
            for line in out.strip().splitlines():
                try:
                    pid = int(line.strip())
                    if pid not in pids:
                        pids.append(pid)
                except:
                    pass
        with open(PID_FILE, "w", encoding="utf-8") as f:
            json.dump({"px4_pids": pids}, f)
        log(f"[snap_px4_pids] wrote {PID_FILE}: {pids}")
        return pids
    except Exception as e:
        log(f"[snap_px4_pids] error: {e}")
        return []

def read_px4_pids():
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return [int(x) for x in d.get("px4_pids", [])]
    except Exception as e:
        log(f"[read_px4_pids] error: {e}")
        return []

def is_pid_alive_not_zombie(pid):
    """
    Return True if /proc/<pid> exists and is not in state 'Z' (zombie).
    """
    try:
        st = Path(f"/proc/{pid}/stat").read_text()
        # safe parse: split after ") "
        after = st.split(") ", 1)[1]
        state = after.split()[0]
        return state != "Z"
    except FileNotFoundError:
        return False
    except Exception:
        # conservatively assume alive if weird
        return True
def kill_existing_px4(timeout_s: float = 8.0):
    """
    Terminate any running real px4 binary (not wrappers). SIGTERM, wait, then SIGKILL.
    """
    try:
        # Simpler: no complex escaping required
        out = subprocess.check_output(
            ["bash", "-lc", f"pgrep -f '{PX4_BIN_MATCH}' || true"],
            stderr=subprocess.DEVNULL, universal_newlines=True, timeout=2.0)
        pids = [int(x) for x in out.strip().split() if x.isdigit()]
        if not pids:
            return
        log(f"[kill_existing_px4] terminating px4 pids: {pids}")
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            alive = [pid for pid in pids if Path(f"/proc/{pid}").exists()]
            if not alive:
                break
            time.sleep(0.2)
        # Hard kill stragglers
        for pid in pids:
            try:
                if Path(f"/proc/{pid}").exists():
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    except Exception as e:
        log(f"[kill_existing_px4] error: {e}")

def kill_gazebo(timeout_s: float = 10.0):
    """
    Kill any lingering Gazebo (gz sim) processes so the world resets cleanly.
    """
    try:
        # Broad but safe matches for SITL runs
        pats = [
            r"^\s*gz sim\b",
            r"/Tools/simulation/gz/worlds/"
        ]
        for pat in pats:
            subprocess.run(f"pkill -f \"{pat}\"", shell=True, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Wait until no gz sim remains
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            rc = subprocess.run("pgrep -f '^\\s*gz sim\\b' >/dev/null",
                                shell=True)
            if rc.returncode != 0:
                break
            time.sleep(0.3)
        # Extra sweep for gazebo/ignition transport helpers
        subprocess.run("pkill -f 'ign transport|gazebo|gz-'", shell=True, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log(f"[kill_gazebo] error: {e}")

# -------------------- MAVLink persistent connection & lifeline -------------
tx = None
_tx_lock = threading.Lock()
_last_hb = 0.0

def connect_tx(timeout=20):
    """Create a persistent udpout MAVLink TX connection and send an initial heartbeat."""
    global tx, _last_hb
    with _tx_lock:
        try:
            # use udpout so we actively send to PX4 and avoid binding conflicts
            conn_str = f"udp:{MAVLINK_HOST}:{MAVLINK_PORT}"
            tx = mavutil.mavlink_connection(conn_str, source_system=250)
            t0 = time.time()
            while time.time() - t0 < timeout:
                if tx.recv_match(type='HEARTBEAT', blocking=True, timeout=1):
                    _last_hb = time.time()
                    log("[connect_tx] connected and initial heartbeat sent")
                    return True
            raise TimeoutError("No HEARTBEAT from PX4")
        except Exception as e:
            log(f"[connect_tx] failed: {e}")
            tx = None
            return False

def ensure_tx():
    if tx is None:
        return connect_tx()
    return True

def send_raw(buf):
    """Send raw bytes quickly. Returns True on success."""
    try:
        with _tx_lock:
            if tx is None:
                if not connect_tx():
                    return False
            # write raw datagram; pymavlink exposes a write() on the connection
            tx.mav.write(buf)
        return True
    except Exception as e:
        log(f"[send_raw] error: {e}")
        return False

# ---------------- lifeline threads: heartbeat + setpoints -------------------
def gcs_heartbeat_thread(stop_evt):
    global _last_hb
    while not stop_evt.is_set():
        try:
            with _tx_lock:
                if tx:
                    tx.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_GCS,
                                          mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                                          0, 0, mavutil.mavlink.MAV_STATE_ACTIVE)
                    _last_hb = time.time()
        except Exception:
            pass
        stop_evt.wait(HEARTBEAT_MS / 1000.0)

def setpoint_thread(stop_evt):
    mask = (
        mavutil.mavlink.POSITION_TARGET_TYPEMASK_VX_IGNORE |
        mavutil.mavlink.POSITION_TARGET_TYPEMASK_VY_IGNORE |
        mavutil.mavlink.POSITION_TARGET_TYPEMASK_VZ_IGNORE |
        mavutil.mavlink.POSITION_TARGET_TYPEMASK_AX_IGNORE |
        mavutil.mavlink.POSITION_TARGET_TYPEMASK_AY_IGNORE |
        mavutil.mavlink.POSITION_TARGET_TYPEMASK_AZ_IGNORE |
        mavutil.mavlink.POSITION_TARGET_TYPEMASK_YAW_IGNORE |
        mavutil.mavlink.POSITION_TARGET_TYPEMASK_YAW_RATE_IGNORE
    )
    while not stop_evt.is_set():
        try:
            with _tx_lock:
                if tx:
                    tx.mav.set_position_target_local_ned_send(
                        int(time.time()*1000) & 0xFFFFFFFF,
                        tx.target_system or 1, tx.target_component or 1,
                        mavutil.mavlink.MAV_FRAME_LOCAL_NED,
                        mask, TARGET_X, TARGET_Y, TARGET_Z,
                        0,0,0, 0,0,0, 0, 0
                    )
        except Exception:
            pass
        stop_evt.wait(1.0 / SETPOINT_HZ)

# ----------------- Offboard switch/arm helpers (best-effort) ----------------

def prepare_vehicle_offboard():
    """Warmup + arm + OFFBOARD; set vehicle_ready True on success."""
    global vehicle_ready
    # 2s warm-up (HB + setpoints already running)
    t0 = time.time()
    while time.time() - t0 < 2.0:
        with _tx_lock:
            if tx: tx.recv_match(blocking=False)
        time.sleep(0.05)

    # (SITL-friendly) relax a few checks if you want:
    # set_param_tx("COM_ARM_WO_GPS", 1.0)
    # set_param_tx("COM_RCL_EXCEPT", 4.0)

    armed = arm_once(wait_s=10)
    set_mode_offboard_once()
    vehicle_ready = armed  # mark ready if we at least armed (OFFBOARD may follow)
    return vehicle_ready

def set_mode_offboard_once():
    try:
        with _tx_lock:
            if tx:
                PX4_MAIN_OFFBOARD = 6
                tx.mav.command_long_send(tx.target_system or 0, tx.target_component or 0,
                                         mavutil.mavlink.MAV_CMD_DO_SET_MODE, 0,
                                         1, PX4_MAIN_OFFBOARD, 0, 0, 0, 0, 0)
                tx.recv_match(type='COMMAND_ACK', blocking=False)
                log("[set_mode_offboard_once] sent command")
    except Exception as e:
        log(f"[set_mode_offboard_once] error: {e}")

def arm_once(wait_s=6):
    try:
        with _tx_lock:
            if tx:
                tx.mav.command_long_send(tx.target_system or 0, tx.target_component or 0,
                                         mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0,
                                         1, 0,0,0,0,0,0)
    except Exception as e:
        log(f"[arm_once] error: {e}")
    # best-effort wait for heartbeat armed flag (non-blocking)
    t0 = time.time()
    while time.time() - t0 < wait_s:
        try:
            with _tx_lock:
                if tx:
                    hb = tx.recv_match(type='HEARTBEAT', blocking=True, timeout=1.0)
                    if hb and (getattr(hb, "base_mode", 0) & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED):
                        log("[arm_once] armed confirmed")
                        return True
        except Exception:
            pass
    log("[arm_once] arm not confirmed")
    return False

def wait_alt(min_alt_m=CONFIRM_ALT_M, timeout_s=30):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            with _tx_lock:
                if tx:
                    msg = tx.recv_match(type='GLOBAL_POSITION_INT', blocking=True, timeout=1.0)
                    if msg and getattr(msg, "relative_alt", None) is not None:
                        if msg.relative_alt/1000.0 >= min_alt_m:
                            log("[wait_alt] altitude confirmed")
                            return True
        except Exception:
            pass
    log("[wait_alt] altitude not confirmed")
    return False

# ---------------------- Crash handling & restart ---------------------------
def handle_px4_failure_and_restart():
    global vehicle_ready
    log("[restart] px4 failure detected; restarting")
    try:
        kill_existing_px4()
        kill_gazebo()
        start_px4()
        # Reconnect TX (retry a few times)
        for _ in range(10):
            if connect_tx():
                break
            time.sleep(1.0)
        # Re-prepare vehicle once per restart
        if connect_tx():
            if prepare_vehicle_offboard():
                log("[restart] vehicle re-armed and in OFFBOARD")
            else:
                log("[restart] vehicle prepare failed; will keep trying during fuzz")
        arm_once(wait_s=6)
        wait_alt()
        vehicle_ready = True
    except Exception as e:
        log(f"[restart] error: {e}")
        vehicle_ready = False

# ---------------------- AFL persistent harness loop -----------------------
def safe_afl_init():
    try: afl.init()
    except RuntimeError as e:
        if "AFL already initialized" not in str(e): raise

def afl_main_loop():
    # Prepare TX connection and lifeline threads BEFORE afl.init() ideally so parent keeps connection:
    connect_tx()
    hb_stop = threading.Event()
    sp_stop = threading.Event()
    hb_t = threading.Thread(target=gcs_heartbeat_thread, args=(hb_stop,), daemon=True)
    sp_t = threading.Thread(target=setpoint_thread, args=(sp_stop,), daemon=True)
    hb_t.start(); sp_t.start()

    # Allow ~2 s of heartbeats & setpoints so PX4 sees a valid GCS
    log("[afl_main_loop] letting heartbeats run 2s so PX4 detects GCS before offboard/arm")
    t0 = time.time()
    while time.time() - t0 < 2.0:
        try:
            if tx:
                tx.recv_match(blocking=False)
        except Exception:
            pass
        time.sleep(0.05)

    # Best-effort: set Offboard and arm once at start
    set_mode_offboard_once()
    arm_once(wait_s=6)
    wait_alt()

    # Initialize AFL forkserver (python-afl). After this, children will run the loop body.
    #afl.init()
    #safe_afl_init()
    log("[afl_main_loop] AFL forkserver started; entering loop")

    #pdb.set_trace()
    while afl.loop():
    #while True:
        # read single testcase from stdin (persistent mode)
        data = sys.stdin.buffer.read(MAX_INPUT)
        if not data:
            data = b'\xfe'

        # snapshot pids and liveness before send
        pids = read_px4_pids()
        before = [is_pid_alive_not_zombie(p) for p in pids]
        #send_unsupported_command_via_mav()
        #send_unsupported_command_raw_udp()
        #print("start crash")
        # send payload (fast)

        ok = send_raw(data)

        # small dwell for crash to manifest
        time.sleep(POST_SEND_MS / 1000.0)

        #time.sleep(10)
        #print("end crash")
        # re-check
        after = [is_pid_alive_not_zombie(p) for p in pids]

        # if any went from alive->dead/zombie, then signal crash *and* request restart
        for b, a, pid in zip(before, after, pids):
            if b and not a:
                log(f"[afl_main_loop] detected px4 pid {pid} died/zombie; triggering restart and crashing harness")
                # request restart in background (don't block)
                threading.Thread(target=handle_px4_failure_and_restart, daemon=True).start()
                # crash this process deliberately so AFL records the crash
                os.kill(os.getpid(), signal.SIGSEGV)

    # cleanup (shouldn't normally reach here while AFL runs)
    hb_stop.set(); sp_stop.set()
    sp_t.join(timeout=1.0); hb_t.join(timeout=1.0)

# --------------------------- Send UDP Messages ---------------------------

def send_unsupported_command_via_mav():
    """
    Send an unsupported COMMAND_LONG via pymavlink.
    PX4 should reply with COMMAND_ACK: RESULT=UNSUPPORTED (and print a log).
    """
    try:
        with _tx_lock:
            if tx:
                log("[send_unsupported_command_via_mav] sending unsupported command...")
                tx.mav.command_long_send(
                    tx.target_system or 0,
                    tx.target_component or 0,
                    UNSUPPORTED_COMMAND_ID,
                    0, 0,0,0,0,0,0,0
                )
                _ = tx.recv_match(type='COMMAND_ACK', blocking=False, timeout=0.05)
    except Exception:
        pass


def send_unsupported_command_raw_udp():
    """
    Fallback: craft a minimal (likely invalid) UDP payload to the PX4 port.
    This is less structured but still causes PX4 to log an unsupported command
    when it resolves to a mis-parse / unsupported command.
    """
    try:
        log("[send_unsupported_command_via_mav] sending unsupported command...")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setblocking(False)
        # send a tiny blob containing the command id as plain bytes (not proper MAVLink)
        # PX4's UDP layer will get this and likely log a parse error — still an "error" event.
        payload = b'CMD' + bytes(str(UNSUPPORTED_COMMAND_ID), 'ascii')
        s.sendto(payload, (MAVLINK_HOST, MAVLINK_PORT))
        s.close()
    except Exception:
        pass

# --------------------------- Entrypoint ----------------------------------
def main():
    log("[main] checking PX4 state...")
    ensure_px4_running()

    try:
        afl_main_loop()
    except KeyboardInterrupt:
        log("[main] keyboard interrupt, exiting")
    except Exception as e:
        log(f"[main] unexpected error: {e}")
    finally:
        try:
            if _log_fh:
                _log_fh.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()
