
#!/usr/bin/env python3
"""
combined_fuzz_lifeline.py

Single-script workflow:
 - optionally start PX4 (via START_CMD),
 - create a persistent MAVLink 'udpout:' connection so we don't reconnect each testcase,
 - run lifeline threads: GCS heartbeat (2 Hz) and Offboard position setpoints (20 Hz),
 - run a Python-managed fork loop: generate testcase bytes, send to PX4 quickly,
 - monitor PX4 PIDs (zombie or missing) and restart PX4 if needed (uses flock to avoid races).

Notes:
 - For reliability, make your START_CMD spawn px4 directly (avoid gnome-terminal parent).
 - Environment overrides (defaults below) are clearly documented.
 - Requires: pymavlink
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
import struct
from pathlib import Path
import textwrap
import hashlib
import random
import math

# third-party
from pymavlink import mavutil
from pymavlink.dialects.v20 import common as mavlink2  # for explicit enums
#from cov_reporter import start_coverage_thread_from_ini

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
MAX_INPUT = int(os.getenv("MAX_INPUT", "8192"))        # max bytes generated per testcase
MIN_INPUT = int(os.getenv("MIN_INPUT", "1"))
PX4_STARTUP_WAIT_S = float(os.getenv("PX4_STARTUP_WAIT_S", "65"))
LOGFILE          = os.getenv("HARNESS_LOG", "/tmp/combined_fuzz.log")
RESTART_LOCK     = os.getenv("PX4_RESTART_LOCK", "/tmp/px4_restart.lock")

GCS_SYSTEM_ID    = int(os.getenv("GCS_SYSTEM_ID", "250"))
GCS_COMPONENT_ID = int(os.getenv(
    "GCS_COMPONENT_ID",
    str(mavutil.mavlink.MAV_COMP_ID_MISSIONPLANNER)
))

UNSUPPORTED_COMMAND_ID = int(os.getenv("UNSUPPORTED_COMMAND_ID", "5000"))

FORKSERVER_ITERATIONS = int(os.getenv("FORKSERVER_ITERATIONS", "1000"))
HARNESS_SEED = os.getenv("HARNESS_SEED")
MISSION_MIN_LEN = int(os.getenv("MISSION_MIN_LEN", "10"))
MISSION_START_MAX_LEN = int(os.getenv("MISSION_START_MAX_LEN", "30"))
MISSION_MAX_LEN_CAP = int(os.getenv("MISSION_MAX_LEN_CAP", "1000"))
MISSION_LEN_GROWTH_STEP = int(os.getenv("MISSION_LEN_GROWTH_STEP", "5"))
MISSION_LEN_GROWTH_EVERY = int(os.getenv("MISSION_LEN_GROWTH_EVERY", "250"))
REPORT_SNAPSHOT_INTERVAL_S = int(os.getenv("REPORT_SNAPSHOT_INTERVAL_S", str(4 * 60 * 60)))

vehicle_ready = False

# Upload Mission Fuzzing
#---------------------------------------------------------------------

def _recv_match_short(m, timeout_s=0.2):
    """Short recv to avoid holding _tx_lock forever (keeps heartbeat thread alive)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with _tx_lock:
            msg = m.recv_match(blocking=False)
        if msg is not None:
            return msg
        time.sleep(0.005)
    return None


def upload_mission_safe(m, items, mission_type=mavutil.mavlink.MAV_MISSION_TYPE_MISSION, timeout_s=8.0):
    """
    Mission upload handshake but avoids long blocking under _tx_lock.
    Returns MAV_MISSION_RESULT code (0 == ACCEPTED).
    """
    count = len(items)

    with _tx_lock:
        m.mav.mission_count_send(m.target_system, m.target_component, count, mission_type)

    sent = 0
    deadline = time.time() + timeout_s

    while time.time() < deadline:
        msg = _recv_match_short(m, timeout_s=0.3)
        if not msg:
            continue

        t = msg.get_type()

        if t in ("MISSION_REQUEST_INT", "MISSION_REQUEST"):
            seq = int(msg.seq)
            if seq < 0 or seq >= count:
                raise RuntimeError(f"PX4 requested out-of-range seq={seq} count={count}")

            it = items[seq]
            with _tx_lock:
                m.mav.mission_item_int_send(
                    m.target_system,
                    m.target_component,
                    it["seq"],
                    it["frame"],
                    it["command"],
                    it["current"],
                    it["autocontinue"],
                    it["param1"], it["param2"], it["param3"], it["param4"],
                    it["x"], it["y"], it["z"],
                    mission_type
                )
            sent += 1
            continue

        if t == "MISSION_ACK":
            return int(msg.type)  # MAV_MISSION_RESULT

    raise RuntimeError(f"Timed out waiting for MISSION_ACK (sent={sent}/{count})")


