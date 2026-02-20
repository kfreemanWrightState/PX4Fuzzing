#!/usr/bin/env python3
from pymavlink import mavutil
import time

# PX4 SITL default: MAVLink on udp:14540 (from PX4 -> GCS)
# We'll connect there, and send back to PX4 on the same link.
CONN = "udp:127.0.0.1:14540"

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
    m.mav.mission_clear_all_send(
        m.target_system,
        m.target_component,
        mission_type
    )
    # PX4 will usually ACK; we wait a bit for robustness
    msg = m.recv_match(type="MISSION_ACK", blocking=True, timeout=5)
    if msg:
        print("CLEAR_ALL ACK:", msg.type)
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
    m.mav.mission_count_send(
        m.target_system,
        m.target_component,
        count,
        mission_type
    )

    sent = 0
    start = time.time()

    while True:
        msg = m.recv_match(blocking=True, timeout=10)
        if not msg:
            raise RuntimeError("Timed out waiting for mission protocol message")

        t = msg.get_type()

        if t in ("MISSION_REQUEST_INT", "MISSION_REQUEST"):
            seq = int(msg.seq)
            if seq < 0 or seq >= count:
                raise RuntimeError(f"PX4 requested out-of-range seq={seq} count={count}")

            it = items[seq]
            # Use MISSION_ITEM_INT to avoid float rounding
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

        elif t == "MISSION_ACK":
            # msg.type is MAV_MISSION_RESULT
            print("MISSION_ACK:", msg.type, "sent_items:", sent, "elapsed:", round(time.time()-start, 2), "s")
            return msg.type

        # Ignore other telemetry

def main():
    m = mavutil.mavlink_connection(CONN, autoreconnect=True)
    wait_heartbeat(m)

    # Prefer MAVLink2 (PX4 supports it). Set False to force MAVLink1.
    set_mavlink2(m, enable=True)

    clear_mission(m)
    items = build_mission_items()
    res = upload_mission(m, items)
    print("Upload result code:", res)

if __name__ == "__main__":
    main()
