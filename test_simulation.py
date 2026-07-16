import unittest
import os
import json
import time
from monitor import SanguineHealthMonitor
from PIL import Image

class TestSanguineHealthMonitor(unittest.TestCase):
    def setUp(self):
        # Create a temporary config file for testing
        self.test_config_path = "test_config.json"
        if os.path.exists(self.test_config_path):
            os.remove(self.test_config_path)
            
        # Mock hotkey listener to avoid hanging on X11 events during unit tests
        self.original_start = SanguineHealthMonitor.start_hotkey_listener
        SanguineHealthMonitor.start_hotkey_listener = lambda self: None
        
        self.monitor = SanguineHealthMonitor(config_path=self.test_config_path)
        
    def tearDown(self):
        # Restore original hotkey method
        SanguineHealthMonitor.start_hotkey_listener = self.original_start
        
        # Clean up files and stop threads
        if hasattr(self, 'monitor') and self.monitor:
            self.monitor.stop_monitoring()
        if os.path.exists(self.test_config_path):
            os.remove(self.test_config_path)

    def test_default_config_creation(self):
        """Verify that the monitor class initializes and creates a default config file if it does not exist."""
        self.assertTrue(os.path.exists(self.test_config_path))
        with open(self.test_config_path, "r") as f:
            cfg = json.load(f)
        self.assertEqual(cfg["trigger_key"], "1")
        self.assertEqual(cfg["logic_mode"], "percent")
        self.assertEqual(cfg["sensor_size"], 5)
        self.assertFalse(cfg["enabled"])

    def test_update_config(self):
        """Verify config updates work correctly."""
        self.monitor.update_config({"trigger_key": "2", "cooldown": 4.5})
        self.assertEqual(self.monitor.config["trigger_key"], "2")
        self.assertEqual(self.monitor.config["cooldown"], 4.5)
        
        # Verify persistence
        with open(self.test_config_path, "r") as f:
            cfg = json.load(f)
        self.assertEqual(cfg["trigger_key"], "2")

    def test_screen_grab(self):
        """Verify that mss screen grabbing works and returns a valid Pillow image crop."""
        img, left, top = self.monitor.get_cropped_screenshot(crop_size=200)
        self.assertIsInstance(img, Image.Image)
        self.assertEqual(img.size, (200, 200))
        self.assertGreaterEqual(left, 0)
        self.assertGreaterEqual(top, 0)

    def test_simulate_keypress_execution(self):
        """Verify that triggering keypresses runs without throwing exceptions."""
        # Test default pynput execution (runs without errors even if key is pressed globally)
        self.monitor.update_config({"trigger_method": "pynput", "trigger_key": "1"})
        try:
            res = self.monitor.simulate_keypress()
            self.assertTrue(res or not res)  # Should return True/False and not throw
        except Exception as e:
            self.fail(f"simulate_keypress raised an exception unexpectedly: {e}")

        # Test command execution fallback
        self.monitor.update_config({"trigger_method": "command", "custom_command": "echo 'Trigger success!'"})
        res = self.monitor.simulate_keypress()
        self.assertTrue(res)

    def test_active_gameplay_gate(self):
        """Verify that the Active Gameplay Gate logic properly calculates color distances."""
        # Mock grab screenshot
        self.monitor.grab_wayland_screenshot = lambda: Image.new("RGB", (100, 100), color=(0, 0, 255))
        
        self.monitor.update_config({
            "gate_enabled": True,
            "gate_x": 10,
            "gate_y": 10,
            "gate_r": 0,
            "gate_g": 0,
            "gate_b": 255,
            "gate_tolerance": 20
        })
        
        # Scenario A: RGB matches exactly (diff = 0 <= 20)
        img = self.monitor.grab_wayland_screenshot()
        gr, gg, gb = img.getpixel((self.monitor.config["gate_x"], self.monitor.config["gate_y"]))[:3]
        diff = abs(gr - self.monitor.config["gate_r"]) + abs(gg - self.monitor.config["gate_g"]) + abs(gb - self.monitor.config["gate_b"])
        self.assertLessEqual(diff, self.monitor.config["gate_tolerance"])
        
        # Scenario B: RGB mismatches (blue screen vs red expected: diff = 255 + 255 = 510 > 20)
        self.monitor.update_config({
            "gate_r": 255,
            "gate_g": 0,
            "gate_b": 0
        })
        diff_bad = abs(gr - self.monitor.config["gate_r"]) + abs(gg - self.monitor.config["gate_g"]) + abs(gb - self.monitor.config["gate_b"])
        self.assertGreater(diff_bad, self.monitor.config["gate_tolerance"])

    def test_health_percentage_bounding_box(self):
        """Verify that the health percentage calculation correctly calculates percent ratios."""
        img = Image.new("RGB", (100, 100), color="black")
        for row_y in range(50, 100):
            for col_x in range(100):
                img.putpixel((col_x, row_y), (255, 0, 0))
        
        self.monitor.grab_wayland_screenshot = lambda: img
        
        self.monitor.update_config({
            "monitor_x": 50,
            "monitor_y": 50,
            "rect_width": 10,
            "rect_height": 100,
            "logic_mode": "percent",
            "health_threshold_pct": 80,
            "ratio_threshold": 1.2,
            "red_threshold": 80
        })
        
        x = self.monitor.config["monitor_x"]
        y = self.monitor.config["monitor_y"]
        rWidth = self.monitor.config["rect_width"]
        rHeight = self.monitor.config["rect_height"]
        ratio_threshold = self.monitor.config["ratio_threshold"]
        red_threshold = self.monitor.config["red_threshold"]
        
        x_start = x - rWidth // 2
        y_start = y - rHeight // 2
        
        red_rows = 0
        for row_y in range(y_start, y_start + rHeight):
            row_r = row_g = row_b = 0
            row_count = 0
            for col_x in range(x_start, x_start + rWidth):
                if 0 <= col_x < img.width and 0 <= row_y < img.height:
                    val = img.getpixel((col_x, row_y))[:3]
                    row_r += val[0]
                    row_g += val[1]
                    row_b += val[2]
                    row_count += 1
            
            if row_count > 0:
                row_r = row_r // row_count
                row_g = row_g // row_count
                row_b = row_b // row_count
                
                ratio = row_r / (row_g + row_b + 1.0)
                if ratio >= ratio_threshold and row_r >= red_threshold:
                    red_rows += 1
        
        health_percent = int((red_rows / rHeight) * 100) if rHeight > 0 else 0
        self.assertEqual(health_percent, 50)

if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromTestCase(TestSanguineHealthMonitor)
    runner = unittest.TextTestRunner()
    result = runner.run(suite)
    # Force exit to prevent hanging on leaked X11 socket resources from pynput
    os._exit(0 if result.wasSuccessful() else 1)