def _u32(b):  # little endian
    return int.from_bytes(b, "little", signed=False)

def _s16(b):
    return int.from_bytes(b, "little", signed=True)

def _f32(b):
    return struct.unpack("<f", b)[0]

def _pick_from(buf, off, n, default=0):
    if off + n <= len(buf):
        return buf[off:off+n]
    return bytes([default]) * n


def mission_length_window(iteration: int):
    min_len = max(1, MISSION_MIN_LEN)
    start_max = max(min_len, MISSION_START_MAX_LEN)
    cap = max(start_max, MISSION_MAX_LEN_CAP)
    growth_step = max(1, MISSION_LEN_GROWTH_STEP)
    growth_every = max(1, MISSION_LEN_GROWTH_EVERY)
    growth_rounds = max(0, iteration) // growth_every
    current_max = min(cap, start_max + growth_rounds * growth_step)
    return min_len, current_max


def mutate_mission_from_bytes(buf, base_items, want_len=10, iteration=0):
    """
    Semantic mutation: keeps mission structure valid but changes fields based on buf.
    Returns a new list of items (MISSION_ITEM_INT compatible).
    """
    items = [dict(x) for x in base_items]

    min_len, max_len = mission_length_window(iteration)

    # Grow the allowable mission length as the fuzz run progresses.
    if len(buf) >= 1:
        span = max_len - min_len + 1
        L = min_len + (buf[0] % span)
    else:
        L = min(max_len, max(min_len, want_len))

    # Expand/shrink by repeating last waypoint-ish item
    while len(items) < L:
        it = dict(items[-1])
        it["seq"] = len(items)
        it["current"] = 0
        items.append(it)
    items = items[:L]

    # Exactly one "current" item (usually seq0)
    for it in items:
        it["current"] = 0
    items[0]["current"] = 1

    # Command pools (safe-ish but interesting)
    NAV_CMDS = [
        mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
        mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
        mavutil.mavlink.MAV_CMD_NAV_LOITER_TIME,
        mavutil.mavlink.MAV_CMD_NAV_LOITER_TURNS,
        mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH,
        mavutil.mavlink.MAV_CMD_NAV_LAND,
    ]
    DO_CMDS = [
        mavutil.mavlink.MAV_CMD_DO_CHANGE_SPEED,
        mavutil.mavlink.MAV_CMD_DO_SET_CAM_TRIGG_DIST,
        mavutil.mavlink.MAV_CMD_DO_SET_ROI_LOCATION,
    ]

    FRAMES = [
        mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
        mavutil.mavlink.MAV_FRAME_GLOBAL_INT,
        mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
        mavutil.mavlink.MAV_FRAME_LOCAL_NED,
    ]

    # Base lat/lon near your current hardcoded location
    base_lat = items[0]["x"]
    base_lon = items[0]["y"]

    # Apply per-item mutations from chunks of buf
    # Each item consumes 20 bytes if available.
    for i, it in enumerate(items):
        off = 1 + i * 20
        chunk = buf[off:off+20]

        # choose command/frame
        if len(chunk) >= 2:
            sel = chunk[0]
            it["command"] = (NAV_CMDS + DO_CMDS)[sel % (len(NAV_CMDS) + len(DO_CMDS))]
            it["frame"] = FRAMES[chunk[1] % len(FRAMES)]

        # fuzz params using floats, including NaN/Inf occasionally
        p1 = _f32(_pick_from(chunk, 2, 4, 0))
        p2 = _f32(_pick_from(chunk, 6, 4, 0))
        p3 = _f32(_pick_from(chunk, 10, 4, 0))
        p4 = _f32(_pick_from(chunk, 14, 4, 0))

        # Occasionally inject NaN / Inf patterns from bytes
        if len(chunk) >= 20 and (chunk[18] & 0x1):
            p4 = float("nan")
        if len(chunk) >= 20 and (chunk[19] & 0x1):
            p3 = float("inf") if (chunk[19] & 0x2) else float("-inf")

        it["param1"], it["param2"], it["param3"], it["param4"] = p1, p2, p3, p4

        # lat/lon deltas (bounded but still explores)
        dlat = _s16(_pick_from(chunk, 0, 2, 0))  # reuse bytes
        dlon = _s16(_pick_from(chunk, 2, 2, 0))
        it["x"] = int(base_lat + dlat * 50)      # 50 = ~5e-6 deg steps
        it["y"] = int(base_lon + dlon * 50)

        # altitude fuzz: -50..+200 meters-ish plus extreme sometimes
        if len(chunk) >= 8:
            z = _f32(_pick_from(chunk, 4, 4, 0))
            if abs(z) < 1e-3:
                z = float((chunk[4] % 250) - 50)  # -50..199
            it["z"] = z

        it["seq"] = i
        it["autocontinue"] = 1

    # Make last item more likely to be LAND/RTL
    items[-1]["command"] = [mavutil.mavlink.MAV_CMD_NAV_LAND,
                            mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH][buf[1] % 2 if len(buf) > 1 else 0]
    return items


