import unittest
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import monitor

class TestMonitor(unittest.TestCase):
    def test_monitor_import(self):
        self.assertTrue(hasattr(monitor, 'SanguineHealthMonitor'))

if __name__ == '__main__':
    unittest.main()
