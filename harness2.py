#!/usr/bin/env python3
import os, sys, socket, select
import afl  # python-afl

# start forkserver ASAP (prevents "Timeout while initializing fork server")
afl.init()

SITL_HOST = os.getenv("SITL_HOST", "127.0.0.1")
# For protocol fuzzing, use the *PX4 offboard* port directly unless you
# deliberately created a separate sink; 14540 is the common one in SITL.
SITL_PORT = int(os.getenv("SITL_PORT", "14540"))
RECV_TIMEOUT_MS = int(os.getenv("RECV_TIMEOUT_MS", "80"))  # keep it snappy

def send_once(data: bytes):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    try:
        s.sendto(data, (SITL_HOST, SITL_PORT))
        # Short, nonblocking feedback path to give AFL some coverage signal
        r, _, _ = select.select([s], [], [], RECV_TIMEOUT_MS / 1000.0)
        if r:
            try:
                s.recvfrom(2048)
            except BlockingIOError:
                pass
    finally:
        s.close()

def main():
    try:
        if len(sys.argv) >= 2 and sys.argv[1] != "-":
            with open(sys.argv[1], "rb") as f:
                data = f.read(8192)
        else:
            data = sys.stdin.buffer.read(8192)
        if data:
            send_once(data)
    except Exception as e:
        # Exit cleanly so AFL doesn't mark a crash
        sys.stderr.write(f"Harness error: {e}\n")
        sys.exit(0)

if __name__ == "__main__":
    main()

