import os, json, logging
class ConfigMixin:
    def load_config(self):
        defaults = {'monitor_x': 200, 'monitor_y': 900, 'trigger_key': '1', 'cooldown': 5.0, 'check_interval': 0.05, 'trigger_method': 'pynput', 'custom_command': 'xdotool key 1', 'red_threshold': 80, 'ratio_threshold': 1.2, 'logic_mode': 'percent', 'enabled': False, 'toggle_hotkey': 'f10', 'capture_method': 'auto', 'sensor_size': 5, 'gate_enabled': True, 'gate_x': 0, 'gate_y': 0, 'gate_r': 0, 'gate_g': 0, 'gate_b': 0, 'gate_tolerance': 20, 'rect_width': 30, 'rect_height': 140, 'health_threshold_pct': 80, 'cv_matching_enabled': False, 'cv_template_filename': 'health_globe.png', 'cv_match_threshold': 0.7, 'bind_ip': '127.0.0.1', 'port': 8080}
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r') as f:
                    loaded = json.load(f)
                self.config = {**defaults, **loaded}
            else:
                self.config = defaults
                
            for k in list(self.config.keys()):
                env_val = os.environ.get(f"SENTRY_{k.upper()}")
                if env_val is not None:
                    if isinstance(defaults.get(k), bool):
                        self.config[k] = env_val.lower() in ('true', '1', 'yes')
                    elif isinstance(defaults.get(k), int):
                        self.config[k] = int(env_val)
                    elif isinstance(defaults.get(k), float):
                        self.config[k] = float(env_val)
                    else:
                        self.config[k] = env_val

            self.validate_config()
            self.save_config()
        except Exception as e:
            logging.error(f'Error loading config: {e}')
    def save_config(self):
        try:
            with open(self.config_path, 'w') as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            logging.error(f'Error saving config: {e}')
    def validate_config(self):
        try:
            self.config['cooldown'] = max(0.1, float(self.config.get('cooldown', 5.0)))
            self.config['check_interval'] = max(0.01, float(self.config.get('check_interval', 0.05)))
            h_thresh = self.config.get('health_threshold_pct', 80)
            self.config['health_threshold_pct'] = max(1, min(100, int(h_thresh)))
            self.config['monitor_x'] = max(0, int(self.config.get('monitor_x', 200)))
            self.config['monitor_y'] = max(0, int(self.config.get('monitor_y', 900)))
            self.config['rect_width'] = max(1, int(self.config.get('rect_width', 10)))
            self.config['rect_height'] = max(1, int(self.config.get('rect_height', 100)))
            self.config['sensor_size'] = max(1, int(self.config.get('sensor_size', 5)))
            self.config['logic_mode'] = 'percent'
            self.config['cv_matching_enabled'] = bool(self.config.get('cv_matching_enabled', False))
        except Exception as e:
            logging.error(f'Error validating config: {e}')
    def update_config(self, new_config):
        with self.lock:
            reinit_kbd = 'trigger_method' in new_config and new_config['trigger_method'] != self.config.get('trigger_method') or ('trigger_key' in new_config and new_config['trigger_key'] != self.config.get('trigger_key'))
            reinit_hotkey = 'toggle_hotkey' in new_config and new_config['toggle_hotkey'] != self.config.get('toggle_hotkey')
            self.config.update(new_config)
            self.validate_config()
            if 'gate_x' in new_config and 'gate_y' in new_config:
                if 'gate_r' not in new_config:
                    gx = int(new_config['gate_x'])
                    gy = int(new_config['gate_y'])
                    try:
                        session_type = self.get_session_type()
                        
                        r, g, b = None, None, None
                        
                        if session_type == 'wayland':
                            gate_img = self.grab_from_socket(gx, gy, 1, 1)
                            if gate_img:
                                r, g, b = gate_img.getpixel((0, 0))[:3]
                            else:
                                # Fallback to spectacle if socket fails
                                img = self.grab_wayland_screenshot()
                                if img and 0 <= gx < img.width and 0 <= gy < img.height:
                                    r, g, b = img.getpixel((gx, gy))[:3]
                        else:
                            import mss
                            from PIL import Image
                            with mss.mss() as sct:
                                gate_monitor = {'top': gy, 'left': gx, 'width': 1, 'height': 1}
                                sct_gate = sct.grab(gate_monitor)
                                gate_img = Image.frombytes('RGB', (1, 1), sct_gate.bgra, 'raw', 'BGRX')
                                r, g, b = gate_img.getpixel((0, 0))[:3]
                        
                        if r is not None and g is not None and b is not None:
                            self.config['gate_r'] = r
                            self.config['gate_g'] = g
                            self.config['gate_b'] = b
                            self.add_log(f'Auto-captured gate reference color at ({gx}, {gy}): RGB({r}, {g}, {b})')
                        else:
                            logging.error('Failed to capture gate pixel color: Image was None.')
                    except Exception as e:
                        logging.error(f'Error auto-capturing gate pixel color: {e}')
            self.save_config()
            if reinit_kbd:
                self.init_keyboard()
            if reinit_hotkey:
                self.start_hotkey_listener()
        self.add_log('Configuration updated.')
