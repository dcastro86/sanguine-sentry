import os
import sys
import json
import time
import logging
import logging.handlers
import threading
import subprocess
import shlex
from PIL import Image
import cv2
import numpy as np
# Removed mss import to only use Spectacle capture on Wayland

# Configure logging using absolute path and RotatingFileHandler
log_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            log_file_path,
            maxBytes=5 * 1024 * 1024, # 5MB limit
            backupCount=3,
            encoding="utf-8"
        )
    ]
)

class SanguineHealthMonitor:
    def __init__(self, config_path="config.json"):
        self.config_path = config_path
        self.config = {}
        self.load_config()
        
        # Thread safety & capture synchronization
        self.lock = threading.RLock()
        self.screenshot_lock = threading.Lock()
        
        # Runtime states
        self.running = False
        self.monitor_thread = None
        self.alignment_thread = None
        self.last_trigger_time = 0.0
        self.logs = []
        self.color_history = []  # List of dicts: {"time": timestamp, "r": r, "g": g, "b": b, "ratio": ratio}
        self.current_rgb = (0, 0, 0)
        self.current_ratio = 0.0
        self.current_health_pct = 100
        self.last_full_screenshot = None
        self.last_screenshot_time = 0.0
        self.ui_suspended = False
        self.shutdown_event = threading.Event()
        
        # Simulator setups
        self.keyboard = None
        self.mouse_controller = None
        self.init_keyboard()
        self.init_mouse()
        
        # Hotkey listener setup
        self.hotkey_listener = None
        self.start_hotkey_listener()
        
    def add_log(self, message, level="INFO"):
        timestamp = time.strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] [{level}] {message}"
        logging.info(message)
        with self.lock:
            self.logs.append(log_entry)
            if len(self.logs) > 100:
                self.logs.pop(0)
                 
    def detect_session_type(self):
        """Autodetects whether the host is running under X11, Wayland, or Windows."""
        import platform
        if platform.system() == "Windows":
            return "windows"
        session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
        if session_type in ["x11", "wayland"]:
            return session_type
        if os.environ.get("WAYLAND_DISPLAY"):
            return "wayland"
        if os.environ.get("DISPLAY"):
            return "x11"
        return "wayland"  # Default fallback if unknown

    def load_config(self):
        defaults = {
            "monitor_x": 200,
            "monitor_y": 900,
            "trigger_key": "1",
            "cooldown": 5.0,
            "check_interval": 0.05,
            "trigger_method": "pynput",
            "custom_command": "xdotool key 1",
            "red_threshold": 80,
            "ratio_threshold": 1.2,
            "logic_mode": "percent",
            "enabled": False,
            "toggle_hotkey": "f10",
            "capture_method": "auto",
            "sensor_size": 5,
            "gate_enabled": True,
            "gate_x": 0,
            "gate_y": 0,
            "gate_r": 0,
            "gate_g": 0,
            "gate_b": 0,
            "gate_tolerance": 20,
            "rect_width": 30,
            "rect_height": 140,
            "health_threshold_pct": 80,
            "cv_matching_enabled": False,
            "cv_template_filename": "health_globe.png",
            "cv_match_threshold": 0.7,
            "bind_ip": "127.0.0.1",
            "port": 8080
        }
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, "r") as f:
                    loaded = json.load(f)
                # Merge loaded config over defaults to handle missing keys
                self.config = {**defaults, **loaded}
                self.config["gate_enabled"] = True # Gate is required, no longer a toggle
                self.validate_config()
                # Save back if we populated missing keys or forced status change
                self.save_config()
            else:
                self.config = defaults
                self.validate_config()
                self.save_config()
        except Exception as e:
            logging.error(f"Error loading config: {e}")
            
    def save_config(self):
        try:
            with open(self.config_path, "w") as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            logging.error(f"Error saving config: {e}")
            
    def validate_config(self):
        try:
            self.config["cooldown"] = max(0.1, float(self.config.get("cooldown", 5.0)))
            self.config["check_interval"] = max(0.01, float(self.config.get("check_interval", 0.05)))
            
            h_thresh = self.config.get("health_threshold_pct", 80)
            self.config["health_threshold_pct"] = max(1, min(100, int(h_thresh)))
            
            self.config["monitor_x"] = max(0, int(self.config.get("monitor_x", 200)))
            self.config["monitor_y"] = max(0, int(self.config.get("monitor_y", 900)))
            
            self.config["rect_width"] = max(1, int(self.config.get("rect_width", 10)))
            self.config["rect_height"] = max(1, int(self.config.get("rect_height", 100)))
            
            self.config["sensor_size"] = max(1, int(self.config.get("sensor_size", 5)))
            
            self.config["logic_mode"] = "percent"
            self.config["cv_matching_enabled"] = bool(self.config.get("cv_matching_enabled", False))
        except Exception as e:
            logging.error(f"Error validating config: {e}")

    def update_config(self, new_config):
        with self.lock:
            # Check if key simulator needs re-initialization
            reinit_kbd = (
                ("trigger_method" in new_config and new_config["trigger_method"] != self.config.get("trigger_method")) or
                ("trigger_key" in new_config and new_config["trigger_key"] != self.config.get("trigger_key"))
            )
            # Check if hotkey needs re-initialization
            reinit_hotkey = "toggle_hotkey" in new_config and new_config["toggle_hotkey"] != self.config.get("toggle_hotkey")
            
            self.config.update(new_config)
            self.validate_config()
            
            if "gate_x" in new_config and "gate_y" in new_config:
                # If gate_r is already sent (extracted clean client-side), skip screen grab
                if "gate_r" not in new_config:
                    gx = int(new_config["gate_x"])
                    gy = int(new_config["gate_y"])
                    img = self.grab_wayland_screenshot()
                    if img:
                        try:
                            if 0 <= gx < img.width and 0 <= gy < img.height:
                                r, g, b = img.getpixel((gx, gy))[:3]
                                self.config["gate_r"] = r
                                self.config["gate_g"] = g
                                self.config["gate_b"] = b
                                self.add_log(f"Auto-captured gate reference color at ({gx}, {gy}): RGB({r}, {g}, {b})")
                        except Exception as e:
                            logging.error(f"Error auto-capturing gate pixel color: {e}")

            self.save_config()
            
            if reinit_kbd:
                self.init_keyboard()
            if reinit_hotkey:
                self.start_hotkey_listener()
                
        self.add_log("Configuration updated.")

    def init_keyboard(self):
        method = self.config.get("trigger_method", "pynput")
        if method == "pynput":
            try:
                from pynput.keyboard import Controller
                self.keyboard = Controller()
                self.add_log("pynput Keyboard Controller initialized successfully.")
            except Exception as e:
                self.add_log(f"Failed to load pynput keyboard controller: {e}. Falling back to command simulation.", "WARNING")
                self.keyboard = None
        else:
            self.keyboard = None
            self.add_log(f"Configured trigger method: {method}")
    def init_mouse(self):
        try:
            from pynput.mouse import Controller
            self.mouse_controller = Controller()
            self.add_log("pynput Mouse Controller initialized successfully.")
        except Exception as e:
            self.add_log(f"Failed to load pynput mouse controller: {e}", "WARNING")
            self.mouse_controller = None
    def start_hotkey_listener(self):
        # Stop existing listener if active
        if self.hotkey_listener:
            try:
                self.hotkey_listener.stop()
            except Exception:
                pass
            self.hotkey_listener = None
            
        hotkey = self.config.get("toggle_hotkey", "f10").strip().lower()
        if not hotkey:
            return
            
        try:
            from pynput import keyboard
            
            def check_hotkey(key):
                if hotkey.startswith('f') and hotkey[1:].isdigit():
                    try:
                        expected_key = getattr(keyboard.Key, hotkey)
                        return key == expected_key
                    except AttributeError:
                        return False
                elif hasattr(key, 'char') and key.char:
                    return key.char.lower() == hotkey
                elif hasattr(key, 'name') and key.name:
                    return key.name.lower() == hotkey
                return False

            def on_press(key):
                if check_hotkey(key):
                    self.toggle_monitor()
                    
            self.hotkey_listener = keyboard.Listener(on_press=on_press)
            self.hotkey_listener.daemon = True
            self.hotkey_listener.start()
            self.add_log(f"Global hotkey listener started. Press '{hotkey.upper()}' to toggle monitoring.")
        except Exception as e:
            self.add_log(f"Could not start global hotkey listener ({e}). You can still use the web UI toggle.", "WARNING")

    def toggle_monitor(self):
        new_state = not self.config.get("enabled", False)
        with self.lock:
            self.config["enabled"] = new_state
            self.save_config()
            
        status_str = "ENABLED" if new_state else "DISABLED"
        self.add_log(f"Monitoring has been {status_str}")
        
        if new_state:
            self.start_monitoring()
        else:
            self.stop_monitoring()

    def start_monitoring(self):
        with self.lock:
            if self.running:
                return
            # Ensure any old threads are cleaned up if still alive
            if self.monitor_thread and self.monitor_thread.is_alive():
                self.monitor_thread.join(timeout=0.2)
            if self.alignment_thread and self.alignment_thread.is_alive():
                self.alignment_thread.join(timeout=0.2)
                
            self.shutdown_event.clear()
            self.running = True
            
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        
        # Spawn CV alignment thread
        self.alignment_thread = threading.Thread(target=self._cv_alignment_loop, daemon=True)
        self.alignment_thread.start()
        
        self.add_log("Screen monitoring and CV alignment threads started.")

    def stop_monitoring(self):
        with self.lock:
            self.running = False
        self.shutdown_event.set()
        if self.monitor_thread:
            self.monitor_thread.join(timeout=1.0)
            self.monitor_thread = None
        if self.alignment_thread:
            self.alignment_thread.join(timeout=1.0)
            self.alignment_thread = None
        self.add_log("Screen monitoring and CV alignment threads stopped.")

    def simulate_keypress(self):
        method = self.config.get("trigger_method", "pynput")
        key = self.config.get("trigger_key", "1").strip().lower()
        
        if method == "pynput":
            # Handle mouse triggers natively
            if key in ("mouse4", "mouse5"):
                if self.mouse_controller:
                    try:
                        from pynput.mouse import Button
                        button = Button.button8 if key == "mouse4" else Button.button9
                        self.mouse_controller.click(button)
                        self.add_log(f"POTION TRIGGERED! Clicked '{key}' via pynput", "TRIGGER")
                        return True
                    except Exception as e:
                        self.add_log(f"pynput mouse trigger error: {e}. Falling back to command simulation.", "WARNING")
            elif self.keyboard:
                try:
                    self.keyboard.press(key)
                    self.keyboard.release(key)
                    self.add_log(f"POTION TRIGGERED! Pressed '{key}' via pynput", "TRIGGER")
                    return True
                except Exception as e:
                    self.add_log(f"pynput keyboard trigger error: {e}. Falling back to command simulation.", "WARNING")
                
        # Command / fallback method
        cmd = self.config.get("custom_command", f"xdotool key {key}")
        # Translate default keyboard command helper to mouse click format if mouse is selected
        if "{key}" in cmd:
            if key == "mouse4":
                cmd = cmd.replace("key {key}", "click 8").replace("{key}", "button8")
            elif key == "mouse5":
                cmd = cmd.replace("key {key}", "click 9").replace("{key}", "button9")
            else:
                cmd = cmd.replace("{key}", key)
        else:
            # Direct replacement fallback
            cmd = cmd.replace("{key}", key)

        try:
            args = shlex.split(cmd)
            # Run asynchronously so we don't block the monitoring thread
            subprocess.Popen(args)
            self.add_log(f"POTION TRIGGERED! Executed command: {cmd}", "TRIGGER")
            return True
        except Exception as e:
            self.add_log(f"Failed to execute command trigger '{cmd}': {e}", "ERROR")
            return False

    def grab_wayland_screenshot(self):
        with self.screenshot_lock:
            # Reuse recent screenshot if it was captured less than 1.0s ago to drastically save CPU and avoid queue congestion
            now = time.time()
            if self.last_full_screenshot and (now - self.last_screenshot_time < 1.0):
                return self.last_full_screenshot

            import uuid
            temp_file = f"/tmp/sanguine_wayland_grab_{uuid.uuid4().hex}.png"
            try:
                # Run spectacle in background to capture screen to a temp file
                # -f (fullscreen), -b (background), -n (nonotify), -o (output file)
                subprocess.run(
                    ["spectacle", "-f", "-b", "-n", "-o", temp_file],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                # Try to read the screenshot for up to 1.5 seconds in case of delayed disk write
                for _ in range(75):
                    if os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
                        try:
                            img = Image.open(temp_file)
                            img.load()
                            self.last_full_screenshot = img
                            self.last_screenshot_time = time.time()
                            return img
                        except Exception:
                            pass
                    time.sleep(0.02)
                logging.warning("Spectacle screenshot file was not created or readable within 200ms.")
            except Exception as e:
                logging.error(f"Spectacle capture failed: {e}")
            finally:
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except Exception:
                        pass
            return None

    def get_socket_path(self):
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        if runtime_dir and os.path.isdir(runtime_dir):
            return os.path.join(runtime_dir, "sanguine_sentry.sock")
        return os.path.expanduser("~/.sanguine_sentry.sock")

    def grab_from_socket(self, x, y, w, h):
        import socket
        socket_path = self.get_socket_path()
        if not os.path.exists(socket_path):
            return None
        
        client = None
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(0.1)  # Sub-millisecond response expected, 100ms is a safe upper bound
            client.connect(socket_path)
            
            # Send coordinates request
            req = f"{x} {y} {w} {h}\n"
            client.sendall(req.encode('utf-8'))
            
            # Read response header (e.g. "OK w h\n")
            header = b""
            while b"\n" not in header:
                chunk = client.recv(1)
                if not chunk:
                    break
                header += chunk
                
            if not header.startswith(b"OK"):
                client.close()
                return None
                
            # Parse header
            parts = header.decode('utf-8').strip().split()
            if len(parts) < 3:
                client.close()
                return None
            ret_w = int(parts[1])
            ret_h = int(parts[2])
            
            # Limit the dimensions to prevent massive allocations / DoS
            if ret_w <= 0 or ret_w > 4096 or ret_h <= 0 or ret_h > 4096:
                client.close()
                logging.warning(f"Rejected invalid frame size from socket: {ret_w}x{ret_h}")
                return None
                
            # Read raw RGB bytes
            expected_size = ret_w * ret_h * 3
            data = b""
            while len(data) < expected_size:
                chunk = client.recv(min(4096, expected_size - len(data)))
                if not chunk:
                    break
                data += chunk
                
            client.close()
            client = None
            
            if len(data) == expected_size:
                return Image.frombytes("RGB", (ret_w, ret_h), data)
        except Exception:
            # Silent fallback if daemon isn't running or socket times out
            pass
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass
            
        return None

    def get_cropped_screenshot(self, crop_size=300):
        """Grabs a cropped screenshot around the target coordinates to send to the UI using high-speed socket daemon (Wayland) or Spectacle fallback."""
        x = self.config.get("monitor_x", 200)
        y = self.config.get("monitor_y", 900)
        
        half = crop_size // 2
        left = max(0, x - half)
        top = max(0, y - half)

        # X11/Windows Native high-speed capture path
        if self.detect_session_type() in ["x11", "windows"]:
            try:
                import mss
                with mss.mss() as sct:
                    monitor = {"top": top, "left": left, "width": crop_size, "height": crop_size}
                    sct_img = sct.grab(monitor)
                    img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                    return img, left, top
            except Exception as e:
                logging.error(f"Native mss cropped capture failed: {e}")
        
        # Try high-speed socket crop first
        img = self.grab_from_socket(left, top, crop_size, crop_size)
        if img:
            return img, left, top
            
        # Fallback to spectacle capture
        img = self.grab_wayland_screenshot()
        if img:
            screen_w, screen_h = img.size
            left = max(0, min(x - half, screen_w - crop_size))
            top = max(0, min(y - half, screen_h - crop_size))
            try:
                crop = img.crop((left, top, left + crop_size, top + crop_size))
                return crop, left, top
            except Exception as e:
                logging.error(f"Error cropping spectacle image: {e}")
        return Image.new("RGB", (crop_size, crop_size), color="black"), 0, 0

    def _monitor_loop(self):
        self.add_log("Monitoring loop entered.")
        
        while True:
            # Check loop run flag
            with self.lock:
                if not self.running or not self.config.get("enabled", False):
                    break
                
                # Fetch settings inside lock
                x = self.config.get("monitor_x", 200)
                y = self.config.get("monitor_y", 900)
                check_interval = self.config.get("check_interval", 0.05)
                cooldown = self.config.get("cooldown", 5.0)
                logic_mode = self.config.get("logic_mode", "percent")
                red_threshold = self.config.get("red_threshold", 80)
                ratio_threshold = self.config.get("ratio_threshold", 1.2)
                sensor_size = self.config.get("sensor_size", 5)
                gate_enabled = self.config.get("gate_enabled", False)
                gate_x = self.config.get("gate_x", 0)
                gate_y = self.config.get("gate_y", 0)
                gate_r = self.config.get("gate_r", 0)
                gate_g = self.config.get("gate_g", 0)
                gate_b = self.config.get("gate_b", 0)
                gate_tolerance = self.config.get("gate_tolerance", 20)
                rect_width = self.config.get("rect_width", 10)
                rect_height = self.config.get("rect_height", 100)
                health_threshold_pct = self.config.get("health_threshold_pct", 80)
            
            try:
                # Try socket-based capturing first
                socket_active = os.path.exists(self.get_socket_path())
                
                if socket_active:
                    # 1. Grab center target region
                    c_half = sensor_size // 2
                    center_img = self.grab_from_socket(x - c_half, y - c_half, sensor_size, sensor_size)
                    
                    if center_img:
                        img_arr = np.asarray(center_img)[:, :, :3]
                        if img_arr.size > 0:
                            mean_color = img_arr.mean(axis=(0, 1))
                            r, g, b = int(mean_color[0]), int(mean_color[1]), int(mean_color[2])
                        else:
                            r, g, b = 0, 0, 0
                    else:
                        r, g, b = (0, 0, 0)
                        
                    # 2. Grab health bounding box region
                    bx_left = x - rect_width // 2
                    bx_top = y - rect_height // 2
                    bbox_img = self.grab_from_socket(bx_left, bx_top, rect_width, rect_height)
                    
                    if bbox_img:
                        img_arr_bbox = np.asarray(bbox_img)[:, :, :3].astype(np.float32)
                        if img_arr_bbox.size > 0:
                            row_means = img_arr_bbox.mean(axis=1)
                            row_r = row_means[:, 0]
                            row_g = row_means[:, 1]
                            row_b = row_means[:, 2]
                            
                            ratios = row_r / (row_g + row_b + 1.0)
                            matches = np.where((ratios >= ratio_threshold) & (row_r >= red_threshold))[0]
                            first_red_row_idx = int(matches[0]) if matches.size > 0 else None
                        else:
                            first_red_row_idx = None
                        
                        if first_red_row_idx is not None:
                            health_percent = int(((rect_height - 1 - first_red_row_idx) / (rect_height - 1)) * 100) if rect_height > 1 else 0
                        else:
                            health_percent = 0
                    else:
                        health_percent = None
                        
                    if health_percent is not None:
                        health_percent = max(0, min(100, health_percent))
                    
                    # 3. Grab active gameplay gate if enabled
                    ui_suspended = False
                    if gate_enabled:
                        gate_img = self.grab_from_socket(gate_x, gate_y, 1, 1)
                        if gate_img:
                            gr, gg, gb = gate_img.getpixel((0, 0))[:3]
                            diff = abs(gr - gate_r) + abs(gg - gate_g) + abs(gb - gate_b)
                            if diff > gate_tolerance:
                                ui_suspended = True
                                if not self.ui_suspended:
                                    self.add_log(f"Monitoring suspended: UI menu detected (Gate color diff: {diff})", "WARNING")
                            else:
                                if self.ui_suspended:
                                    self.add_log("Monitoring resumed: active gameplay detected.", "INFO")
                        else:
                            ui_suspended = True
                            
                else:
                    session_type = self.detect_session_type()
                    if session_type in ["x11", "windows"]:
                        # High-speed native X11/Windows MSS grab
                        try:
                            import mss
                            with mss.mss() as sct:
                                # 1. Grab center target region
                                c_half = sensor_size // 2
                                center_monitor = {"top": y - c_half, "left": x - c_half, "width": sensor_size, "height": sensor_size}
                                sct_center = sct.grab(center_monitor)
                                center_img = Image.frombytes("RGB", (sensor_size, sensor_size), sct_center.bgra, "raw", "BGRX")
                                
                                img_arr = np.asarray(center_img)[:, :, :3]
                                if img_arr.size > 0:
                                    mean_color = img_arr.mean(axis=(0, 1))
                                    r, g, b = int(mean_color[0]), int(mean_color[1]), int(mean_color[2])
                                else:
                                    r, g, b = 0, 0, 0
                                    
                                # 2. Grab health bounding box region
                                bx_left = x - rect_width // 2
                                bx_top = y - rect_height // 2
                                bbox_monitor = {"top": bx_top, "left": bx_left, "width": rect_width, "height": rect_height}
                                sct_bbox = sct.grab(bbox_monitor)
                                bbox_img = Image.frombytes("RGB", (rect_width, rect_height), sct_bbox.bgra, "raw", "BGRX")
                                
                                img_arr_bbox = np.asarray(bbox_img)[:, :, :3].astype(np.float32)
                                if img_arr_bbox.size > 0:
                                    row_means = img_arr_bbox.mean(axis=1)
                                    row_r = row_means[:, 0]
                                    row_g = row_means[:, 1]
                                    row_b = row_means[:, 2]
                                    
                                    ratios = row_r / (row_g + row_b + 1.0)
                                    matches = np.where((ratios >= ratio_threshold) & (row_r >= red_threshold))[0]
                                    first_red_row_idx = int(matches[0]) if matches.size > 0 else None
                                else:
                                    first_red_row_idx = None
                                            
                                if first_red_row_idx is not None:
                                    health_percent = int(((rect_height - 1 - first_red_row_idx) / (rect_height - 1)) * 100) if rect_height > 1 else 0
                                else:
                                    health_percent = 0
                                    
                                health_percent = max(0, min(100, health_percent))
                                
                                # 3. Gameplay Gate
                                ui_suspended = False
                                if gate_enabled:
                                    gate_monitor = {"top": gate_y, "left": gate_x, "width": 1, "height": 1}
                                    sct_gate = sct.grab(gate_monitor)
                                    gate_img = Image.frombytes("RGB", (1, 1), sct_gate.bgra, "raw", "BGRX")
                                    gr, gg, gb = gate_img.getpixel((0, 0))[:3]
                                    diff = abs(gr - gate_r) + abs(gg - gate_g) + abs(gb - gate_b)
                                    if diff > gate_tolerance:
                                        ui_suspended = True
                                        if not self.ui_suspended:
                                            self.add_log(f"Monitoring suspended: UI menu detected (Gate color diff: {diff})", "WARNING")
                                    else:
                                        if self.ui_suspended:
                                            self.add_log("Monitoring resumed: active gameplay detected.", "INFO")
                        except Exception as e:
                            logging.error(f"X11 mss grab loop failed: {e}")
                            time.sleep(max(0.1, check_interval))
                            continue
                    else:
                        # FALLBACK PATH: full spectacle screen screenshot (Wayland)
                        img = self.grab_wayland_screenshot()
                        if not img:
                            time.sleep(max(0.1, check_interval))
                            continue
                        
                        half = sensor_size // 2
                        c_left = max(0, x - half)
                        c_top = max(0, y - half)
                        c_right = min(img.width, x - half + sensor_size)
                        c_bottom = min(img.height, y - half + sensor_size)
                        center_img = img.crop((c_left, c_top, c_right, c_bottom))
                        
                        img_arr = np.asarray(center_img)[:, :, :3]
                        if img_arr.size > 0:
                            mean_color = img_arr.mean(axis=(0, 1))
                            r, g, b = int(mean_color[0]), int(mean_color[1]), int(mean_color[2])
                        else:
                            r, g, b = 0, 0, 0
                            
                        x_start = max(0, x - rect_width // 2)
                        y_start = max(0, y - rect_height // 2)
                        x_end = min(img.width, x + rect_width // 2)
                        y_end = min(img.height, y + rect_height // 2)
                        bbox_img = img.crop((x_start, y_start, x_end, y_end))
                        
                        img_arr_bbox = np.asarray(bbox_img)[:, :, :3].astype(np.float32)
                        if img_arr_bbox.size > 0:
                            row_means = img_arr_bbox.mean(axis=1)
                            row_r = row_means[:, 0]
                            row_g = row_means[:, 1]
                            row_b = row_means[:, 2]
                            
                            ratios = row_r / (row_g + row_b + 1.0)
                            matches = np.where((ratios >= ratio_threshold) & (row_r >= red_threshold))[0]
                            first_red_row_idx = int(matches[0]) if matches.size > 0 else None
                        else:
                            first_red_row_idx = None
                        
                        if first_red_row_idx is not None:
                            health_percent = int(((rect_height - 1 - first_red_row_idx) / (rect_height - 1)) * 100) if rect_height > 1 else 0
                        else:
                            health_percent = 0
                        
                        health_percent = max(0, min(100, health_percent))
                        
                        ui_suspended = False
                        if gate_enabled:
                            if 0 <= gate_x < img.width and 0 <= gate_y < img.height:
                                gr, gg, gb = img.getpixel((gate_x, gate_y))[:3]
                                diff = abs(gr - gate_r) + abs(gg - gate_g) + abs(gb - gate_b)
                                if diff > gate_tolerance:
                                    ui_suspended = True
                                    if not self.ui_suspended:
                                        self.add_log(f"Monitoring suspended: UI menu detected (Gate color diff: {diff})", "WARNING")
                                else:
                                    if self.ui_suspended:
                                        self.add_log("Monitoring resumed: active gameplay detected.", "INFO")
                            else:
                                ui_suspended = True
                
                # Store state
                with self.lock:
                    self.current_rgb = (r, g, b)
                    self.current_ratio = r / (g + b + 1.0)
                    if health_percent is not None:
                        self.current_health_pct = health_percent
                    self.ui_suspended = ui_suspended
                    
                    # Log history (keep last 60 records)
                    self.color_history.append({
                        "time": time.time(),
                        "r": r,
                        "g": g,
                        "b": b,
                        "ratio": self.current_ratio,
                        "health_pct": health_percent if health_percent is not None else self.current_health_pct
                    })
                    if len(self.color_history) > 60:
                        self.color_history.pop(0)
                
                # Evaluate trigger logic
                triggered = False
                now = time.time()
                
                if now - self.last_trigger_time >= cooldown and not self.ui_suspended:
                    if health_percent is not None and health_percent < health_threshold_pct:
                        triggered = True
                        reason = f"Health percentage {health_percent}% < threshold {health_threshold_pct}%"
                            
                    if triggered:
                        self.last_trigger_time = now
                        self.add_log(f"Low health detected: {reason}")
                        self.simulate_keypress()
                        
            except Exception as e:
                self.add_log(f"Error in monitor loop iteration: {e}", "ERROR")
                self.shutdown_event.wait(1.0)
                
            self.shutdown_event.wait(max(0.01, check_interval))
            
        self.add_log("Monitoring loop exited.")

    def autodetect_game_window(self):
        known_titles = [
            "path of exile",
            "path of exile 2",
            "last epoch",
            "grim dawn",
            "diablo iv",
            "diablo 4",
            "diablo iii",
            "diablo 3"
        ]
        try:
            from Xlib import display
            d = display.Display()
            root = d.screen().root
            target_win = None
            
            # 1. Try EWMH client list
            client_list_atom = d.intern_atom('_NET_CLIENT_LIST')
            prop = root.get_full_property(client_list_atom, 0)
            if prop and prop.value:
                for win_id in prop.value:
                    win = d.create_resource_object('window', win_id)
                    try:
                        name = win.get_wm_name()
                        if name:
                            name_lower = name.lower()
                            if any(title in name_lower for title in known_titles):
                                target_win = win
                                break
                    except Exception:
                        continue
            
            # 2. Fallback to recursive tree traversal
            if not target_win:
                def find_rec(win):
                    try:
                        name = win.get_wm_name()
                        if name:
                            name_lower = name.lower()
                            if any(title in name_lower for title in known_titles):
                                return win
                        children = win.query_tree().children
                        for child in children:
                            found = find_rec(child)
                            if found:
                                return found
                    except Exception:
                        pass
                    return None
                target_win = find_rec(root)
                
            if target_win:
                geom = target_win.get_geometry()
                pos = target_win.translate_coords(root, 0, 0)
                abs_x = pos.x
                abs_y = pos.y
                width = geom.width
                height = geom.height
                
                win_name = target_win.get_wm_name()
                self.add_log(f"Auto-detected game window: '{win_name}' at ({abs_x}, {abs_y}) size {width}x{height}")
                
                # Define game-specific coordinate profiles (scaled to 1920x1080 design)
                # Default is Path of Exile profile
                profile = {
                    "monitor_x_pct": 125 / 1920,
                    "monitor_y_pct": 905 / 1080,
                    "gate_x_pct": 135 / 1920,
                    "gate_y_pct": 985 / 1080
                }
                
                win_name_lower = win_name.lower()
                if "diablo" in win_name_lower:
                    # Diablo III / IV profile (centered health globe at bottom-center-left)
                    profile = {
                        "monitor_x_pct": 630 / 1920,
                        "monitor_y_pct": 985 / 1080,
                        "gate_x_pct": 715 / 1920,
                        "gate_y_pct": 921 / 1080
                    }
                elif "last epoch" in win_name_lower:
                    # Last Epoch profile
                    profile = {
                        "monitor_x_pct": 695 / 1920,
                        "monitor_y_pct": 995 / 1080,
                        "gate_x_pct": 960 / 1920,
                        "gate_y_pct": 1025 / 1080
                    }
                elif "grim dawn" in win_name_lower:
                    # Grim Dawn profile
                    profile = {
                        "monitor_x_pct": 600 / 1920,
                        "monitor_y_pct": 1005 / 1080,
                        "gate_x_pct": 960 / 1920,
                        "gate_y_pct": 1035 / 1080
                    }
                
                new_monitor_x = abs_x + int(width * profile["monitor_x_pct"])
                new_monitor_y = abs_y + int(height * profile["monitor_y_pct"])
                new_gate_x = abs_x + int(width * profile["gate_x_pct"])
                new_gate_y = abs_y + int(height * profile["gate_y_pct"])
                
                with self.lock:
                    self.config["monitor_x"] = new_monitor_x
                    self.config["monitor_y"] = new_monitor_y
                    self.config["gate_x"] = new_gate_x
                    self.config["gate_y"] = new_gate_y
                    self.save_config()
                    
                return {
                    "success": True,
                    "title": win_name,
                    "geometry": {"x": abs_x, "y": abs_y, "width": width, "height": height},
                    "monitor_x": new_monitor_x,
                    "monitor_y": new_monitor_y,
                    "gate_x": new_gate_x,
                    "gate_y": new_gate_y
                }
            else:
                self.add_log("No known ARPG game window detected.")
                return {"success": False, "error": "No running game window found matching known ARPG list."}
                
        except Exception as e:
            self.add_log(f"Error during game window autodetection: {str(e)}")
            return {"success": False, "error": str(e)}

    def find_template_scale_invariant(self, screenshot_img, template_img, threshold=0.7):
        """Finds the best template match across multiple scales (0.5 to 1.5)."""
        # Convert PIL images to cv2 grayscale images
        screen_cv = cv2.cvtColor(np.array(screenshot_img), cv2.COLOR_RGB2GRAY)
        template_cv = cv2.cvtColor(np.array(template_img), cv2.COLOR_RGB2GRAY)

        t_h, t_w = template_cv.shape[:2]
        best_match = None

        # Scale range 0.5 to 1.5 with step size 0.05
        for scale in np.linspace(0.5, 1.5, 21):
            resized_w = int(t_w * scale)
            resized_h = int(t_h * scale)
            if resized_w < 10 or resized_h < 10 or resized_w > screen_cv.shape[1] or resized_h > screen_cv.shape[0]:
                continue

            resized_template = cv2.resize(template_cv, (resized_w, resized_h), interpolation=cv2.INTER_AREA)

            res = cv2.matchTemplate(screen_cv, resized_template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)

            if best_match is None or max_val > best_match[0]:
                best_match = (max_val, max_loc, scale, resized_w, resized_h)

        if best_match and best_match[0] >= threshold:
            max_val, max_loc, scale, w, h = best_match
            center_x = max_loc[0] + w // 2
            center_y = max_loc[1] + h // 2
            return {
                "x": center_x,
                "y": center_y,
                "w": w,
                "h": h,
                "score": max_val,
                "scale": scale
            }
        return None

    def save_template_from_screen(self, crop_x, crop_y, crop_w, crop_h, name="health_globe.png"):
        """Saves a portion of the screen as a template image for CV matching."""
        name = os.path.basename(name)
        if not name.endswith(".png"):
            name = "health_globe.png"
        session_type = self.detect_session_type()
        img = None
        if session_type in ["x11", "windows"]:
            try:
                import mss
                with mss.mss() as sct:
                    sct_img = sct.grab(sct.monitors[0])
                    img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            except Exception:
                pass
        if not img:
            img = self.grab_wayland_screenshot()
            
        if not img:
            # Fallback: grab from socket if the coordinates are small
            img = self.grab_from_socket(crop_x, crop_y, crop_w, crop_h)
            if img:
                try:
                    os.makedirs("templates", exist_ok=True)
                    template_path = os.path.join("templates", name)
                    img.save(template_path)
                    self.add_log(f"Saved template '{name}' from socket region ({crop_x}, {crop_y}) size {crop_w}x{crop_h}")
                    
                    x_center = crop_x + crop_w // 2
                    y_center = crop_y + crop_h // 2
                    metadata = {
                        "base_gate_dx": self.config.get("gate_x", 0) - x_center,
                        "base_gate_dy": self.config.get("gate_y", 0) - y_center,
                        "base_monitor_dx": self.config.get("monitor_x", 0) - x_center,
                        "base_monitor_dy": self.config.get("monitor_y", 0) - y_center,
                        "base_rect_width": self.config.get("rect_width", 30),
                        "base_rect_height": self.config.get("rect_height", 140),
                        "base_template_width": crop_w,
                        "base_template_height": crop_h
                    }
                    meta_path = os.path.join("templates", name.replace(".png", "_metadata.json"))
                    with open(meta_path, "w") as f:
                        json.dump(metadata, f, indent=2)
                    return True
                except Exception as e:
                    logging.error(f"Failed to save template: {e}")
                    return False
            return False

        try:
            os.makedirs("templates", exist_ok=True)
            screen_w, screen_h = img.size
            cx = max(0, min(crop_x, screen_w - 1))
            cy = max(0, min(crop_y, screen_h - 1))
            cw = max(10, min(crop_w, screen_w - cx))
            ch = max(10, min(crop_h, screen_h - cy))
            
            crop = img.crop((cx, cy, cx + cw, cy + ch))
            template_path = os.path.join("templates", name)
            crop.save(template_path)
            self.add_log(f"Saved template '{name}' from screen region ({cx}, {cy}) size {cw}x{ch}")
            
            x_center = cx + cw // 2
            y_center = cy + ch // 2
            metadata = {
                "base_gate_dx": self.config.get("gate_x", 0) - x_center,
                "base_gate_dy": self.config.get("gate_y", 0) - y_center,
                "base_monitor_dx": self.config.get("monitor_x", 0) - x_center,
                "base_monitor_dy": self.config.get("monitor_y", 0) - y_center,
                "base_rect_width": self.config.get("rect_width", 30),
                "base_rect_height": self.config.get("rect_height", 140),
                "base_template_width": cw,
                "base_template_height": ch
            }
            meta_path = os.path.join("templates", name.replace(".png", "_metadata.json"))
            with open(meta_path, "w") as f:
                json.dump(metadata, f, indent=2)
            return True
        except Exception as e:
            logging.error(f"Failed to save template: {e}")
            return False

    def _cv_alignment_loop(self):
        self.add_log("CV Auto-Align loop entered.")
        template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
        
        last_logged_pos = None
        
        while True:
            with self.lock:
                if not self.running:
                    break
                enabled = self.config.get("cv_matching_enabled", False)
                template_name = self.config.get("cv_template_filename", "health_globe.png")
                template_name = os.path.basename(template_name)
                threshold = self.config.get("cv_match_threshold", 0.7)
                
            if enabled:
                template_path = os.path.join(template_dir, template_name)
                meta_path = os.path.join(template_dir, template_name.replace(".png", "_metadata.json"))
                
                if os.path.exists(template_path) and os.path.exists(meta_path):
                    try:
                        with open(meta_path, "r") as f:
                            meta = json.load(f)
                            
                        screenshot_img = None
                        session_type = self.detect_session_type()
                        if session_type in ["x11", "windows"]:
                            try:
                                import mss
                                with mss.mss() as sct:
                                    sct_img = sct.grab(sct.monitors[0])
                                    screenshot_img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                            except Exception:
                                pass
                                
                        if not screenshot_img:
                            screenshot_img = self.grab_wayland_screenshot()
                            
                        if screenshot_img:
                            template_img = Image.open(template_path)
                            match = self.find_template_scale_invariant(screenshot_img, template_img, threshold)
                            
                            if match:
                                mx = match["x"]
                                my = match["y"]
                                scale = match["scale"]
                                
                                base_gate_dx = meta.get("base_gate_dx", 0)
                                base_gate_dy = meta.get("base_gate_dy", 0)
                                base_monitor_dx = meta.get("base_monitor_dx", 0)
                                base_monitor_dy = meta.get("base_monitor_dy", 0)
                                base_rect_width = meta.get("base_rect_width", 30)
                                base_rect_height = meta.get("base_rect_height", 140)
                                
                                new_monitor_x = int(mx + base_monitor_dx * scale)
                                new_monitor_y = int(my + base_monitor_dy * scale)
                                new_gate_x = int(mx + base_gate_dx * scale)
                                new_gate_y = int(my + base_gate_dy * scale)
                                new_rect_width = max(10, int(base_rect_width * scale))
                                new_rect_height = max(10, int(base_rect_height * scale))
                                
                                with self.lock:
                                    has_changed = (
                                        self.config.get("monitor_x") != new_monitor_x or
                                        self.config.get("monitor_y") != new_monitor_y or
                                        self.config.get("gate_x") != new_gate_x or
                                        self.config.get("gate_y") != new_gate_y or
                                        self.config.get("rect_width") != new_rect_width or
                                        self.config.get("rect_height") != new_rect_height
                                    )
                                    if has_changed:
                                        self.config["monitor_x"] = new_monitor_x
                                        self.config["monitor_y"] = new_monitor_y
                                        self.config["gate_x"] = new_gate_x
                                        self.config["gate_y"] = new_gate_y
                                        self.config["rect_width"] = new_rect_width
                                        self.config["rect_height"] = new_rect_height
                                        self.save_config()
                                
                                current_pos = (new_monitor_x, new_monitor_y, new_rect_width, new_rect_height)
                                if last_logged_pos is None or abs(last_logged_pos[0] - new_monitor_x) > 3 or abs(last_logged_pos[1] - new_monitor_y) > 3:
                                    self.add_log(f"Auto-Aligned target via CV to ({new_monitor_x}, {new_monitor_y}) [Scale: {scale:.2f}, Score: {match['score']:.2f}]")
                                    last_logged_pos = current_pos
                            else:
                                if last_logged_pos is not None:
                                    self.add_log("Auto-Align: Health globe template match score fell below threshold.", "WARNING")
                                    last_logged_pos = None
                    except Exception as e:
                        logging.error(f"Error in CV auto-alignment: {e}")
                else:
                    if last_logged_pos != "missing_warn":
                        self.add_log("Auto-Align active but templates/health_globe.png not found. Please calibrate/save a template.", "WARNING")
                        last_logged_pos = "missing_warn"
            else:
                last_logged_pos = None
                
            self.shutdown_event.wait(5.0)
        self.add_log("CV Auto-Align loop exited.")

if __name__ == "__main__":
    # Test execution
    monitor = SanguineHealthMonitor()
    print("Sanguine Sentry Flask Monitor instantiated. Enabled status:", monitor.config.get("enabled"))
