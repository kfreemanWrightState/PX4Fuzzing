#!/usr/bin/env python3
"""
make_seeds.py

Generate initial AFL++ seeds for the PX4 fuzz harness that interprets stdin
as "field bytes" and then builds MAVLink v2 messages via pymavlink.

Seeds created (in ./seeds):

  - heartbeat.bin
      selector -> HEARTBEAT
      base_mode/custom_mode/system_status set to sane values

  - mission_item_int_square_start.bin
      selector -> MISSION_ITEM_INT
      NAV_WAYPOINT with reasonable params and lat/lon/alt

  - command_long_arm.bin
      selector -> COMMAND_LONG (ARM/DISARM with param1 = 1.0)

  - command_long_takeoff.bin
      selector -> COMMAND_LONG (NAV_TAKEOFF-like parameters)

  - command_long_change_speed.bin
      selector -> COMMAND_LONG (DO_CHANGE_SPEED-like parameters)
"""

import os
import struct
from pathlib import Path

SEEDS_DIR = Path("seeds")


def put_f32(buf: bytearray, offset: int, value: float) -> None:
    """Write a little-endian float32 into buf at offset."""
    packed = struct.pack("<f", value)
    end = offset + 4
    if end > len(buf):
        # grow buffer if needed
        buf.extend(b"\x00" * (end - len(buf)))
    buf[offset:end] = packed


def write_seed(name: str, data: bytearray) -> None:
    path = SEEDS_DIR / name
    path.write_bytes(bytes(data))
    print(f"[+] wrote {path}")


def make_heartbeat_seed():
    """
    Seed for msg_type == 0 (HEARTBEAT).

    Mapping in harness:
      selector = data[0]           -> 0 % 3 == 0  => HEARTBEAT
      base_mode   = data[1]
      custom_mode = u32 at data[2]
      system_status = data[6]
    """
    buf = bytearray(16)

    # selector -> HEARTBEAT
    buf[0] = 0  # 0 % 3 == 0

    # base_mode: set some flags (e.g., custom, guided, armed bits etc.)
    buf[1] = 0b00001111  # arbitrary non-zero base_mode

    # custom_mode: just 0 for now
    buf[2:6] = (0).to_bytes(4, "little", signed=False)

    # system_status: MAV_STATE_ACTIVE ~ 4
    buf[6] = 4

    return buf


def make_mission_item_int_seed():
    """
    Seed for msg_type == 1 (MISSION_ITEM_INT).

    Mapping in harness:
      selector = data[0] -> 1 % 3 == 1 => MISSION_ITEM_INT
      seq        = u16 at data[1]
      frame_idx  = data[3] % len(frames)
      cmd_idx    = data[4] % len(commands)
      current    = data[5] & 1
      autocont   = data[6] & 1
      param1     = f32 at data[7]
      param2     = f32 at data[11]
      param3     = f32 at data[15]
      param4     = f32 at data[19]
      lat_e7     = u32 at data[23]
      lon_e7     = u32 at data[27]
      alt        = f32 at data[31]
    """
    buf = bytearray(64)

    # selector -> MISSION_ITEM_INT
    buf[0] = 1  # 1 % 3 == 1

    # seq = 0
    buf[1:3] = (0).to_bytes(2, "little", signed=False)

    # frame index -> 0 => MAV_FRAME_GLOBAL_RELATIVE_ALT_INT
    buf[3] = 0

    # command index -> 0 => MAV_CMD_NAV_WAYPOINT
    buf[4] = 0

    # current waypoint
    buf[5] = 1

    # autocontinue
    buf[6] = 1

    # params: hold time, acceptance radius, etc.
    put_f32(buf, 7, 5.0)    # param1: hold time (s)
    put_f32(buf, 11, 10.0)  # param2: acceptance radius (m)
    put_f32(buf, 15, 0.0)   # param3
    put_f32(buf, 19, 0.0)   # param4: yaw

    # Coordinates near (lat=39N, lon=-84W), encoded as degE7
    lat_e7 = int(39.0 * 1e7)
    # For negative longitude, store two's complement in 32 bits
    lon_e7 = (int(-84.0 * 1e7)) & 0xFFFFFFFF

    buf[23:27] = lat_e7.to_bytes(4, "little", signed=False)
    buf[27:31] = lon_e7.to_bytes(4, "little", signed=False)

    # altitude 10 m
    put_f32(buf, 31, 10.0)

    return buf


