#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

from pymavlink import mavutil


def mav_cmd_name(command_id):
    enum_entry = mavutil.mavlink.enums["MAV_CMD"].get(int(command_id))
    return enum_entry.name if enum_entry else f"MAV_CMD_{command_id}"


def load_json_files(search_dir):
    if not search_dir.exists():
        return []
    return sorted(search_dir.glob("*.json"))


def format_item(item):
    command = item.get("command")
    return (
        f"seq={item.get('seq')} "
        f"cmd={command} ({mav_cmd_name(command)}) "
        f"frame={item.get('frame')} "
        f"x={item.get('x')} y={item.get('y')} z={item.get('z')}"
    )


def print_match(source_path, label, meta, items):
    print(f"source: {source_path}")
    print(f"match_type: {label}")
    if meta:
        print("meta:")
        for key, value in sorted(meta.items()):
            print(f"  {key}: {value}")
    print("mission_items:")
    for item in items:
        print(f"  {format_item(item)}")


def try_top_level_match(path, target_hash):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False

    if payload.get("sha1") == target_hash:
        print_match(path, "case", payload.get("meta", {}), payload.get("mission_items", []))
        return True

    for entry in payload.get("recent_missions", []):
        if entry.get("sha1") == target_hash:
            print_match(path, "recent_history", entry.get("meta", {}), entry.get("mission_items", []))
            return True

    return False


def main():
    parser = argparse.ArgumentParser(description="Decode a saved mission hash into MAV_CMDs.")
    parser.add_argument("sha1", help="SHA-1 hash of the testcase bytes")
    parser.add_argument(
        "--search-dir",
        default="/tmp/mission_fuzz_cases",
        help="Directory containing saved mission JSON files",
    )
    args = parser.parse_args()

    search_dir = Path(args.search_dir)
    for path in load_json_files(search_dir):
        if try_top_level_match(path, args.sha1):
            return

    print(f"No mission JSON found for hash {args.sha1} in {search_dir}")


if __name__ == "__main__":
    main()
