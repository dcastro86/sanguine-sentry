import os, json, logging
import cv2
import numpy as np
from PIL import Image
class OCRMixin:
    def find_template_scale_invariant(self, screenshot_img, template_img, threshold=0.7):
        """Finds the best template match across multiple scales (0.5 to 1.5)."""
        screen_cv = cv2.cvtColor(np.array(screenshot_img), cv2.COLOR_RGB2GRAY)
        template_cv = cv2.cvtColor(np.array(template_img), cv2.COLOR_RGB2GRAY)
        t_h, t_w = template_cv.shape[:2]
        best_match = None
        for scale in np.linspace(0.5, 1.5, 21):
            resized_w = int(t_w * scale)
            resized_h = int(t_h * scale)
            if resized_w < 10 or resized_h < 10 or resized_w > screen_cv.shape[1] or (resized_h > screen_cv.shape[0]):
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
            return {'x': center_x, 'y': center_y, 'w': w, 'h': h, 'score': max_val, 'scale': scale}
        return None
    def save_template_from_screen(self, crop_x, crop_y, crop_w, crop_h, name='health_globe.png'):
        """Saves a portion of the screen as a template image for CV matching."""
        name = os.path.basename(name)
        if not name.endswith('.png'):
            name = 'health_globe.png'
        session_type = self.detect_session_type()
        img = None
        if session_type in ['x11', 'windows']:
            try:
                import mss
                with mss.mss() as sct:
                    sct_img = sct.grab(sct.monitors[0])
                    img = Image.frombytes('RGB', sct_img.size, sct_img.bgra, 'raw', 'BGRX')
            except Exception:
                pass
        if not img:
            img = self.grab_wayland_screenshot()
        if not img:
            img = self.grab_from_socket(crop_x, crop_y, crop_w, crop_h)
            if img:
                try:
                    os.makedirs('templates', exist_ok=True)
                    template_path = os.path.join('templates', name)
                    img.save(template_path)
                    self.add_log(f"Saved template '{name}' from socket region ({crop_x}, {crop_y}) size {crop_w}x{crop_h}")
                    x_center = crop_x + crop_w // 2
                    y_center = crop_y + crop_h // 2
                    metadata = {'base_gate_dx': self.config.get('gate_x', 0) - x_center, 'base_gate_dy': self.config.get('gate_y', 0) - y_center, 'base_monitor_dx': self.config.get('monitor_x', 0) - x_center, 'base_monitor_dy': self.config.get('monitor_y', 0) - y_center, 'base_rect_width': self.config.get('rect_width', 30), 'base_rect_height': self.config.get('rect_height', 140), 'base_template_width': crop_w, 'base_template_height': crop_h}
                    meta_path = os.path.join('templates', name.replace('.png', '_metadata.json'))
                    with open(meta_path, 'w') as f:
                        json.dump(metadata, f, indent=2)
                    return True
                except Exception as e:
                    logging.error(f'Failed to save template: {e}')
                    return False
            return False
        try:
            os.makedirs('templates', exist_ok=True)
            screen_w, screen_h = img.size
            cx = max(0, min(crop_x, screen_w - 1))
            cy = max(0, min(crop_y, screen_h - 1))
            cw = max(10, min(crop_w, screen_w - cx))
            ch = max(10, min(crop_h, screen_h - cy))
            crop = img.crop((cx, cy, cx + cw, cy + ch))
            template_path = os.path.join('templates', name)
            crop.save(template_path)
            self.add_log(f"Saved template '{name}' from screen region ({cx}, {cy}) size {cw}x{ch}")
            x_center = cx + cw // 2
            y_center = cy + ch // 2
            metadata = {'base_gate_dx': self.config.get('gate_x', 0) - x_center, 'base_gate_dy': self.config.get('gate_y', 0) - y_center, 'base_monitor_dx': self.config.get('monitor_x', 0) - x_center, 'base_monitor_dy': self.config.get('monitor_y', 0) - y_center, 'base_rect_width': self.config.get('rect_width', 30), 'base_rect_height': self.config.get('rect_height', 140), 'base_template_width': cw, 'base_template_height': ch}
            meta_path = os.path.join('templates', name.replace('.png', '_metadata.json'))
            with open(meta_path, 'w') as f:
                json.dump(metadata, f, indent=2)
            return True
        except Exception as e:
            logging.error(f'Failed to save template: {e}')
            return False
    def _cv_alignment_loop(self):
        self.add_log('CV Auto-Align loop entered.')
        template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
        last_logged_pos = None

        sct_instance = None
        session_type = self.detect_session_type()
        if session_type in ['x11', 'windows']:
            try:
                import mss
                sct_instance = mss.mss()
            except Exception:
                pass

        while True:
            with self.lock:
                if not self.running:
                    break
                enabled = self.config.get('cv_matching_enabled', False)
                template_name = self.config.get('cv_template_filename', 'health_globe.png')
                template_name = os.path.basename(template_name)
                threshold = self.config.get('cv_match_threshold', 0.7)
            if enabled:
                template_path = os.path.join(template_dir, template_name)
                meta_path = os.path.join(template_dir, template_name.replace('.png', '_metadata.json'))
                if os.path.exists(template_path) and os.path.exists(meta_path):
                    try:
                        with open(meta_path, 'r') as f:
                            meta = json.load(f)
                        screenshot_img = None
                        if sct_instance:
                            try:
                                sct_img = sct_instance.grab(sct_instance.monitors[0])
                                screenshot_img = Image.frombytes('RGB', sct_img.size, sct_img.bgra, 'raw', 'BGRX')
                            except Exception:
                                pass
                        if not screenshot_img:
                            screenshot_img = self.grab_wayland_screenshot()
                        if screenshot_img:
                            template_img = Image.open(template_path)
                            match = self.find_template_scale_invariant(screenshot_img, template_img, threshold)
                            if match:
                                mx = match['x']
                                my = match['y']
                                scale = match['scale']
                                base_gate_dx = meta.get('base_gate_dx', 0)
                                base_gate_dy = meta.get('base_gate_dy', 0)
                                base_monitor_dx = meta.get('base_monitor_dx', 0)
                                base_monitor_dy = meta.get('base_monitor_dy', 0)
                                base_rect_width = meta.get('base_rect_width', 30)
                                base_rect_height = meta.get('base_rect_height', 140)
                                new_monitor_x = int(mx + base_monitor_dx * scale)
                                new_monitor_y = int(my + base_monitor_dy * scale)
                                new_gate_x = int(mx + base_gate_dx * scale)
                                new_gate_y = int(my + base_gate_dy * scale)
                                new_rect_width = max(10, int(base_rect_width * scale))
                                new_rect_height = max(10, int(base_rect_height * scale))
                                with self.lock:
                                    has_changed = self.config.get('monitor_x') != new_monitor_x or self.config.get('monitor_y') != new_monitor_y or self.config.get('gate_x') != new_gate_x or (self.config.get('gate_y') != new_gate_y) or (self.config.get('rect_width') != new_rect_width) or (self.config.get('rect_height') != new_rect_height)
                                    if has_changed:
                                        self.config['monitor_x'] = new_monitor_x
                                        self.config['monitor_y'] = new_monitor_y
                                        self.config['gate_x'] = new_gate_x
                                        self.config['gate_y'] = new_gate_y
                                        self.config['rect_width'] = new_rect_width
                                        self.config['rect_height'] = new_rect_height
                                        self.save_config()
                                current_pos = (new_monitor_x, new_monitor_y, new_rect_width, new_rect_height)
                                if last_logged_pos is None or abs(last_logged_pos[0] - new_monitor_x) > 3 or abs(last_logged_pos[1] - new_monitor_y) > 3:
                                    self.add_log(f"Auto-Aligned target via CV to ({new_monitor_x}, {new_monitor_y}) [Scale: {scale:.2f}, Score: {match['score']:.2f}]")
                                    last_logged_pos = current_pos
                            elif last_logged_pos is not None:
                                self.add_log('Auto-Align: Health globe template match score fell below threshold.', 'WARNING')
                                last_logged_pos = None
                    except Exception as e:
                        logging.error(f'Error in CV auto-alignment: {e}')
                elif last_logged_pos != 'missing_warn':
                    self.add_log('Auto-Align active but templates/health_globe.png not found. Please calibrate/save a template.', 'WARNING')
                    last_logged_pos = 'missing_warn'
            else:
                last_logged_pos = None
            self.shutdown_event.wait(5.0)
        self.add_log('CV Auto-Align loop exited.')
