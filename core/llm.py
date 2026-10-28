import os
import json
import urllib.request
import base64
import logging

# Any OpenAI-compatible server; the default is the Unraid llama-swap, so the in-game calls stay off the workstation GPU.
LLM_BASE_URL = os.getenv("SENTRY_LLM_BASE_URL", "http://192.168.50.71:9292/v1")
TEXT_MODEL = os.getenv("SENTRY_LLM_MODEL", "deepseek-r1-8b")
VISION_MODEL = os.getenv("SENTRY_LLM_VISION_MODEL", "qwen3-vl")

def _chat_json(model, content, schema, timeout):
    """One chat completion constrained to `schema`. Returns the answer text only (a reasoning model's
    thinking stays in reasoning_content). The timeout covers a model swap on the server (~15-35 s)."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "response_format": {"type": "json_schema", "json_schema": {"schema": schema}},
        # deepseek-r1 can think until the context is full and answer nothing; this caps it (ignored by non-reasoning models).
        "thinking_budget_tokens": 1024,
    }
    req = urllib.request.Request(f"{LLM_BASE_URL}/chat/completions", data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))["choices"][0]["message"]["content"]

class LLMMixin:
    def optimize_thresholds(self, color_history):
        """Analyzes recent color telemetry using the text LLM and returns optimal thresholds."""
        if not color_history:
            return None
        
        prompt = f"""
You are an expert system configuring an auto-flask utility.
The user's health globe color telemetry over the last few seconds is provided as JSON.
Analyze the data and determine the optimal 'ratio_threshold' (red / (green+blue+1)) and 'red_threshold' (absolute red value 0-255).
The goal is to avoid false positives (high ratio when not a health globe) but catch when the health globe drops (ratio stays high, but red absolute drops, or both drop if it's empty).
Just return a valid JSON object with no markdown and no extra text, exactly like this:
{{"ratio_threshold": 1.25, "red_threshold": 75}}

Telemetry:
{json.dumps(color_history[-30:])}
"""
        schema = {"type": "object", "properties": {"ratio_threshold": {"type": "number"}, "red_threshold": {"type": "number"}},
                  "required": ["ratio_threshold", "red_threshold"]}
        try:
            self.add_log("Sending telemetry to the LLM for threshold optimization...")
            new_thresholds = json.loads(_chat_json(TEXT_MODEL, prompt, schema, 120.0))
            self.add_log(f"LLM successfully calculated new thresholds: {new_thresholds}")
            return new_thresholds
        except Exception as e:
            logging.error(f"LLM text model error: {e}")
            self.add_log(f"LLM text model error (model '{TEXT_MODEL}' at {LLM_BASE_URL}): {e}", "ERROR")
            return None

    def analyze_gameplay_screenshot(self, image):
        """Analyzes a full screenshot using a hybrid OpenCV + vision-LLM approach to find the health globe."""
        import cv2
        import numpy as np
        import io
        from PIL import Image

        self.add_log("Scanning bottom half of screen for red globe candidates using OpenCV...")
        
        cv_img = np.array(image)
        if image.mode == 'RGB':
            cv_img = cv_img[:, :, ::-1].copy() # Convert RGB to BGR
        else:
            image = image.convert('RGB')
            cv_img = np.array(image)[:, :, ::-1].copy()

        h, w = cv_img.shape[:2]
        bottom_half_y = h // 2
        bottom_half = cv_img[bottom_half_y:, :]

        hsv = cv2.cvtColor(bottom_half, cv2.COLOR_BGR2HSV)
        
        # Red spans across 0 and 180 in OpenCV HSV (0-10 and 160-180)
        mask1 = cv2.inRange(hsv, np.array([0, 100, 100]), np.array([10, 255, 255]))
        mask2 = cv2.inRange(hsv, np.array([160, 100, 100]), np.array([180, 255, 255]))
        mask = cv2.bitwise_or(mask1, mask2)
        
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        candidates = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area > 1000: # large enough blob
                x, y, cw, ch = cv2.boundingRect(cnt)
                # Aspect ratio check (globes are roughly square/circular, bars are wide)
                if 0.4 < (cw / ch) < 2.5:
                    candidates.append((x, y, cw, ch))

        self.add_log(f"OpenCV found {len(candidates)} potential red UI candidate(s). Asking '{VISION_MODEL}' to verify...")

        for (x, y, cw, ch) in candidates:
            # Draw a thick green bounding box around the candidate on the bottom half of the image
            # We use the bottom half so the vision model still has enough context of the UI, but it's not a massive 4K image.
            annotated_img = bottom_half.copy()
            cv2.rectangle(annotated_img, (x, y), (x + cw, y + ch), (0, 255, 0), 4)
            
            crop_pil = Image.fromarray(annotated_img[:, :, ::-1])
            img_byte_arr = io.BytesIO()
            crop_pil.save(img_byte_arr, format='JPEG', quality=85)
            encoded_image = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
            
            prompt = """
Look at this image from an Action RPG game (like Path of Exile or Diablo).
I have drawn a bright GREEN BOUNDING BOX around a red UI element.
Is the item inside the green box the player's main Health Globe / Health Bar?
Answer ONLY with a JSON object containing a single boolean field 'is_health_globe', exactly like this:
{"is_health_globe": true}
"""
            content = [{"type": "text", "text": prompt},
                       {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded_image}"}}]
            schema = {"type": "object", "properties": {"is_health_globe": {"type": "boolean"}}, "required": ["is_health_globe"]}

            try:
                ans = json.loads(_chat_json(VISION_MODEL, content, schema, 90.0))
                self.add_log(f"{VISION_MODEL} answered: {ans}")
                
                is_health_globe = ans.get('is_health_globe') in [True, "true", "True", "yes"]
                
                if not is_health_globe and len(candidates) == 1:
                    self.add_log(f"{VISION_MODEL} rejected this image, but since it is the ONLY red globe detected by OpenCV, we will assume it is the health globe.")
                    is_health_globe = True
                    
                if is_health_globe:
                    absolute_x = x + cw // 2
                    absolute_y = bottom_half_y + y + ch // 2
                    
                    coords = {
                        "monitor_x": absolute_x,
                        "monitor_y": absolute_y,
                        "rect_width": cw + 20,
                        "rect_height": ch + 20,
                        "gate_x": absolute_x,
                        "gate_y": absolute_y - 200 # approximate a static spot above the globe
                    }
                    self.add_log(f"LLM vision confirmed Health Globe at {coords}")
                    return coords
            except Exception as e:
                logging.error(f"LLM vision model error: {e}")
                
        if len(candidates) == 0:
            self.add_log(f"OpenCV could not find any red globe candidates.", "ERROR")
        else:
            self.add_log(f"LLM vision could not confirm any candidates.", "ERROR")
        return None
