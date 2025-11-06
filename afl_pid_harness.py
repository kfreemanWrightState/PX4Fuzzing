#!/usr/bin/env python3
import os, sys, json, socket, select, time, signal

SITL_HOST = os.getenv("SITL_HOST", "127.0.0.1")
SITL_PORT1 = int(os.getenv("SITL_PORT1", "14560"))
SITL_PORT2 = int(os.getenv("SITL_PORT2", "14570"))
PID_FILE = os.getenv("PID_FILE", "/tmp/px4_pids.json")
RECV_TIMEOUT = float(os.getenv("RECV_TIMEOUT", "0.12"))

def read_pids(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [int(p) for p in data.get("px4_pids", [])]

def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

def send_udp(host, port, buf):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    try:
        s.sendto(buf, (host, port))
        r,_,_ = select.select([s], [], [], RECV_TIMEOUT)
        if r:
            try: s.recvfrom(2048)
            except BlockingIOError: pass
    finally:
        s.close()

def main():
    if len(sys.argv) >= 2 and sys.argv[1] != '-':
        with open(sys.argv[1], 'rb') as f:
            data = f.read(8192)
    else:
        data = sys.stdin.buffer.read(8192)
    if not data: data = b'\xfe'

    try:
        pids = read_pids(PID_FILE)
    except Exception as e:
        sys.stderr.write(f"[afl_pid_harness] PID file error: {e}\n")
        return 0

    before = [pid_alive(p) for p in pids]
    send_udp(SITL_HOST, SITL_PORT1, data)
    send_udp(SITL_HOST, SITL_PORT2, data)
    time.sleep(0.05)
    after = [pid_alive(p) for p in pids]

    for b, a, p in zip(before, after, pids):
        if b and not a:
            sys.stderr.write(f"[afl_pid_harness] Detected PX4 PID {p} terminated; signaling crash.\n")
            os.kill(os.getpid(), signal.SIGSEGV)
    return 0

if __name__ == "__main__":
    sys.exit(main() or 0)
