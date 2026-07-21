import urllib.request
import json
import base64
import subprocess
from PIL import Image
import io
import time
import os

# Grab a screenshot
subprocess.run(['spectacle', '-f', '-b', '-n', '-o', '/tmp/test_screen.png'])
time.sleep(1)
if not os.path.exists('/tmp/test_screen.png'):
    print("No screenshot")
    exit(1)

img = Image.open('/tmp/test_screen.png')
if img.mode != 'RGB':
    img = img.convert('RGB')
    
# Resize to max 1024 to save time
img.thumbnail((1024, 1024))
buf = io.BytesIO()
img.save(buf, format='JPEG', quality=85)
b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

prompt = """
You are an expert computer vision assistant.
Detect the bounding box of the terminal window or main text area in this screenshot.
Provide the bounding box coordinates as [ymin, xmin, ymax, xmax], where each coordinate is an integer from 0 to 1000 representing a relative position (e.g. 500 is the center).
Return ONLY the array like [ymin, xmin, ymax, xmax].
"""

payload = {
    "model": "llava",
    "prompt": prompt,
    "images": [b64],
    "stream": False
}

req = urllib.request.Request("http://192.168.50.71:11434/api/generate", data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.loads(response.read().decode('utf-8'))
        print(result['response'])
except Exception as e:
    print(f"Error: {e}")
