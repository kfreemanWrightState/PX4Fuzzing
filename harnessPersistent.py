
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
from pathlib import Path

# third-party
from pymavlink import mavutil
import afl  # python-afl - provides afl.loop()/afl.init()

# --------------------- Configuration (env overrides) ---------------------
MAVLINK_HOST = os.getenv("MAVLINK_HOST", "127.0.0.1")   # PX4 IP for UDP
MAVLINK_PORT = int(os.getenv("MAVLINK_PORT", "14560")) # PX4 RX port (we send to this)
START_CMD = os.getenv("PX4_START_CMD", "./startPX4.sh")  # script/command to start PX4
PX4_PID_GREP = os.getenv("PX4_PID_GREP",
    "/usr/bin/cmake -E env PX4_SIM_MODEL=gz_x500")  # grep pattern to find px4 wrapper PIDs
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
# -------------------------------------------------------------------------

# simple logging helper (nonblocking to not slow AFL)
_log_fh = open(LOGFILE, "a", buffering=1) if LOGFILE else None
def log(msg):
    if _log_fh:
        _log_fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")

# -------------------- PX4 start / PID management -------------------------
def start_px4():
    """
    Start PX4 using START_CMD. We run the command via bash -lc so the START_CMD
    can be a shell script or complex command. Uses flock to avoid concurrent starts.
    After starting, pause briefly and snap PIDs to PID_FILE as JSON: {"px4_pids":[p1,p2]}
    """
    lock = RESTART_LOCK
    # Use flock to serialise restarts (shell 'flock -n FILE cmd')
    cmd = f"flock -n {lock} -c '{START_CMD}'"
    log(f"[start_px4] running: {cmd}")
    # Run asynchronously; do not wait for it to exit (it should exec px4 or a supervisor)
    subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Give PX4 some time to appear, then snapshot pids
    time.sleep(4.0)
    snap_px4_pids()

def snap_px4_pids():
    """
    Find px4-related PIDs using ps+grep pattern (PX4_PID_GREP) and write JSON
    to PID_FILE: {"px4_pids": [pid1, pid2, ...]}
    """
    try:
        # run ps aux and filter by pattern
        out = subprocess.check_output(["bash", "-lc",
            f"ps aux | grep '[{PX4_PID_GREP[0]}]{PX4_PID_GREP[1:]}' | awk '{{print $2}}' || true"],
            stderr=subprocess.DEVNULL, universal_newlines=True, timeout=2.0)
        # Keep tokens that look like ints, unique, and convert
        pids = []
        for line in out.strip().splitlines():
            try:
                p = int(line.strip())
                if p not in pids: pids.append(p)
            except Exception:
                continue
        if not pids:
            log("[snap_px4_pids] no px4 pids found")
        # Write JSON array (may be one or more PIDs)
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

# -------------------- MAVLink persistent connection & lifeline -------------
tx = None
_tx_lock = threading.Lock()
_last_hb = 0.0

def connect_tx():
    """Create a persistent udpout MAVLink TX connection and send an initial heartbeat."""
    global tx, _last_hb
    with _tx_lock:
        try:
            # use udpout so we actively send to PX4 and avoid binding conflicts
            conn_str = f"udpout:{MAVLINK_HOST}:{MAVLINK_PORT}"
            tx = mavutil.mavlink_connection(conn_str, source_system=250)
            # send an initial heartbeat so PX4 learns our addr
            tx.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_GCS, mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                                  0, 0, mavutil.mavlink.MAV_STATE_ACTIVE)
            _last_hb = time.time()
            log("[connect_tx] connected and initial heartbeat sent")
            return True
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
    """
    Called when harness detects px4 PID died or became zombie.
    Attempt to restart PX4 via start_px4(). The start function uses flock to avoid races.
    """
    log("[handle_px4_failure_and_restart] px4 failure detected; restarting")
    try:
        # Try to kill any detected stale px4 parents matching pattern (risky but useful)
        try:
            subprocess.run(f"pkill -f '{PX4_PID_GREP}'", shell=True, check=False)
        except Exception:
            pass
        start_px4()
        # reconnect MAVLink tx after a short wait
        time.sleep(10.0)
        connect_tx()
    except Exception as e:
        log(f"[handle_px4_failure_and_restart] restart error: {e}")

# ---------------------- AFL persistent harness loop -----------------------
def afl_main_loop():
    # Prepare TX connection and lifeline threads BEFORE afl.init() ideally so parent keeps connection:
    connect_tx()
    hb_stop = threading.Event()
    sp_stop = threading.Event()
    hb_t = threading.Thread(target=gcs_heartbeat_thread, args=(hb_stop,), daemon=True)
    sp_t = threading.Thread(target=setpoint_thread, args=(sp_stop,), daemon=True)
    hb_t.start(); sp_t.start()

    # Best-effort: set Offboard and arm once at start
    set_mode_offboard_once()
    arm_once(wait_s=6)
    wait_alt()

    # Initialize AFL forkserver (python-afl). After this, children will run the loop body.
    afl.init()
    log("[afl_main_loop] AFL forkserver started; entering loop")

    while afl.loop():
        # read single testcase from stdin (persistent mode)
        data = sys.stdin.buffer.read(MAX_INPUT)
        if not data:
            data = b'\xfe'

        # snapshot pids and liveness before send
        pids = read_px4_pids()
        before = [is_pid_alive_not_zombie(p) for p in pids]

        # send payload (fast)
        ok = send_raw(data)

        # small dwell for crash to manifest
        time.sleep(POST_SEND_MS / 1000.0)

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

# --------------------------- Entrypoint ----------------------------------
def main():
    # Optionally start PX4 now if user provided START_CMD
    if START_CMD:
        log(f"[main] starting PX4 using: {START_CMD}")
        start_px4()
        time.sleep(1.5)
    else:
        log("[main] START_CMD empty; assume PX4 already running")

    # Ensure we have a pid snapshot before entering AFL
    snap_px4_pids()

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
