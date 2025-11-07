#!/usr/bin/env python3
"""
AFL harness — Option 2: send an unsupported COMMAND_LONG (command id 5000)
to PX4 to elicit an error/COMMAND_ACK, then send AFL fuzz bytes to the
PX4 MAVLink UDP port.

Usage (example):
  AFL_SKIP_BIN_CHECK=1 AFL_DUMB_FORKSRV=1 \
  py-afl-fuzz -i seeds -o findings -- python3 harness_option2.py @@

Env vars:
  MAVLINK_HOST   (default 127.0.0.1)  - PX4 offboard (remote) host
  MAVLINK_PORT   (default 14540)      - PX4 offboard (remote) port (where PX4 listens)
  SINK_HOST      (default 127.0.0.1)  - fallback raw UDP sink for fuzz bytes
  SINK_PORT      (default 14540)      - fallback raw UDP sink port (usually same as MAVLINK_PORT)
  HEARTBEAT_HZ   (default 2)          - heartbeat frequency (Hz)
  TIMEOUT_CONNECT_MS (default 1500)   - socket connect timeout (ms)
"""

import os
import sys
import socket
import select
import threading
import time
import json
import signal
import subprocess
import afl  # python-afl; must be available
from pymavlink import mavutil

# --- start AFC forkserver ASAP ---
afl.init()

# --- configuration ---
MAVLINK_HOST = os.getenv("MAVLINK_HOST", "127.0.0.1")
MAVLINK_PORT = int(os.getenv("MAVLINK_PORT", "14540"))  # PX4 offboard port (PX4 remote -> our local addr)
SINK_HOST = os.getenv("SINK_HOST", MAVLINK_HOST)
SINK_PORT = int(os.getenv("SINK_PORT", MAVLINK_PORT))
LOCKFILE  = os.getenv("PX4_LOCK", "/tmp/px4.restart.lock")


SHELL_TARGET = os.getenv("PX4_SHELL_ENDPOINT", "udpout:127.0.0.1:14540")
STARTER   = os.getenv("PX4_STARTER", "./startPX4.sh") 
PID_FILE  = os.getenv("PID_FILE", "/tmp/px4_pids.json")

POST_SEND_SLEEP = float(os.getenv("POST_SEND_SLEEP", "0.03"))  # small dwell


HEARTBEAT_HZ = float(os.getenv("HEARTBEAT_HZ", "2"))
TIMEOUT_CONNECT_MS = int(os.getenv("TIMEOUT_CONNECT_MS", "1500"))

# unsupported command id to provoke an error/unsupported ack on PX4
UNSUPPORTED_COMMAND_ID = int(os.getenv("UNSUPPORTED_COMMAND_ID", "5000"))

RECV_TIMEOUT_MS = int(os.getenv("RECV_TIMEOUT_MS", "80"))

# Internal globals
_mav = None
_mav_lock = threading.Lock()
_hb_stop = threading.Event()


# ----------------- MAVLink helpers -----------------
def try_connect_mavlink():
    """
    Try to open a pymavlink UDP endpoint to PX4.
    Return the pymavlink connection or None on failure.
    Quick timeout to avoid slowing AFL init.
    """
    try:
        # source_system chosen high to avoid conflicts
        conn_str = f"udp:{MAVLINK_HOST}:{MAVLINK_PORT}"
        mav = mavutil.mavlink_connection(conn_str, source_system=250)
        # wait briefly for heartbeat so PX4 sees our addr
        t0 = time.time()
        while time.time() - t0 < (TIMEOUT_CONNECT_MS / 1000.0):
            hb = mav.recv_match(type='HEARTBEAT', blocking=True, timeout=0.5)
            if hb:
                return mav
        # no heartbeat seen — still return mav (it lets us send), but caller may choose None
        return None
    except Exception:
        return None


def hb_thread_fn(mav, stop_evt):
    """Send HEARTBEAT periodically while stop_evt not set."""
    interval = 1.0 / max(0.1, HEARTBEAT_HZ)
    while not stop_evt.is_set():
        try:
            mav.mav.heartbeat_send(
                mavutil.mavlink.MAV_TYPE_ONBOARD_CONTROLLER,
                mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                0, 0, mavutil.mavlink.MAV_STATE_ACTIVE
            )
        except Exception:
            # ignore and continue — connection might go away transiently
            pass
        stop_evt.wait(interval)


def send_unsupported_command_via_mav(mav):
    """
    Send an unsupported COMMAND_LONG via pymavlink.
    PX4 should reply with COMMAND_ACK: RESULT=UNSUPPORTED (and print a log).
    """
    try:
        # params are all zero except param1..7 unused here
        mav.mav.command_long_send(
            mav.target_system or 0,
            mav.target_component or 0,
            UNSUPPORTED_COMMAND_ID,
            0,  # confirmation
            0,0,0,0,0,0,0
        )
        # try to read any immediate ack (non-blocking)
        _ = mav.recv_match(type='COMMAND_ACK', blocking=False, timeout=0.05)
    except Exception:
        pass


