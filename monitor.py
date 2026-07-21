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

from core.config import ConfigMixin
from core.scanner import ScannerMixin
from core.ocr import OCRMixin
from core.trigger import TriggerMixin
from core.llm import LLMMixin

# Configure logging using absolute path and RotatingFileHandler
def setup_logging():
    log_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug.log")
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file_path,
                maxBytes=5 * 1024 * 1024, # 5MB limit
                backupCount=3,
                encoding="utf-8"
            )
        )
    except Exception:
        # Fallback to stdout logging if log file is unwritable
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers
    )

setup_logging()

class SanguineHealthMonitor(ConfigMixin, ScannerMixin, OCRMixin, TriggerMixin, LLMMixin):
    def __init__(self, config_path='config.json'):
        self.config_path = config_path
        self.config = {}
        self.load_config()
        self.lock = threading.RLock()
        self.screenshot_lock = threading.Lock()
        self.running = False
        self.monitor_thread = None
        self.alignment_thread = None
        self.last_trigger_time = 0.0
        self.logs = []
        self.color_history = []
        self.current_rgb = (0, 0, 0)
        self.current_ratio = 0.0
        self.current_health_pct = 100
        self.last_full_screenshot = None
        self.last_screenshot_time = 0.0
        self.ui_suspended = False
        self.shutdown_event = threading.Event()
        self.keyboard = None
        self.mouse_controller = None
        self.init_keyboard()
        self.init_mouse()
        self.hotkey_listener = None
        self.start_hotkey_listener()
    def add_log(self, message, level='INFO'):
        timestamp = time.strftime('%H:%M:%S')
        log_entry = f'[{timestamp}] [{level}] {message}'
        logging.info(message)
        with self.lock:
            self.logs.append(log_entry)
            if len(self.logs) > 100:
                self.logs.pop(0)
    def _monitor_loop(self):
        self.add_log('Monitoring loop entered.')
        sct_instance = None
        session_type = self.detect_session_type()
        if session_type in ['x11', 'windows']:
            try:
                import mss
                sct_instance = mss.mss()
            except Exception as e:
                self.add_log(f"Failed to initialize mss: {e}", 'WARNING')

        while True:
            with self.lock:
                if not self.running or not self.config.get('enabled', False):
                    break
                x = self.config.get('monitor_x', 200)
                y = self.config.get('monitor_y', 900)
                check_interval = self.config.get('check_interval', 0.05)
                cooldown = self.config.get('cooldown', 5.0)
                logic_mode = self.config.get('logic_mode', 'percent')
                red_threshold = self.config.get('red_threshold', 80)
                ratio_threshold = self.config.get('ratio_threshold', 1.2)
                sensor_size = self.config.get('sensor_size', 5)
                gate_enabled = self.config.get('gate_enabled', False)
                gate_x = self.config.get('gate_x', 0)
                gate_y = self.config.get('gate_y', 0)
                gate_r = self.config.get('gate_r', 0)
                gate_g = self.config.get('gate_g', 0)
                gate_b = self.config.get('gate_b', 0)
                gate_tolerance = self.config.get('gate_tolerance', 20)
                rect_width = self.config.get('rect_width', 10)
                rect_height = self.config.get('rect_height', 100)
                health_threshold_pct = self.config.get('health_threshold_pct', 80)
            try:
                socket_active = self.is_socket_active()
                if socket_active:
                    c_half = sensor_size // 2
                    center_img = self.grab_from_socket(x - c_half, y - c_half, sensor_size, sensor_size)
                    if center_img:
                        img_arr = np.asarray(center_img)[:, :, :3]
                        if img_arr.size > 0:
                            mean_color = img_arr.mean(axis=(0, 1))
                            r, g, b = (int(mean_color[0]), int(mean_color[1]), int(mean_color[2]))
                        else:
                            r, g, b = (0, 0, 0)
                    else:
                        r, g, b = (0, 0, 0)
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
                            health_percent = int((rect_height - 1 - first_red_row_idx) / (rect_height - 1) * 100) if rect_height > 1 else 0
                        else:
                            health_percent = 0
                    else:
                        health_percent = None
                    if health_percent is not None:
                        health_percent = max(0, min(100, health_percent))
                    ui_suspended = False
                    if gate_enabled:
                        gate_size = 40
                        half_g = gate_size // 2
                        g_left = max(0, gate_x - half_g)
                        g_top = max(0, gate_y - half_g)
                        gate_img = self.grab_from_socket(g_left, g_top, gate_size, gate_size)
                        if gate_img:
                            gate_arr = np.asarray(gate_img)[:, :, :3].astype(int)
                            diffs = np.abs(gate_arr[:, :, 0] - gate_r) + np.abs(gate_arr[:, :, 1] - gate_g) + np.abs(gate_arr[:, :, 2] - gate_b)
                            min_diff = np.min(diffs)
                            if min_diff > gate_tolerance:
                                ui_suspended = True
                                if not self.ui_suspended:
                                    self.add_log(f'Monitoring suspended: UI menu detected (Gate color diff: {min_diff})', 'WARNING')
                            elif self.ui_suspended:
                                self.add_log('Monitoring resumed: active gameplay detected.', 'INFO')
                        else:
                            ui_suspended = True
                else:
                    if sct_instance:
                        try:
                            c_half = sensor_size // 2
                            center_monitor = {'top': y - c_half, 'left': x - c_half, 'width': sensor_size, 'height': sensor_size}
                            sct_center = sct_instance.grab(center_monitor)
                            center_img = Image.frombytes('RGB', (sensor_size, sensor_size), sct_center.bgra, 'raw', 'BGRX')
                            img_arr = np.asarray(center_img)[:, :, :3]
                            if img_arr.size > 0:
                                mean_color = img_arr.mean(axis=(0, 1))
                                r, g, b = (int(mean_color[0]), int(mean_color[1]), int(mean_color[2]))
                            else:
                                r, g, b = (0, 0, 0)
                            bx_left = x - rect_width // 2
                            bx_top = y - rect_height // 2
                            bbox_monitor = {'top': bx_top, 'left': bx_left, 'width': rect_width, 'height': rect_height}
                            sct_bbox = sct_instance.grab(bbox_monitor)
                            bbox_img = Image.frombytes('RGB', (rect_width, rect_height), sct_bbox.bgra, 'raw', 'BGRX')
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
                                health_percent = int((rect_height - 1 - first_red_row_idx) / (rect_height - 1) * 100) if rect_height > 1 else 0
                            else:
                                health_percent = 0
                            health_percent = max(0, min(100, health_percent))
                            ui_suspended = False
                            if gate_enabled:
                                gate_size = 40
                                half_g = gate_size // 2
                                gate_monitor = {
                                    'top': max(0, gate_y - half_g), 
                                    'left': max(0, gate_x - half_g), 
                                    'width': gate_size, 
                                    'height': gate_size
                                }
                                sct_gate = sct_instance.grab(gate_monitor)
                                gate_img = Image.frombytes('RGB', (sct_gate.width, sct_gate.height), sct_gate.bgra, 'raw', 'BGRX')
                                gate_arr = np.asarray(gate_img)[:, :, :3].astype(int)
                                diffs = np.abs(gate_arr[:, :, 0] - gate_r) + np.abs(gate_arr[:, :, 1] - gate_g) + np.abs(gate_arr[:, :, 2] - gate_b)
                                min_diff = np.min(diffs)
                                if min_diff > gate_tolerance:
                                    ui_suspended = True
                                    if not self.ui_suspended:
                                        self.add_log(f'Monitoring suspended: UI menu detected (Gate color diff: {min_diff})', 'WARNING')
                                elif self.ui_suspended:
                                    self.add_log('Monitoring resumed: active gameplay detected.', 'INFO')
                        except Exception as e:
                            logging.error(f'X11 mss grab loop failed: {e}')
                            time.sleep(max(0.1, check_interval))
                            continue
                    else:
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
                            r, g, b = (int(mean_color[0]), int(mean_color[1]), int(mean_color[2]))
                        else:
                            r, g, b = (0, 0, 0)
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
                            health_percent = int((rect_height - 1 - first_red_row_idx) / (rect_height - 1) * 100) if rect_height > 1 else 0
                        else:
                            health_percent = 0
                        health_percent = max(0, min(100, health_percent))
                        ui_suspended = False
                        if gate_enabled:
                            gate_size = 40
                            half_g = gate_size // 2
                            gx_start = max(0, gate_x - half_g)
                            gy_start = max(0, gate_y - half_g)
                            gx_end = min(img.width, gate_x + half_g)
                            gy_end = min(img.height, gate_y + half_g)
                            if gx_end > gx_start and gy_end > gy_start:
                                gate_box = img.crop((gx_start, gy_start, gx_end, gy_end))
                                gate_arr = np.asarray(gate_box)[:, :, :3].astype(int)
                                diffs = np.abs(gate_arr[:, :, 0] - gate_r) + np.abs(gate_arr[:, :, 1] - gate_g) + np.abs(gate_arr[:, :, 2] - gate_b)
                                min_diff = np.min(diffs)
                                if min_diff > gate_tolerance:
                                    ui_suspended = True
                                    if not self.ui_suspended:
                                        self.add_log(f'Monitoring suspended: UI menu detected (Gate color diff: {min_diff})', 'WARNING')
                                elif self.ui_suspended:
                                    self.add_log('Monitoring resumed: active gameplay detected.', 'INFO')
                            else:
                                ui_suspended = True
                with self.lock:
                    self.current_rgb = (r, g, b)
                    self.current_ratio = r / (g + b + 1.0)
                    if health_percent is not None:
                        self.current_health_pct = health_percent
                    self.ui_suspended = ui_suspended
                    self.color_history.append({'time': time.time(), 'r': r, 'g': g, 'b': b, 'ratio': self.current_ratio, 'health_pct': health_percent if health_percent is not None else self.current_health_pct})
                    if len(self.color_history) > 60:
                        self.color_history.pop(0)
                triggered = False
                now = time.time()
                if now - self.last_trigger_time >= cooldown and (not self.ui_suspended):
                    if health_percent is not None and health_percent < health_threshold_pct:
                        triggered = True
                        reason = f'Health percentage {health_percent}% < threshold {health_threshold_pct}%'
                    if triggered:
                        self.last_trigger_time = now
                        self.add_log(f'Low health detected: {reason}')
                        self.simulate_keypress()
            except Exception as e:
                logging.exception(f'Error in monitor loop iteration: {e}')
                self.add_log(f'Error in monitor loop iteration: {e}', 'ERROR')
                self.shutdown_event.wait(1.0)
            self.shutdown_event.wait(max(0.01, check_interval))
        self.add_log('Monitoring loop exited.')

if __name__ == "__main__":
    import api.server
    api.server.main()