def _json_safe_value(value):
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
    return value


def _serialize_mission_items(items):
    serialized = []
    for item in items:
        serialized.append({key: _json_safe_value(value) for key, value in item.items()})
    return serialized


def _periodic_snapshot_path(out_dir):
    return os.path.join(out_dir, ".last_periodic_snapshot")


def should_persist_case(why, out_dir):
    crash_reasons = {"exception", "px4_died", "child_signal"}
    if why in crash_reasons:
        return True, True

    if REPORT_SNAPSHOT_INTERVAL_S <= 0:
        return False, False

    marker_path = _periodic_snapshot_path(out_dir)
    now = time.time()
    last_snapshot = 0.0
    try:
        last_snapshot = float(Path(marker_path).read_text(encoding="utf-8").strip() or "0")
    except Exception:
        last_snapshot = 0.0

    if now - last_snapshot >= REPORT_SNAPSHOT_INTERVAL_S:
        try:
            Path(marker_path).write_text(str(now), encoding="utf-8")
        except Exception:
            pass
        return True, True

    return False, False


def save_last_case(buf, why="case", out_dir="/tmp/mission_fuzz_cases", mission_items=None, meta=None):
    os.makedirs(out_dir, exist_ok=True)
    should_save_raw, should_save_details = should_persist_case(why, out_dir)
    if not should_save_raw and not should_save_details:
        return None

    h = hashlib.sha1(buf).hexdigest()
    p = os.path.join(out_dir, f"{why}_{h}.bin")
    if should_save_raw:
        try:
            with open(p, "wb") as f:
                f.write(buf)
        except Exception:
            pass
    if should_save_details and (mission_items is not None or meta is not None):
        details_path = os.path.join(out_dir, f"{why}_{h}.json")
        payload = {
            "why": why,
            "sha1": h,
            "input_len": len(buf),
            "meta": meta or {},
            "mission_items": _serialize_mission_items(mission_items or []),
        }
        try:
            with open(details_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception:
            pass
    return p


def px4_alive_or_die(current_input: bytes, mission_items=None, meta=None):
    """
    If PX4 dies, force this python process to exit nonzero so the parent can record it.
    """
    pids = read_px4_pids()
    if not pids:
        return
    alive = all(is_pid_alive_not_zombie(p) for p in pids)
    if not alive:
        save_last_case(current_input, why="px4_died", mission_items=mission_items, meta=meta)
        os._exit(1)

def wait_heartbeat(m):
    hb = m.wait_heartbeat(timeout=10)
    if not hb:
        raise RuntimeError("No heartbeat from PX4 (is SITL running?)")
    print(f"Heartbeat: sysid={m.target_system} compid={m.target_component}")

def set_mavlink2(m, enable=True):
    # Pymavlink will speak MAVLink2 if the peer supports it.
    # You can force v1 if needed via m.force_mavlink1 = True
    m.force_mavlink1 = not enable

def clear_mission(m, mission_type=mavutil.mavlink.MAV_MISSION_TYPE_MISSION):
    try:
        with _tx_lock:
            m.mav.mission_clear_all_send(
                m.target_system,
                m.target_component,
                mission_type
            )
            # PX4 will usually ACK; we wait a bit for robustness
            msg = m.recv_match(type="MISSION_ACK", blocking=True, timeout=5)
    except Exception:
        pass
    if msg:
        pass
        #print("CLEAR_ALL ACK:", msg.type)
    else:
        print("No MISSION_ACK to CLEAR_ALL (continuing)")

def build_mission_items():
    # A simple global mission near "home".
    # In SITL you can use any plausible lat/lon; PX4 will accept and simulate.
    # Use *_INT messages with lat/lon in 1e7.
    lat = int(47.3977420 * 1e7)
    lon = int(8.5455940 * 1e7)
    alt = 20.0

    items = []

    # 0) TAKEOFF
    items.append(dict(
        seq=0,
        frame=mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
        command=mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
        current=1,
        autocontinue=1,
        param1=0, param2=0, param3=0, param4=float("nan"),
        x=lat, y=lon, z=alt
    ))

    # 1) WAYPOINT
    items.append(dict(
        seq=1,
        frame=mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
        command=mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
        current=0,
        autocontinue=1,
        param1=0, param2=2, param3=0, param4=float("nan"),
        x=lat + int(0.0000100 * 1e7),  # ~1e-5 deg
        y=lon,
        z=alt
    ))

    # 2) WAYPOINT
    items.append(dict(
        seq=2,
        frame=mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
        command=mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
        current=0,
        autocontinue=1,
        param1=0, param2=2, param3=0, param4=float("nan"),
        x=lat + int(0.0000100 * 1e7),
        y=lon + int(0.0000100 * 1e7),
        z=alt
    ))

    # 3) LAND
    items.append(dict(
        seq=3,
        frame=mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
        command=mavutil.mavlink.MAV_CMD_NAV_LAND,
        current=0,
        autocontinue=1,
        param1=0, param2=0, param3=0, param4=float("nan"),
        x=lat,
        y=lon,
        z=0.0
    ))

    return items

def upload_mission(m, items, mission_type=mavutil.mavlink.MAV_MISSION_TYPE_MISSION):
    count = len(items)

    # Tell PX4 how many items
    try:
        with _tx_lock:

            m.mav.mission_count_send(
                m.target_system,
                m.target_component,
                count,
                mission_type
            )
    except Exception:
        pass

    sent = 0
    start = time.time()

    while True:
        try:
            with _tx_lock:
                msg = m.recv_match(blocking=True, timeout=10)
        except Exception:
            pass

        if not msg:
            raise RuntimeError("Timed out waiting for mission protocol message")

        t = msg.get_type()

        if t in ("MISSION_REQUEST_INT", "MISSION_REQUEST"):
            seq = int(msg.seq)
            if seq < 0 or seq >= count:
                raise RuntimeError(f"PX4 requested out-of-range seq={seq} count={count}")

            it = items[seq]
            # Use MISSION_ITEM_INT to avoid float rounding
            try:
                with _tx_lock:
                    m.mav.mission_item_int_send(
                        m.target_system,
                        m.target_component,
                        it["seq"],
                        it["frame"],
                        it["command"],
                        it["current"],
                        it["autocontinue"],
                        it["param1"], it["param2"], it["param3"], it["param4"],
                        it["x"], it["y"], it["z"],
                        mission_type
                    )
            except Exception:
                pass
            sent += 1

        elif t == "MISSION_ACK":
            # msg.type is MAV_MISSION_RESULT
            print("MISSION_ACK:", msg.type, "sent_items:", sent, "elapsed:", round(time.time()-start, 2), "s")
            return msg.type


# -------------------------------------------------------------------------

# simple logging helper
_log_fh = open(LOGFILE, "a", buffering=1) if LOGFILE else None
def log(msg):
    if _log_fh:
        _log_fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def wait_for_px4_settle(reason: str):
    log(f"[wait_for_px4_settle] waiting {PX4_STARTUP_WAIT_S:.0f}s for PX4 startup ({reason})")
    time.sleep(PX4_STARTUP_WAIT_S)

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
        return True

    alive = [is_pid_alive_not_zombie(p) for p in pids]
    if all(alive):
        log(f"[ensure_px4_running] PX4 appears already running (pids={pids})")
        return False

    log(f"[ensure_px4_running] some PX4 pids dead/zombie ({pids}); restarting")
    start_px4()
    return True


def start_px4():
    px4_dir = PX4_DIR
    num_procs = os.getenv("NUM_PROCS", str(os.cpu_count() or 4))

    '''make_cmd = (
        f'export LD_PRELOAD="$PWD/scripts/libgcov_flush.so${LD_PRELOAD:+:$LD_PRELOAD}"'
        f'PX4_CMAKE_BUILD_TYPE=Coverage '
        f'CC=clang CXX=clang++ '
        f'CMAKE_ARGS="-DCMAKE_C_FLAGS=-fsanitize=address '
        f'-DCMAKE_CXX_FLAGS=-fsanitize=address '
        f'-DCMAKE_EXE_LINKER_FLAGS=-fsanitize=address" '
        f'make px4_sitl gz_x500 -j{num_procs}'
    )'''


    '''make_cmd = (
        f'PX4_CMAKE_BUILD_TYPE=Coverage '
        f'CC=clang CXX=clang++ '
        f'CMAKE_ARGS="-DCMAKE_C_FLAGS=-fsanitize=address '
        f'-DCMAKE_CXX_FLAGS=-fsanitize=address '
        f'-DCMAKE_EXE_LINKER_FLAGS=-fsanitize=address" '
        f'make px4_sitl gz_x500 -j{num_procs}'
    )'''

    make_cmd = f"CC=clang CXX=clang++ PX4_ASAN=1 make px4_sitl gz_x500 -j{num_procs}"
    
    ld_preload_cmd = ""

    print(make_cmd)

    kill_existing_px4()
    kill_gazebo()

    # Acquire lock in python (simpler than flock -c nested quoting)
    lock_fd = os.open(RESTART_LOCK, os.O_CREAT | os.O_RDWR, 0o666)
    try:
        import fcntl
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("[start_px4] restart lock held; skipping start")
        os.close(lock_fd)
        return
    except Exception as e:
        log(f"[start_px4] flock error: {e}")
        os.close(lock_fd)
        raise

    try:
        # Send gnome-terminal stderr/stdout to a real log file so you can see failures
        term_log = open("/tmp/px4_gnome_terminal.log", "ab", buffering=0)

        # ONE bash -lc layer only; no extra quoting games
        #bash_line = (
        #    'export LD_PRELOAD=$PWD/../scripts/libgcov_flush.so:${LD_PRELOAD}; '
        #    f'cd "{px4_dir}" && {make_cmd}; exec bash'
        #)
        bash_line = (
        f'cd "{px4_dir}" && '
        #f'LD_PRELOAD="$PWD/../scripts/libgcov_flush.so${{LD_PRELOAD:+:$LD_PRELOAD}}" '
        f'{make_cmd}; '
        f'exec bash'
)
        print("BASH_LINE:", bash_line, flush=True)

        cmd = [
            "gnome-terminal",
            "--working-directory", px4_dir,
            "--",
            "bash", "-lc", bash_line,
        ]

        log(f"[start_px4] launching: {cmd}")

        subprocess.Popen(cmd, stdout=term_log, stderr=term_log)

    finally:
        # release lock
        try:
            import fcntl
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except Exception:
            pass
        os.close(lock_fd)

    time.sleep(5.0)
    snap_px4_pids()
    wait_for_px4_settle("PX4 launch")
    log("[start_px4] start complete")

def start_px42():
    """
    Launch PX4 SITL in a new GNOME Terminal (interactive output).
    - Kills any existing px4 binary and Gazebo first (clean reset).
    - Serialized by RESTART_LOCK to avoid multiple terminals.
    - Snapshots only the real px4 PID(s).
    - Waits ~15s for full boot/settle.
    """
    px4_dir = PX4_DIR
    num_procs = os.getenv("NUM_PROCS", str(os.cpu_count() or 4))


    make_cmd = (
        f'PX4_CMAKE_BUILD_TYPE=Coverage '
        f'CC=clang CXX=clang++ '
        f'CMAKE_ARGS="-DCMAKE_C_FLAGS=-fsanitize=address '
        f'-DCMAKE_CXX_FLAGS=-fsanitize=address '
        f'-DCMAKE_EXE_LINKER_FLAGS=-fsanitize=address" '
        f'make px4_sitl gz_x500 -j{num_procs}'
    )
    #make_cmd = f'PX4_CMAKE_BUILD_TYPE=Coverage CC=clang CXX=clang++ CMAKE_ARGS="-DCMAKE_C_FLAGS=-fsanitize=address -DCMAKE_CXX_FLAGS=-fsanitize=address -DCMAKE_EXE_LINKER_FLAGS=-fsanitize=address" make px4_sitl gz_x500 -j{num_procs}'
    
    #make_cmd1 = f"CC=clang CXX=clang++ PX4_ASAN=1 make px4_sitl gz_x500 -j{num_procs}"
    print(make_cmd)
    #print(make_cmd1)
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

    wait_for_px4_settle("PX4 launch")
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
parent_tx = None
_parent_tx_lock = threading.Lock()

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


def connect_parent_tx(timeout=20):
    """Create the parent's long-lived MAVLink connection for continuous GCS heartbeats."""
    global parent_tx
    with _parent_tx_lock:
        try:
            conn_str = f"udp:{MAVLINK_HOST}:{MAVLINK_PORT}"
            parent_tx = mavutil.mavlink_connection(conn_str, source_system=GCS_SYSTEM_ID)
            t0 = time.time()
            while time.time() - t0 < timeout:
                if parent_tx.recv_match(type='HEARTBEAT', blocking=True, timeout=1):
                    log("[connect_parent_tx] connected parent heartbeat channel")
                    return True
            raise TimeoutError("No HEARTBEAT from PX4 on parent heartbeat channel")
        except Exception as e:
            log(f"[connect_parent_tx] failed: {e}")
            parent_tx = None
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


def parent_gcs_heartbeat_thread(stop_evt):
    while not stop_evt.is_set():
        try:
            with _parent_tx_lock:
                if parent_tx:
                    parent_tx.mav.heartbeat_send(
                        mavutil.mavlink.MAV_TYPE_GCS,
                        mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                        0, 0, mavutil.mavlink.MAV_STATE_ACTIVE
                    )
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
    global vehicle_ready, parent_tx
    log("[restart] px4 failure detected; restarting")
    try:
        with _parent_tx_lock:
            try:
                if parent_tx:
                    parent_tx.close()
            except Exception:
                pass
            parent_tx = None
            parent_tx = None
        kill_existing_px4()
        kill_gazebo()
        start_px4()
        connect_parent_tx()
        vehicle_ready = True
    except Exception as e:
        log(f"[restart] error: {e}")
        vehicle_ready = False

# ---------------------- Python forkserver harness loop ---------------------
def make_case_generator():
    rng = random.Random()
    if HARNESS_SEED is not None:
        rng.seed(int(HARNESS_SEED))
        log(f"[make_case_generator] using deterministic seed {HARNESS_SEED}")
    else:
        seed_bytes = os.urandom(16)
        rng.seed(int.from_bytes(seed_bytes, "little"))
        log("[make_case_generator] using os.urandom seed")
    return rng


def generate_random_case(rng):
    if MAX_INPUT <= 0:
        return b"\x00"
    max_len = max(MIN_INPUT, MAX_INPUT)
    min_len = max(1, min(MIN_INPUT, max_len))
    size = rng.randint(min_len, max_len)
    return bytes(rng.getrandbits(8) for _ in range(size))


def run_single_mission_iteration(buf, base_items, iteration):
    global tx

    if not connect_tx():
        raise RuntimeError("Unable to connect to PX4 MAVLink endpoint")

    try:
        log("[run_single_mission_iteration] parent heartbeat active; waiting 2s before mission upload")
        t0 = time.time()
        while time.time() - t0 < 2.0:
            try:
                if tx:
                    tx.recv_match(blocking=False)
            except Exception:
                pass
            time.sleep(0.05)

        px4_alive_or_die(buf, meta={"iteration": iteration})

        if (buf[0] & 0x7) == 0:
            clear_mission(tx)

        items = mutate_mission_from_bytes(buf, base_items, want_len=10, iteration=iteration)
        min_len, max_len = mission_length_window(iteration)
        case_meta = {
            "iteration": iteration,
            "mission_len": len(items),
            "mission_min_len": min_len,
            "mission_max_len": max_len,
        }
        res = upload_mission_safe(tx, items)
        save_last_case(buf, why=f"mission_ack_{res}", mission_items=items, meta=case_meta)

        time.sleep(POST_SEND_MS / 1000.0)
        px4_alive_or_die(buf, mission_items=items, meta=case_meta)
        return 0
    except Exception as e:
        log(f"[run_single_mission_iteration] exception: {e}")
        items = mutate_mission_from_bytes(buf, base_items, want_len=10, iteration=iteration)
        min_len, max_len = mission_length_window(iteration)
        save_last_case(
            buf,
            why="exception",
            mission_items=items,
            meta={
                "iteration": iteration,
                "mission_len": len(items),
                "mission_min_len": min_len,
                "mission_max_len": max_len,
                "exception": str(e),
            },
        )
        return 2
    finally:
        with _tx_lock:
            try:
                if tx:
                    tx.close()
            except Exception:
                pass
            tx = None


def missionLoop():
    global parent_tx
    rng = make_case_generator()
    base_items = build_mission_items()
    hb_stop = threading.Event()
    hb_t = None

    log(f"[missionLoop] starting Python fork loop for {FORKSERVER_ITERATIONS} iterations")
    wait_for_px4_settle("forkserver bootstrap")
    if not connect_parent_tx():
        raise RuntimeError("Unable to establish parent heartbeat channel to PX4")

    hb_t = threading.Thread(target=parent_gcs_heartbeat_thread, args=(hb_stop,), daemon=True)
    hb_t.start()

    try:
        for iteration in range(FORKSERVER_ITERATIONS):
            buf = generate_random_case(rng)
            pid = os.fork()

            if pid == 0:
                exit_code = run_single_mission_iteration(buf or b"\x00", base_items, iteration)
                os._exit(exit_code)

            child_pid, status = os.waitpid(pid, 0)
            log(f"[missionLoop] child {child_pid} finished with status {status}")

            if os.WIFSIGNALED(status):
                log(f"[missionLoop] child killed by signal {os.WTERMSIG(status)}; restarting PX4")
                items = mutate_mission_from_bytes(buf or b"\x00", base_items, want_len=10, iteration=iteration)
                min_len, max_len = mission_length_window(iteration)
                save_last_case(
                    buf,
                    why="child_signal",
                    mission_items=items,
                    meta={
                        "iteration": iteration,
                        "signal": os.WTERMSIG(status),
                        "mission_len": len(items),
                        "mission_min_len": min_len,
                        "mission_max_len": max_len,
                    },
                )
                handle_px4_failure_and_restart()
                continue

            exit_code = os.WEXITSTATUS(status)
            if exit_code == 0:
                continue

            log(f"[missionLoop] iteration {iteration} returned exit code {exit_code}; restarting PX4")
            handle_px4_failure_and_restart()
    finally:
        hb_stop.set()
        if hb_t:
            hb_t.join(timeout=1.0)
        with _parent_tx_lock:
            try:
                if parent_tx:
                    parent_tx.close()
            except Exception:
                pass

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
        missionLoop()
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
