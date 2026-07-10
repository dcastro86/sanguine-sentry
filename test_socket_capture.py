import os
import socket
import sys
import time

def test_loopback():
    socket_path = "/tmp/sanguine_sentry.sock"
    if not os.path.exists(socket_path):
        print("Error: /tmp/sanguine_sentry.sock does not exist. Make sure the Rust daemon is running!")
        sys.exit(1)
        
    print("Connecting to /tmp/sanguine_sentry.sock...")
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(socket_path)
        
        # Request a 10x10 crop at (100, 100)
        print("Sending crop request: '100 100 10 10'...")
        client.sendall(b"100 100 10 10")
        
        # Read header
        header = b""
        while b"\n" not in header:
            chunk = client.recv(1)
            if not chunk:
                break
            header += chunk
            
        print("Received response header:", header.decode('utf-8').strip())
        if not header.startswith(b"OK"):
            client.close()
            return
            
        parts = header.decode('utf-8').strip().split()
        w = int(parts[1])
        h = int(parts[2])
        
        expected_bytes = w * h * 3
        print(f"Reading {expected_bytes} bytes of raw RGB data...")
        
        data = b""
        start = time.time()
        while len(data) < expected_bytes:
            chunk = client.recv(min(1024, expected_bytes - len(data)))
            if not chunk:
                break
            data += chunk
            
        duration = (time.time() - start) * 1000
        print(f"Successfully read {len(data)} bytes of RGB data in {duration:.2f} ms!")
        client.close()
    except Exception as e:
        print("Failed to perform socket loopback test:", e)

if __name__ == "__main__":
    test_loopback()
