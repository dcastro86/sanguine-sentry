import os
import json
import io
import webbrowser
import secrets
import time
import threading
import logging
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingTCPServer
import urllib.parse
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from monitor import SanguineHealthMonitor
PORT = 8080
monitor_instance = None
last_request_time = 0.0

class SanguineHTTPRequestHandler(BaseHTTPRequestHandler):
    # Disable console logging for every single request to prevent cluttering the stdout logs
    def log_message(self, format, *args):
        pass

    def is_authorized(self):
        # 1. Validate Host header to prevent DNS rebinding
        host = self.headers.get("Host", "")
        if ":" in host:
            host_domain = host.split(":")[0]
        else:
            host_domain = host
            
        bind_ip = "127.0.0.1"
        if monitor_instance and monitor_instance.config:
            bind_ip = monitor_instance.config.get("bind_ip", "127.0.0.1")
        allowed_hosts = {"localhost", "127.0.0.1", bind_ip}
        if host_domain not in allowed_hosts:
            return False
            
        # Static files only require Host validation, not token
        if not self.path.startswith("/api/"):
            return True
            
        # 2. Check CSRF Header and Token
        token = monitor_instance.config.get("api_token") if monitor_instance and monitor_instance.config else ""
        client_token = self.headers.get("X-Sanguine-Auth")
        
        # If no token is set in config (e.g. disabled auth), allow it, otherwise require match
        if not token:
            return True
            
        return client_token == token

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def send_error_response(self, message, status=400):
        self.send_json({"error": message}, status)

    def do_OPTIONS(self):
        if not self.is_authorized():
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        global last_request_time
        last_request_time = time.time()
        if not self.is_authorized():
            self.send_error_response("Unauthorized", 401)
            return
        parsed_url = urllib.parse.urlparse(self.path)
        
        if parsed_url.path == "/health":
            self.send_json({"status": "ok"})
            return

        # API Routes
        if parsed_url.path == "/api/status":
            with monitor_instance.lock:
                status_data = {
                    "running": monitor_instance.running,
                    "config": monitor_instance.config,
                    "current_rgb": monitor_instance.current_rgb,
                    "current_ratio": monitor_instance.current_ratio,
                    "current_health_pct": monitor_instance.current_health_pct,
                    "logs": list(monitor_instance.logs),
                    "color_history": list(monitor_instance.color_history),
                    "ui_suspended": monitor_instance.ui_suspended,
                    "session_type": monitor_instance.detect_session_type(),
                    "capture_method": "mss" if monitor_instance.detect_session_type() in ["x11", "windows"] else ("socket" if os.path.exists(monitor_instance.get_socket_path()) else "spectacle")
                }
            self.send_json(status_data)
            return

        elif parsed_url.path == "/api/screenshot":
            try:
                # Capture cropped screenshot
                img, left, top = monitor_instance.get_cropped_screenshot(crop_size=300)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                img_byte_arr = io.BytesIO()
                img.save(img_byte_arr, format='JPEG', quality=85)
                img_byte_arr = img_byte_arr.getvalue()

                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("X-Crop-Left", str(left))
                self.send_header("X-Crop-Top", str(top))
                self.end_headers()
                self.wfile.write(img_byte_arr)
            except Exception as e:
                import logging
                import traceback
                logging.error(f"Screenshot endpoint failed: {e}\n{traceback.format_exc()}")
                self.send_error_response(f"Screenshot grab failed: {e}", 500)
            return

        elif parsed_url.path == "/api/mouse":
            try:
                from pynput.mouse import Controller as MouseController
                mouse = MouseController()
                mx, my = mouse.position
                self.send_json({"x": mx, "y": my})
            except Exception as e:
                self.send_error_response(f"Mouse read failed: {e}", 500)
            return
        elif parsed_url.path == "/api/debug_log":
            try:
                # debug.log is in the root directory (parent of api/)
                log_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "debug.log")
                if os.path.exists(log_path):
                    with open(log_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()[-200:]
                        content = "".join(lines)
                else:
                    content = f"No debug.log file exists at {log_path} yet. Perform some actions to generate logs."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.end_headers()
                self.wfile.write(content.encode("utf-8"))
            except Exception as e:
                self.send_error_response(f"Failed to read debug.log: {e}", 500)
            return
        # Static file server
        self.serve_static_file(parsed_url.path)

    def do_POST(self):
        global last_request_time
        last_request_time = time.time()
        if not self.is_authorized():
            self.send_error_response("Unauthorized", 401)
            return
        parsed_url = urllib.parse.urlparse(self.path)
        
        # Read content length and parse JSON body
        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length) if content_length > 0 else b""
        
        try:
            body = json.loads(post_data.decode("utf-8")) if post_data else {}
        except Exception:
            self.send_error_response("Invalid JSON payload", 400)
            return

        if parsed_url.path == "/api/config":
            monitor_instance.update_config(body)
            with monitor_instance.lock:
                config_copy = dict(monitor_instance.config)
            self.send_json({"status": "success", "config": config_copy})
            return

        elif parsed_url.path == "/api/toggle":
            monitor_instance.toggle_monitor()
            with monitor_instance.lock:
                running = monitor_instance.running
                config_copy = dict(monitor_instance.config)
            self.send_json({"status": "success", "running": running, "config": config_copy})
            return

        elif parsed_url.path == "/api/trigger":
            success = monitor_instance.simulate_keypress()
            self.send_json({"status": "success" if success else "failed"})
            return

        elif parsed_url.path == "/api/autodetect_game_window":
            result = monitor_instance.autodetect_game_window()
            self.send_json(result)
            return

        elif parsed_url.path == "/api/save_template":
            crop_x = int(body.get("x", 100))
            crop_y = int(body.get("y", 100))
            crop_w = int(body.get("w", 150))
            crop_h = int(body.get("h", 150))
            name = body.get("name", "health_globe.png")
            success = monitor_instance.save_template_from_screen(crop_x, crop_y, crop_w, crop_h, name)
            if success:
                self.send_json({"status": "success"})
            else:
                self.send_json({"status": "failed", "error": "Failed to save template."})
            return
        elif parsed_url.path == "/api/llm/optimize":
            with monitor_instance.lock:
                history = list(monitor_instance.color_history)
            if not history:
                self.send_json({"status": "failed", "error": "No telemetry history available to optimize."})
                return
            new_thresholds = monitor_instance.optimize_thresholds(history)
            if new_thresholds:
                with monitor_instance.lock:
                    if "ratio_threshold" in new_thresholds:
                        monitor_instance.config["ratio_threshold"] = new_thresholds["ratio_threshold"]
                    if "red_threshold" in new_thresholds:
                        monitor_instance.config["red_threshold"] = new_thresholds["red_threshold"]
                    monitor_instance.save_config()
                self.send_json({"status": "success", "config": monitor_instance.config, "thresholds": new_thresholds})
            else:
                self.send_json({"status": "failed", "error": "LLM failed to return optimized thresholds."})
            return

        elif parsed_url.path == "/api/llm/train_coordinates":
            img = None
            if monitor_instance.detect_session_type() in ['x11', 'windows']:
                try:
                    import mss
                    with mss.mss() as sct:
                        sct_img = sct.grab(sct.monitors[0])
                        from PIL import Image
                        img = Image.frombytes('RGB', sct_img.size, sct_img.bgra, 'raw', 'BGRX')
                except Exception as e:
                    logging.error(f"Failed to grab mss screenshot for LLM: {e}")
            else:
                img = monitor_instance.grab_wayland_screenshot()

            if img:
                coords = monitor_instance.analyze_gameplay_screenshot(img)
                if coords:
                    with monitor_instance.lock:
                        for k, v in coords.items():
                            if k in ["monitor_x", "monitor_y", "rect_width", "rect_height", "gate_x", "gate_y"]:
                                monitor_instance.config[k] = v
                        monitor_instance.save_config()
                    self.send_json({"status": "success", "config": monitor_instance.config, "coords": coords})
                else:
                    self.send_json({"status": "failed", "error": "LLM failed to return valid coordinates."})
            else:
                self.send_json({"status": "failed", "error": "Failed to capture screenshot for training."})
            return

        self.send_error_response("Endpoint not found", 404)

    def serve_static_file(self, path):
        # Normalize and find file in web directory
        if path == "/" or path == "":
            path = "/index.html"
            
        # Clean path to prevent directory traversal
        web_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
        rel_path = path.lstrip('/')
        file_path = os.path.abspath(os.path.join(web_dir, rel_path))
        if not file_path.startswith(os.path.abspath(web_dir)):
            self.send_error(403, "Forbidden")
            return
        
        # Fallback to index.html if the file doesn't exist
        if not os.path.exists(file_path) or os.path.isdir(file_path):
            file_path = os.path.join(web_dir, "index.html")

        # Determine Content-Type
        content_type = "text/html"
        if file_path.endswith(".css"):
            content_type = "text/css"
        elif file_path.endswith(".js"):
            content_type = "application/javascript"
        elif file_path.endswith(".json"):
            content_type = "application/json"
        elif file_path.endswith(".png"):
            content_type = "image/png"
        elif file_path.endswith(".jpg") or file_path.endswith(".jpeg"):
            content_type = "image/jpeg"
        elif file_path.endswith(".svg"):
            content_type = "image/svg+xml"

        try:
            if file_path.endswith("index.html"):
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read().encode("utf-8")
            else:
                with open(file_path, "rb") as f:
                    content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"Internal server error serving static file: {e}".encode("utf-8"))

