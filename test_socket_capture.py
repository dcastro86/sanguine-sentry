import os
import socket
import sys
import time

def test_loopback():
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and os.path.isdir(runtime_dir):
        socket_path = os.path.join(runtime_dir, "sanguine_sentry.sock")
    else:
        socket_path = os.path.expanduser("~/.sanguine_sentry.sock")
        
    if not os.path.exists(socket_path):
        # Final fallback to /tmp
        socket_path = "/tmp/sanguine_sentry.sock"
        
    if not os.path.exists(socket_path):
        print(f"Error: {socket_path} does not exist. Make sure the Rust daemon is running!")
        sys.exit(1)
        
    print(f"Connecting to {socket_path}...")
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(socket_path)
        
        # We might need to retry a few times to give the PipeWire stream time to initialize and capture the first frame.
        for attempt in range(1, 21):
            print(f"Sending crop request (attempt {attempt}): '100 100 10 10\\n'...")
            client.sendall(b"100 100 10 10\n")
            
            # Read header
            header = b""
            while b"\n" not in header:
                chunk = client.recv(1)
                if not chunk:
                    break
                header += chunk
                
            response = header.decode('utf-8').strip()
            print("Received response header:", response)
            
            if response == "ERROR: No frame captured yet":
                print("No frame captured yet. Waiting 200ms before retrying...")
                time.sleep(0.2)
                continue
            elif not response.startswith("OK"):
                print("Error from socket daemon:", response)
                client.close()
                return
            else:
                # Success
                break
        else:
            print("Failed to capture frame after 20 attempts.")
            client.close()
            return
            
        parts = response.split()
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
