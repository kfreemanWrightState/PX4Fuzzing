#!/usr/bin/env python3
import os
from pymavlink.dialects.v20 import common as mav

os.makedirs("seeds", exist_ok=True)

def dump(msg, path):
    pkt = msg.pack(mav.MAVLink("", 2, 1))  # v2, little sysid/component default
    with open(path, "wb") as f:
        f.write(pkt)

# HEARTBEAT
hb = mav.MAVLink_heartbeat_message(
    type=mav.MAV_TYPE_QUADROTOR,
    autopilot=mav.MAV_AUTOPILOT_PX4,
    base_mode=mav.MAV_MODE_FLAG_MANUAL_INPUT_ENABLED,
    custom_mode=0,
    system_status=mav.MAV_STATE_STANDBY,
    mavlink_version=3
)
dump(hb, "seeds/heartbeat.bin")

# COMMAND_LONG (ARM)
cmd = mav.MAVLink_command_long_message(
    target_system=1, target_component=0,
    command=mav.MAV_CMD_COMPONENT_ARM_DISARM,
    confirmation=0,
    param1=1, param2=0, param3=0, param4=0, param5=0, param6=0, param7=0
)
dump(cmd, "seeds/command_long_arm.bin")

# PARAM_SET
ps = mav.MAVLink_param_set_message(
    target_system=1, target_component=mav.MAV_COMP_ID_AUTOPILOT1,
    param_id=b"MIS_TAKEOFF_ALT",
    param_value=35.0,
    param_type=mav.MAV_PARAM_TYPE_REAL32
)
dump(ps, "seeds/param_set.bin")

print("Seeds written to ./seeds")