def send_unsupported_command_raw_udp():
    """
    Fallback: craft a minimal (likely invalid) UDP payload to the PX4 port.
    This is less structured but still causes PX4 to log an unsupported command
    when it resolves to a mis-parse / unsupported command.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setblocking(False)
        # send a tiny blob containing the command id as plain bytes (not proper MAVLink)
        # PX4's UDP layer will get this and likely log a parse error — still an "error" event.
        payload = b'CMD' + bytes(str(UNSUPPORTED_COMMAND_ID), 'ascii')
        s.sendto(payload, (SINK_HOST, SINK_PORT))
        s.close()
    except Exception:
        pass


# ----------------- fuzz sink -----------------
def send_once(data: bytes):
    """Send raw fuzz bytes over UDP to the SINK (PX4 offboard port by default)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    try:
        s.sendto(data, (SINK_HOST, SINK_PORT))
        # short non-blocking receive feedback to give AFL a timing/behavior signal
        r, _, _ = select.select([s], [], [], RECV_TIMEOUT_MS / 1000.0)
        if r:
            try:
                s.recvfrom(4096)
            except BlockingIOError:
                pass
    finally:
        try:
            s.close()
        except Exception:
            pass
# ----------------- pid helpers -----------------


def restart_px4():
    # Use flock to avoid parallel restarts when AFL respawns harness quickly
    cmd = f"flock -n {LOCKFILE} bash -lc '{STARTER}'"
    subprocess.run(cmd, shell=True, check=False)
    time.sleep(10)

def _read_px4_pid(path=PID_FILE):
    with open(path, "r", encoding="utf-8") as f:
        return int(json.load(f)["px4_pid"])


def _read_px4_pids(path=PID_FILE):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [int(p) for p in data.get("px4_pids", [])]

def _alive_and_not_zombie(pid: int) -> bool:
    """
    Return True if process exists and is not in Zombie state.
    Linux: /proc/<pid>/stat 3rd field is state char (R,S,D,T,Z,...)
    """
    try:
        with open(f"/proc/{pid}/stat", "r") as st:
            # format: pid (comm) state ...
            # state is the third token, but (comm) may contain spaces.
            txt = st.read()
        # Extract tokens safely: pid (comm) state — split on ') ' then space
        after_comm = txt.split(") ", 1)[1]
        state = after_comm.split()[0]  # single letter
        return state != "Z"
    except FileNotFoundError:
        # /proc/<pid> missing => dead
        return False
    except Exception:
        # If anything odd happens, be conservative: assume alive
        return True

def reap_zombie_parent():
    import subprocess
    # Kill any gnome-terminal or bash parents running the old cmake-env command
    subprocess.run(
        "pkill -f 'gnome-terminal.*PX4_SIM_MODEL=gz_x500'",
        shell=True, check=False)
    subprocess.run(
        "pkill -f '/usr/bin/cmake -E env PX4_SIM_MODEL=gz_x500'",
        shell=True, check=False)

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)     # does not kill; checks existence
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

# ----------------- main harness -----------------
def main():
    global _mav
    
    # Read the two tracked PIDs
    try:
        px4_pids = _read_px4_pids()
    except Exception as e:
        sys.stderr.write(f"[harness] PID file error: {e}\n")
        px4_pids = []

    before = [ _alive_and_not_zombie(p) for p in px4_pids ]


    # Try to connect to MAVLink quickly (non-blocking-ish). If it fails, proceed anyway.
    _mav = try_connect_mavlink()

    # If we have do not a pymavlink connection
    if _mav:
        print("MAV Connected" )
    else:
        # no mavlink connection — we still proceed but we warn
        sys.stderr.write("[harness] Warning: could not connect to MAVLink; proceeding with raw UDP sends.\n")

    try:
        # Read AFL input (file path provided as argv[1] or stdin "-")
        if len(sys.argv) >= 2 and sys.argv[1] != "-":
            inpath = sys.argv[1]
            try:
                with open(inpath, "rb") as f:
                    data = f.read(8192)
            except Exception:
                data = b""
        else:
            data = sys.stdin.buffer.read(8192)

        # 1) Send unsupported command to provoke immediate error/COMMAND_ACK from PX4
        if _mav:
            send_unsupported_command_via_mav(_mav)
        else:
            send_unsupported_command_raw_udp()

        # short pause to give PX4 a moment to log the event (keeps AFL iteration meaningful)
        time.sleep(0.01)

        # 2) Send the fuzz bytes to the sink (PX4 will typically parse these on its MAVLink port)
        if data:
            send_once(data)

    except Exception as e:
        # never crash the harness itself — AFL forkserver expects clean exits
        sys.stderr.write(f"[harness] Exception: {e}\n")
        try:
            _hb_stop.set()
        except Exception:
            pass
        sys.exit(0)
    finally:
        # cleanup heartbeat thread
        try:
            _hb_stop.set()
            if hb_thread:
                hb_thread.join(timeout=0.5)
        except Exception:
            pass


    time.sleep(POST_SEND_SLEEP)  # tiny dwell so a crash can manifest
    #print("Cause Crash start\n")
    #time.sleep(10)
    #print("Cause Crash end\n")
    after = [ _alive_and_not_zombie(p) for p in px4_pids ]

    # If any went from alive->dead OR alive->zombie, signal crash to AFL
    for b, a, p in zip(before, after, px4_pids):
        if b and not a:
            sys.stderr.write(f"[harness] PX4 PID {p} died or became zombie; signaling crash.\n")
            restart_px4()
            os.kill(os.getpid(), signal.SIGSEGV)


if __name__ == "__main__":
    main()