def main():
    global monitor_instance
    print("Initializing Sanguine Sentry HTTP Monitor...")
    
    # Initialize monitor instance
    monitor_instance = SanguineHealthMonitor()
    
    # Auto-start monitoring if enabled in config
    if monitor_instance.config.get("enabled", False):
        monitor_instance.start_monitoring()
        
    # Generate API token if not exists
    if "api_token" not in monitor_instance.config or not monitor_instance.config["api_token"]:
        monitor_instance.config["api_token"] = secrets.token_hex(16)
        monitor_instance.save_config()

    # Create the web folder if it doesn't exist
    web_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
    os.makedirs(web_dir, exist_ok=True)
    
    # Setup HTTP Server
    bind_ip = monitor_instance.config.get("bind_ip", "127.0.0.1")
    port = int(monitor_instance.config.get("port", 8080))
    server_address = (bind_ip, port)
    ThreadingTCPServer.allow_reuse_address = True
    success = False
    try:
        with ThreadingTCPServer(server_address, SanguineHTTPRequestHandler) as httpd:
            display_host = "localhost" if bind_ip == "127.0.0.1" else (bind_ip if bind_ip else "localhost")
            print("Sanguine Sentry Core Active.")
            print("---------------------------------------------")
            token = monitor_instance.config.get("api_token", "")
            dashboard_url = f"http://{display_host}:{port}/"
            auth_launch_url = f"{dashboard_url}?token={token}" if token else dashboard_url
            print(f"Access the dashboard at: {dashboard_url}")
            print("---------------------------------------------")
            
            # Automatically open browser (prefer ungoogled-chromium if available)
            try:
                try:
                    webbrowser.get("chromium").open(auth_launch_url)
                except Exception:
                    webbrowser.open(auth_launch_url)
            except Exception as e:
                print(f"Could not open browser automatically: {e}")
                
            global last_request_time
            last_request_time = time.time()
            
            # Auto-shutdown heartbeat thread (starts tracking after a 20s initial window to allow page load)
            def monitor_heartbeat():
                time.sleep(20.0)
                while True:
                    time.sleep(2.0)
                    # If no requests were received in the last 10 seconds, shut down the server cleanly
                    if time.time() - last_request_time > 10.0:
                        print("\nNo client heartbeat detected (browser tab closed). Auto-shutting down server...")
                        # Run shutdown in a background thread to prevent blocking
                        threading.Thread(target=httpd.shutdown).start()
                        break
            
            heartbeat_thread = threading.Thread(target=monitor_heartbeat, daemon=True)
            heartbeat_thread.start()
            
            import signal
            def shutdown_handler(signum, frame):
                print("\nShutdown signal received. Stopping monitoring...", flush=True)
                threading.Thread(target=httpd.shutdown).start()
            
            signal.signal(signal.SIGINT, shutdown_handler)
            signal.signal(signal.SIGTERM, shutdown_handler)

            httpd.serve_forever()
            success = True
    except Exception:
        pass
    finally:
        if monitor_instance:
            monitor_instance.stop_monitoring()
            # Stop the pynput hotkey listener if it's running
            if monitor_instance.hotkey_listener:
                monitor_instance.hotkey_listener.stop()
        print("Server stopped. Exiting.")
        
        if success:
            try:
                logging.shutdown()
                log_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug.log")
                if os.path.exists(log_file_path):
                    with open(log_file_path, "w"):
                        pass
                    print("Log file successfully cleared on clean shutdown.")
            except Exception as e:
                print(f"Could not clear log file: {e}")

if __name__ == "__main__":
    main()
