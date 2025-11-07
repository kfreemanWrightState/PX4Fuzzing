#!/usr/bin/env python3
import os, sys, json, socket, select, time, signal

SITL_HOST = os.getenv("SITL_HOST", "127.0.0.1")
SITL_PORT = int(os.getenv("SITL_PORT", "14560"))
PID_FILE = os.getenv("PID_FILE", "/tmp/px4_pid.json")
RECV_TIMEOUT = float(os.getenv("RECV_TIMEOUT", "0.10"))

def read_pid(path):
    with open(path, "r", encoding="utf-8") as f:
        return int(json.load(f).get("px4_pid"))

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
        pid = read_pid(PID_FILE)
    except Exception as e:
        sys.stderr.write(f"[afl_pid_harness_single] PID file error: {e}\n")
        return 0

    before = pid_alive(pid)
    send_udp(SITL_HOST, SITL_PORT, data)
    time.sleep(0.03)
    after = pid_alive(pid)

    if before and not after:
        sys.stderr.write(f"[afl_pid_harness_single] Detected PX4 PID {pid} terminated; signaling crash.\n")
        os.kill(os.getpid(), signal.SIGSEGV)
    return 0

if __name__ == "__main__":
    sys.exit(main() or 0)
