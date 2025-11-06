#!/usr/bin/env python3
import os
import sys
import socket
import select
import afl # Import the python-afl module

# --- Add afl.init() here, right after imports ---
afl.init()

SITL_HOST = os.getenv("SITL_HOST", "127.0.0.1")
SITL_PORT = int(os.getenv("SITL_PORT", "14560"))
RECV_TIMEOUT_MS = 150

def send_once(data: bytes):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setblocking(False)
    s.sendto(data, (SITL_HOST, SITL_PORT))
    # Optional: short wait for any reply (gives AFL some behavioral signal)
    r, _, _ = select.select([s], [], [], RECV_TIMEOUT_MS / 1000.0)
    if r:
        try:
            s.recvfrom(2048)
        except BlockingIOError:
            pass
    s.close()

def main():
    # --- Add try...except block to handle crashes gracefully ---
    try:
        if len(sys.argv) >= 2 and sys.argv[1] != "-":
            # Read from the file specified by AFL++
            with open(sys.argv[1], "rb") as f:
                data = f.read(8192)
        else:
            # Fallback for manual testing, reads from stdin
            data = sys.stdin.buffer.read(8192)
        if data:
            send_once(data)
            
    except Exception as e:
        # Fuzzer needs the program to terminate gracefully, not crash.
        # This will prevent the "Fork server handshake failed" error.
        sys.stderr.write(f"Harness error: {e}\n")
        # Exit cleanly
        sys.exit(0)

if __name__ == "__main__":
    main()
