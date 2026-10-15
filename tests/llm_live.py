# Live check of both LLM calls against the configured server (not a pytest file: it needs the network and the GPU).
# Prints LLM-LIVE-OK only if deepseek returns both thresholds as numbers and qwen3-vl confirms a drawn red globe.
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image, ImageDraw
from core.llm import LLMMixin

class M(LLMMixin):
    def add_log(self, msg, level="INFO"): print(level, msg)

m = M()
t = m.optimize_thresholds([{"r": 180, "g": 30, "b": 25, "ratio": 3.2}] * 10 + [{"r": 60, "g": 20, "b": 20, "ratio": 1.4}] * 5)
img = Image.new("RGB", (1280, 720), (30, 30, 30))
ImageDraw.Draw(img).ellipse((80, 520, 200, 640), fill=(200, 20, 20))
c = m.analyze_gameplay_screenshot(img)
ok = t and all(isinstance(t.get(k), (int, float)) for k in ("ratio_threshold", "red_threshold")) and c
print("LLM-LIVE-OK" if ok else f"FAIL thresholds={t} coords={c}")
