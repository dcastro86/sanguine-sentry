import threading, subprocess, shlex, time
import logging
class TriggerMixin:
    def init_keyboard(self):
        method = self.config.get('trigger_method', 'pynput')
        if method == 'pynput':
            try:
                from pynput.keyboard import Controller
                self.keyboard = Controller()
                self.add_log('pynput Keyboard Controller initialized successfully.')
            except Exception as e:
                self.add_log(f'Failed to load pynput keyboard controller: {e}. Falling back to command simulation.', 'WARNING')
                self.keyboard = None
        else:
            self.keyboard = None
            self.add_log(f'Configured trigger method: {method}')
    def init_mouse(self):
        try:
            from pynput.mouse import Controller
            self.mouse_controller = Controller()
            self.add_log('pynput Mouse Controller initialized successfully.')
        except Exception as e:
            self.add_log(f'Failed to load pynput mouse controller: {e}', 'WARNING')
            self.mouse_controller = None
    def start_hotkey_listener(self):
        if self.hotkey_listener:
            try:
                self.hotkey_listener.stop()
            except Exception:
                pass
            self.hotkey_listener = None
        hotkey = self.config.get('toggle_hotkey', 'f10').strip().lower()
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
            self.add_log(f'Could not start global hotkey listener ({e}). You can still use the web UI toggle.', 'WARNING')
    def toggle_monitor(self):
        new_state = not self.config.get('enabled', False)
        with self.lock:
            self.config['enabled'] = new_state
            self.save_config()
        status_str = 'ENABLED' if new_state else 'DISABLED'
        self.add_log(f'Monitoring has been {status_str}')
        if new_state:
            self.start_monitoring()
        else:
            self.stop_monitoring()
    def start_monitoring(self):
        with self.lock:
            if self.running:
                return
            if self.monitor_thread and self.monitor_thread.is_alive():
                self.monitor_thread.join(timeout=0.2)
            if self.alignment_thread and self.alignment_thread.is_alive():
                self.alignment_thread.join(timeout=0.2)
            self.shutdown_event.clear()
            self.running = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        self.alignment_thread = threading.Thread(target=self._cv_alignment_loop, daemon=True)
        self.alignment_thread.start()
        self.add_log('Screen monitoring and CV alignment threads started.')
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
        self.add_log('Screen monitoring and CV alignment threads stopped.')
    def simulate_keypress(self):
        method = self.config.get('trigger_method', 'pynput')
        key = self.config.get('trigger_key', '1').strip().lower()
        if method == 'pynput':
            if key in ('mouse4', 'mouse5'):
                if self.mouse_controller:
                    try:
                        from pynput.mouse import Button
                        button = Button.button8 if key == 'mouse4' else Button.button9
                        self.mouse_controller.click(button)
                        self.add_log(f"POTION TRIGGERED! Clicked '{key}' via pynput", 'TRIGGER')
                        return True
                    except Exception as e:
                        self.add_log(f'pynput mouse trigger error: {e}. Falling back to command simulation.', 'WARNING')
            elif self.keyboard:
                try:
                    self.keyboard.press(key)
                    self.keyboard.release(key)
                    self.add_log(f"POTION TRIGGERED! Pressed '{key}' via pynput", 'TRIGGER')
                    return True
                except Exception as e:
                    self.add_log(f'pynput keyboard trigger error: {e}. Falling back to command simulation.', 'WARNING')
        cmd = self.config.get('custom_command', f'xdotool key {key}')
        safe_key = shlex.quote(key)
        if '{key}' in cmd:
            if key == 'mouse4':
                cmd = cmd.replace('key {key}', 'click 8').replace('{key}', 'button8')
            elif key == 'mouse5':
                cmd = cmd.replace('key {key}', 'click 9').replace('{key}', 'button9')
            else:
                cmd = cmd.replace('{key}', safe_key)
        else:
            cmd = cmd.replace('{key}', safe_key)
        try:
            args = shlex.split(cmd)
            subprocess.Popen(args)
            self.add_log(f'POTION TRIGGERED! Executed command: {cmd}', 'TRIGGER')
            return True
        except Exception as e:
            self.add_log(f"Failed to execute command trigger '{cmd}': {e}", 'ERROR')
            return False
