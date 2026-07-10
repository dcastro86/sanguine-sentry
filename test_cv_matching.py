import sys
import os
import cv2
import numpy as np
from PIL import Image

# Import SanguineHealthMonitor
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from monitor import SanguineHealthMonitor

def run_test():
    print("Initializing test monitor...")
    monitor = SanguineHealthMonitor()
    
    # 1. Create a baseline synthetic screen (1920x1080)
    print("Generating baseline synthetic game screen (1920x1080)...")
    baseline_screen = np.zeros((1080, 1920, 3), dtype=np.uint8)
    # Draw a distinct health globe (red circle with white boundary) at (125, 905)
    cv2.circle(baseline_screen, (125, 905), 75, (20, 20, 220), -1) # Red fill
    cv2.circle(baseline_screen, (125, 905), 75, (255, 255, 255), 5) # White border
    
    # 2. Extract template centered at (125, 905) size 150x150
    template_img = Image.fromarray(baseline_screen[830:980, 50:200])
    
    # 3. Create a scaled synthetic screen (1280x720)
    # The scale is 1280 / 1920 = 2/3 ≈ 0.67
    print("Generating scaled synthetic game screen (1280x720)...")
    scaled_screen = np.zeros((720, 1280, 3), dtype=np.uint8)
    # New health globe center should be at (125*2/3, 905*2/3) ≈ (83, 603)
    cv2.circle(scaled_screen, (83, 603), 50, (20, 20, 220), -1)
    cv2.circle(scaled_screen, (83, 603), 50, (255, 255, 255), 3)
    scaled_screen_img = Image.fromarray(scaled_screen)
    
    # 4. Run scale-invariant template matching
    print("Running scale-invariant template matching...")
    match = monitor.find_template_scale_invariant(scaled_screen_img, template_img, threshold=0.5)
    
    if match:
        print("\nMatch Success!")
        print(f"Detected Center: ({match['x']}, {match['y']})")
        print(f"Detected Scale: {match['scale']:.2f}")
        print(f"Match Score: {match['score']:.4f}")
        
        # Expected center (83, 603)
        dx = abs(match['x'] - 83)
        dy = abs(match['y'] - 603)
        if dx <= 2 and dy <= 2:
            print("Verification PASSED: Matched coordinates are within tolerance (<= 2px error).")
            return True
        else:
            print(f"Verification FAILED: Coordinates shifted too far by dx={dx}, dy={dy}")
            return False
    else:
        print("\nVerification FAILED: No template match found.")
        return False

if __name__ == "__main__":
    success = run_test()
    sys.exit(0 if success else 1)