def make_command_long_arm_seed():
    """
    Seed for msg_type == 2 (COMMAND_LONG -> ARM/DISARM).

    Mapping in harness:
      selector = data[0] -> 2 % 3 == 2 => COMMAND_LONG
      cmd_idx  = data[1] % len(cmd_list)
                  0 => MAV_CMD_COMPONENT_ARM_DISARM
      confirmation = data[2]
      param1..param7 = f32 at 3,7,11,15,19,23,27
    """
    buf = bytearray(64)

    # selector -> COMMAND_LONG
    buf[0] = 2  # 2 % 3 == 2

    # cmd_idx 0 => ARM/DISARM
    buf[1] = 0

    # confirmation
    buf[2] = 0

    # param1 = 1.0 => arm
    put_f32(buf, 3, 1.0)

    # rest params can be 0
    put_f32(buf, 7, 0.0)
    put_f32(buf, 11, 0.0)
    put_f32(buf, 15, 0.0)
    put_f32(buf, 19, 0.0)
    put_f32(buf, 23, 0.0)
    put_f32(buf, 27, 0.0)

    return buf


def make_command_long_takeoff_seed():
    """
    Seed for COMMAND_LONG variant that maps to NAV_TAKEOFF-like behavior.

    cmd_idx = 2 => MAV_CMD_NAV_TAKEOFF in the harness cmd_list.
    """
    buf = bytearray(64)

    buf[0] = 2          # selector -> COMMAND_LONG
    buf[1] = 2          # cmd_idx 2 => NAV_TAKEOFF
    buf[2] = 0          # confirmation

    # Use params in the usual MAV_CMD_NAV_TAKEOFF convention:
    # param1: minimum pitch, param4: yaw, param5..7: lat, lon, alt (depending on interpretation)
    put_f32(buf, 3, 15.0)    # min pitch (deg)
    put_f32(buf, 7, 0.0)     # unused here
    put_f32(buf, 11, 0.0)    # unused
    put_f32(buf, 15, 0.0)    # yaw
    put_f32(buf, 19, 39.0)   # latitude-ish
    put_f32(buf, 23, -84.0)  # longitude-ish
    put_f32(buf, 27, 10.0)   # altitude

    return buf


def make_command_long_change_speed_seed():
    """
    Seed for COMMAND_LONG variant that maps to DO_CHANGE_SPEED.

    cmd_idx = 4 => MAV_CMD_DO_CHANGE_SPEED in the harness cmd_list.
    """
    buf = bytearray(64)

    buf[0] = 2          # selector -> COMMAND_LONG
    buf[1] = 4          # cmd_idx 4 => DO_CHANGE_SPEED
    buf[2] = 0          # confirmation

    # params (following the usual convention for DO_CHANGE_SPEED)
    # param1: speed type (1 = ground speed)
    # param2: speed (m/s)
    # param3: throttle (-1 = no change)
    put_f32(buf, 3, 1.0)    # speed type
    put_f32(buf, 7, 5.0)    # speed m/s
    put_f32(buf, 11, -1.0)  # throttle no-change
    put_f32(buf, 15, 0.0)
    put_f32(buf, 19, 0.0)
    put_f32(buf, 23, 0.0)
    put_f32(buf, 27, 0.0)

    return buf


def main():
    SEEDS_DIR.mkdir(exist_ok=True)

    hb = make_heartbeat_seed()
    write_seed("heartbeat.bin", hb)

    mission = make_mission_item_int_seed()
    write_seed("mission_item_int_square_start.bin", mission)

    cmd_arm = make_command_long_arm_seed()
    write_seed("command_long_arm.bin", cmd_arm)

    cmd_takeoff = make_command_long_takeoff_seed()
    write_seed("command_long_takeoff.bin", cmd_takeoff)

    cmd_speed = make_command_long_change_speed_seed()
    write_seed("command_long_change_speed.bin", cmd_speed)

    print(f"[+] All seeds written to {SEEDS_DIR.resolve()}")


if __name__ == "__main__":
    main()


