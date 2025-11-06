#!/usr/bin/env python3
# file: px4_lifeline_offboard_gcs.py
import os, time, threading, sys
from pymavlink import mavutil

MAVLINK_HOST = os.getenv("MAVLINK_HOST", "127.0.0.1")
MAVLINK_PORT = int(os.getenv("MAVLINK_PORT", "14540"))

TARGET_X = float(os.getenv("TARGET_X", "0"))
TARGET_Y = float(os.getenv("TARGET_Y", "0"))
TARGET_Z = float(os.getenv("TARGET_Z", "-5.0"))   # LOCAL_NED: negative Z is up
CONFIRM_ALT_M = float(os.getenv("CONFIRM_ALT_M", "2.0"))

def connect(timeout=20):
    m = mavutil.mavlink_connection(f"udp:{MAVLINK_HOST}:{MAVLINK_PORT}", source_system=246)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if m.recv_match(type='HEARTBEAT', blocking=True, timeout=1):
            return m
    raise TimeoutError("No HEARTBEAT from PX4")

def gcs_heartbeats(m, stop_evt):
    while not stop_evt.is_set():
        try:
            m.mav.heartbeat_send(
                mavutil.mavlink.MAV_TYPE_GCS,              # <<< this is the key
                mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                0, 0, mavutil.mavlink.MAV_STATE_ACTIVE
            )
        except Exception:
            pass
        stop_evt.wait(0.5)  # 2 Hz

def send_pos_sp(m, stop_evt):
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
            m.mav.set_position_target_local_ned_send(
                int(time.time()*1000) & 0xFFFFFFFF,
                m.target_system or 1, m.target_component or 1,
                mavutil.mavlink.MAV_FRAME_LOCAL_NED,
                mask, TARGET_X, TARGET_Y, TARGET_Z,
                0,0,0, 0,0,0, 0, 0
            )
        except Exception:
            pass
        stop_evt.wait(0.05)  # 20 Hz

def set_mode_offboard(m):
    PX4_MAIN_OFFBOARD = 6
    m.mav.command_long_send(
        m.target_system or 0, m.target_component or 0,
        mavutil.mavlink.MAV_CMD_DO_SET_MODE, 0,
        1, PX4_MAIN_OFFBOARD, 0, 0,0,0,0
    )
    m.recv_match(type='COMMAND_ACK', blocking=False)

def arm(m, wait_s=8):
    m.mav.command_long_send(
        m.target_system or 0, m.target_component or 0,
        mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0, 1, 0,0,0,0,0,0
    )
    t0 = time.time()
    while time.time() - t0 < wait_s:
        hb = m.recv_match(type='HEARTBEAT', blocking=True, timeout=1)
        if hb and (hb.base_mode or 0) & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED:
            return True
    return False

def wait_alt(m, min_alt=2.0, timeout_s=30):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        msg = m.recv_match(type='GLOBAL_POSITION_INT', blocking=True, timeout=1)
        if msg and getattr(msg, "relative_alt", None) is not None:
            if msg.relative_alt/1000.0 >= min_alt:
                return True
    return False

def main():
    m = connect()
    hb_stop = threading.Event()
    sp_stop = threading.Event()
    hb = threading.Thread(target=gcs_heartbeats, args=(m, hb_stop), daemon=True)
    sp = threading.Thread(target=send_pos_sp, args=(m, sp_stop), daemon=True)
    hb.start(); sp.start()

    # let heartbeats run a couple seconds so preflight sees a GCS
    t0 = time.time()
    while time.time() - t0 < 2.0:
        m.recv_match(blocking=False); time.sleep(0.05)

    set_mode_offboard(m)
    if not arm(m):
        print("[lifeline] arm timeout; still streaming setpoints & heartbeats", file=sys.stderr)
    ok = wait_alt(m, CONFIRM_ALT_M, 30)
    print(f"[lifeline] offboard hover: {'OK' if ok else 'alt not confirmed'}; holding setpoints …")

    try:
        while True:
            m.recv_match(blocking=False)
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        sp_stop.set(); hb_stop.set()
        sp.join(timeout=1.0); hb.join(timeout=1.0)
        try: m.close()
        except Exception: pass

if __name__ == "__main__":
    main()

