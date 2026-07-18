import os, time, logging, subprocess
from PIL import Image
class ScannerMixin:
    def detect_session_type(self):
        """Autodetects whether the host is running under X11, Wayland, or Windows."""
        if hasattr(self, '_cached_session_type'):
            return self._cached_session_type
        import platform
        if platform.system() == 'Windows':
            self._cached_session_type = 'windows'
            return 'windows'
        session_type = os.environ.get('XDG_SESSION_TYPE', '').lower()
        if session_type in ['x11', 'wayland']:
            self._cached_session_type = session_type
            return session_type
        if os.environ.get('WAYLAND_DISPLAY'):
            self._cached_session_type = 'wayland'
            return 'wayland'
        if os.environ.get('DISPLAY'):
            self._cached_session_type = 'x11'
            return 'x11'
        self._cached_session_type = 'wayland'
        return 'wayland'
    def grab_wayland_screenshot(self):
        with self.screenshot_lock:
            now = time.time()
            if self.last_full_screenshot and now - self.last_screenshot_time < 1.0:
                return self.last_full_screenshot
            import uuid
            temp_file = f'/tmp/sanguine_wayland_grab_{uuid.uuid4().hex}.png'
            try:
                subprocess.run(['spectacle', '-f', '-b', '-n', '-o', temp_file], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
                logging.warning('Spectacle screenshot file was not created or readable within 200ms.')
            except Exception as e:
                logging.error(f'Spectacle capture failed: {e}')
            finally:
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except Exception:
                        pass
            return None
    def get_socket_path(self):
        if hasattr(self, '_cached_socket_path'):
            return self._cached_socket_path
        runtime_dir = os.environ.get('XDG_RUNTIME_DIR')
        if runtime_dir and os.path.isdir(runtime_dir):
            self._cached_socket_path = os.path.join(runtime_dir, 'sanguine_sentry.sock')
        else:
            self._cached_socket_path = os.path.expanduser('~/.sanguine_sentry.sock')
        return self._cached_socket_path

    def is_socket_active(self):
        now = time.time()
        if hasattr(self, '_last_socket_check_time') and now - self._last_socket_check_time < 1.0:
            return getattr(self, '_last_socket_check', False)
        active = os.path.exists(self.get_socket_path())
        self._last_socket_check = active
        self._last_socket_check_time = now
        return active
    def grab_from_socket(self, x, y, w, h):
        import socket
        socket_path = self.get_socket_path()
        if not os.path.exists(socket_path):
            return None
        client = None
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(0.1)
            client.connect(socket_path)
            req = f'{x} {y} {w} {h}\n'
            client.sendall(req.encode('utf-8'))
            header = b''
            while b'\n' not in header:
                chunk = client.recv(1)
                if not chunk:
                    break
                header += chunk
            if not header.startswith(b'OK'):
                client.close()
                return None
            parts = header.decode('utf-8').strip().split()
            if len(parts) < 3:
                client.close()
                return None
            ret_w = int(parts[1])
            ret_h = int(parts[2])
            if ret_w <= 0 or ret_w > 4096 or ret_h <= 0 or (ret_h > 4096):
                client.close()
                logging.warning(f'Rejected invalid frame size from socket: {ret_w}x{ret_h}')
                return None
            expected_size = ret_w * ret_h * 3
            data = b''
            while len(data) < expected_size:
                chunk = client.recv(min(4096, expected_size - len(data)))
                if not chunk:
                    break
                data += chunk
            client.close()
            client = None
            if len(data) == expected_size:
                return Image.frombytes('RGB', (ret_w, ret_h), data)
        except Exception:
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
        x = self.config.get('monitor_x', 200)
        y = self.config.get('monitor_y', 900)
        half = crop_size // 2
        left = max(0, x - half)
        top = max(0, y - half)
        if self.detect_session_type() in ['x11', 'windows']:
            try:
                import mss
                with mss.mss() as sct:
                    monitor = {'top': top, 'left': left, 'width': crop_size, 'height': crop_size}
                    sct_img = sct.grab(monitor)
                    img = Image.frombytes('RGB', sct_img.size, sct_img.bgra, 'raw', 'BGRX')
                    return (img, left, top)
            except Exception as e:
                logging.error(f'Native mss cropped capture failed: {e}')
        img = self.grab_from_socket(left, top, crop_size, crop_size)
        if img:
            return (img, left, top)
        img = self.grab_wayland_screenshot()
        if img:
            screen_w, screen_h = img.size
            left = max(0, min(x - half, screen_w - crop_size))
            top = max(0, min(y - half, screen_h - crop_size))
            try:
                crop = img.crop((left, top, left + crop_size, top + crop_size))
                return (crop, left, top)
            except Exception as e:
                logging.error(f'Error cropping spectacle image: {e}')
        return (Image.new('RGB', (crop_size, crop_size), color='black'), 0, 0)
    def autodetect_game_window(self):
        known_titles = ['path of exile', 'path of exile 2', 'last epoch', 'grim dawn', 'diablo iv', 'diablo 4', 'diablo iii', 'diablo 3']
        try:
            from Xlib import display
            d = display.Display()
            root = d.screen().root
            target_win = None
            client_list_atom = d.intern_atom('_NET_CLIENT_LIST')
            prop = root.get_full_property(client_list_atom, 0)
            if prop and prop.value:
                for win_id in prop.value:
                    win = d.create_resource_object('window', win_id)
                    try:
                        name = win.get_wm_name()
                        if name:
                            name_lower = name.lower()
                            if any((title in name_lower for title in known_titles)):
                                target_win = win
                                break
                    except Exception:
                        continue
            if not target_win:
    
                def find_rec(win):
                    try:
                        name = win.get_wm_name()
                        if name:
                            name_lower = name.lower()
                            if any((title in name_lower for title in known_titles)):
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
                profile = {'monitor_x_pct': 125 / 1920, 'monitor_y_pct': 905 / 1080, 'gate_x_pct': 135 / 1920, 'gate_y_pct': 985 / 1080}
                win_name_lower = win_name.lower()
                if 'diablo' in win_name_lower:
                    profile = {'monitor_x_pct': 630 / 1920, 'monitor_y_pct': 985 / 1080, 'gate_x_pct': 715 / 1920, 'gate_y_pct': 921 / 1080}
                elif 'last epoch' in win_name_lower:
                    profile = {'monitor_x_pct': 695 / 1920, 'monitor_y_pct': 995 / 1080, 'gate_x_pct': 960 / 1920, 'gate_y_pct': 1025 / 1080}
                elif 'grim dawn' in win_name_lower:
                    profile = {'monitor_x_pct': 600 / 1920, 'monitor_y_pct': 1005 / 1080, 'gate_x_pct': 960 / 1920, 'gate_y_pct': 1035 / 1080}
                new_monitor_x = abs_x + int(width * profile['monitor_x_pct'])
                new_monitor_y = abs_y + int(height * profile['monitor_y_pct'])
                new_gate_x = abs_x + int(width * profile['gate_x_pct'])
                new_gate_y = abs_y + int(height * profile['gate_y_pct'])
                with self.lock:
                    self.config['monitor_x'] = new_monitor_x
                    self.config['monitor_y'] = new_monitor_y
                    self.config['gate_x'] = new_gate_x
                    self.config['gate_y'] = new_gate_y
                    self.save_config()
                return {'success': True, 'title': win_name, 'geometry': {'x': abs_x, 'y': abs_y, 'width': width, 'height': height}, 'monitor_x': new_monitor_x, 'monitor_y': new_monitor_y, 'gate_x': new_gate_x, 'gate_y': new_gate_y}
            else:
                self.add_log('No known ARPG game window detected.')
                return {'success': False, 'error': 'No running game window found matching known ARPG list.'}
        except Exception as e:
            self.add_log(f'Error during game window autodetection: {str(e)}')
            return {'success': False, 'error': str(e)}
