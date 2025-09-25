import os
import shutil
import subprocess
import base64
import sys
import uuid
import json
from flask import Flask, request, send_file, render_template, jsonify
from pathlib import Path
import re
from typing import Dict, Any, List, Optional
from werkzeug.utils import secure_filename
from camera import register_camera_routes

app = Flask(__name__)
register_camera_routes(app)

HOST = "0.0.0.0"
PORT = 8080


SESSIONS = {}  # {session_id: {"mode": "gesture"|"object", "video_path": str|None}}
UPLOAD_DIR = os.path.join(os.getcwd(), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ---- Helpers ---------------------------------------------------------------
def _list_recent_qr_images() -> List[str]:
    imgs = []
    for ext in ("*.jpg", "*.jpeg", "*.png"):
        for file in Path('.').glob(ext):
            name = file.name.lower()
            if 'qr' in name or 'code' in name:
                imgs.append(file.name)
    if imgs:
        return imgs
    # fallback: last 5 seconds any of the above
    import time
    current_time = time.time()
    recent = []
    for ext in ("*.jpg", "*.jpeg", "*.png"):
        for file in Path('.').glob(ext):
            if current_time - os.path.getmtime(file) < 5:
                recent.append(file.name)
    return recent

# Small helper to standardize API responses
def _json_response(kind: str, output: str, data: Dict[str, Any], errors: List[str] = None, session_id: Optional[str] = None):
    return jsonify({
        "session_id": session_id,
        "kind": kind,
        "output_text": output,
        "data": data,
        "errors": errors or []
    })

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/qr-code/<filename>')
def serve_qr_code(filename):
    """Serve QR code image file."""
    if not os.path.exists(filename):
        return "QR code not found", 404
    return send_file(filename, mimetype='image/png')

@app.route('/generate', methods=['POST'])
def generate():
    prompt = request.get_data(as_text=True)
    mode = request.args.get('mode', 'qr')  # read mode
    ## TODO: We can add system prompt.

    print(f"\n{'='*50}")
    print(f"POST request received at /generate")
    print(f"Prompt: {prompt}")
    print(f"{'='*50}\n")


    if mode == 'qr':
        prompt += """Generate a new random UUID and prepare files for a QR verification workflow.

Before writing new code:
- Search the current project directory for existing Python scripts or notebooks that already generate QR codes and/or UUID files (e.g., names containing 'qr', 'uuid', 'reference'). Prefer reusing or minimally editing an existing script over creating a new one.
- Only generate a new script if no suitable existing code is found. If you modify an existing file, keep its name.

Requirements:
- Create (or overwrite) a file named reference_uuid.json in the current working directory with JSON content: {\"uuid\": \"<GENERATED_UUID>\"}.
- Create (or overwrite) a QR code image file named qr_image.jpeg (JPEG) that encodes exactly the same <GENERATED_UUID> string.
- Use Python. If libraries are missing, install them programmatically (e.g., qrcode[pil], pillow). Avoid reinstalling if already present.
- On completion, print a single line to stdout in the exact format: qr_ready <GENERATED_UUID>

Finally, run the prepared Python script (or the reused script) to produce the outputs.

ultrathink"""
        result, claude_session_id = run_claude(prompt)
        print(claude_session_id)

        # After Claude runs, enumerate QR images and read the reference UUID if present
        qr_files = _list_recent_qr_images()
        uuid_text = None
        try:
            ## TODO: error handling, when reference_uuid.json is malformed.
            if os.path.exists("reference_uuid.json"):
                with open("reference_uuid.json", "r", encoding="utf-8") as f:
                    ref = json.load(f) or {}
                    uuid_text = ref.get("uuid")
        except Exception:
            uuid_text = None

        data = {"qr_codes": qr_files}

        return _json_response('qr', result, data, session_id=claude_session_id)

    elif mode in ('gesture', 'object'):
        # For live modes, immediately show the camera stream in the client UI
        return jsonify({
            "session_id": None,
            "kind": "live",
            "output_text": "",
            "data": {},
            "errors": [],
            "stream_url": "/video_feed"
        }), 200

@app.route('/validate', methods=['POST'])
def validate():
    mode = request.args.get('mode', 'qr')
    expected_override = request.args.get('expected')  # optional expected token from client

    # Accept session_id via query or JSON body (POST)
    provided_session_id = request.args.get('session_id')
    body = request.get_json(silent=True) or {}
    provided_session_id = body.get('session_id', provided_session_id)
    # allow body to override mode/expected if provided
    mode = body.get('mode', mode)
    expected_override = body.get('expected', expected_override)

    if mode == 'qr':
        prompt = """Write (or reuse) a Python script that captures a livestream from the device's webcam, scans its frames in real time, and detects the most probable QR code that appears in the stream.

Before writing new code:
- Search the current project directory for existing QR detection scripts or utilities (files whose names or contents mention 'qr', 'detect', 'zbar', 'pyzbar', 'opencv', etc.). Prefer reusing and minimally editing an existing script over creating a new one.
- Only create a new file if no suitable script exists.

Requirements:
- When a QR is detected, print a single line to stdout in the exact format: qr_code <QR_CODE_CONTENT>
- Save a JSON file named uuid.json in the current working directory with JSON content: {\"uuid\": <QR_CODE_CONTENT>}
- Exit non-zero with a clear message on failure.
- The script should stop after successful detection or after ~15 seconds.
- Use Python. If dependencies (e.g., opencv-python, pyzbar, numpy, pillow) are missing, install them programmatically; skip install if already available.

Finally, run the script (reused or newly created) to produce uuid.json.

ultrathink"""
    ## TODO: split 
    elif mode == 'gesture':
        prompt = """Write (or reuse) and run a Python script that uses MediaPipe to detect a thumbs-up gesture from the device's webcam in real time.

Before writing new code:
- Search the current project directory for existing gesture/hand detection scripts (files mentioning 'mediapipe', 'hands', 'gesture', 'thumb', etc.). Prefer reusing and minimally editing an existing script over creating a new one.
- Only create a new file if no suitable script exists.

The script must:
- Depend on mediapipe and opencv-python; install only if missing.
- Open the default webcam and process frames continuously.
- Use MediaPipe Hands to detect hand landmarks. Consider a 'thumbs_up' gesture when the thumb tip is above the thumb IP joint and the other four fingertips are folded (y-coordinate greater than their respective PIP joints), with a stable detection over ~5 consecutive frames to reduce jitter.
- When detected, print exactly: gesture thumbs_up
- Save a JSON file named gesture_output.json containing at least: {\"gesture\": \"thumbs_up\", \"verified\": true, \"confidence\": <float between 0 and 1>}
- Continue running for up to 15 seconds or until detection occurs; then exit.
- Be resilient to missing webcam / install errors by printing a clear error and exiting non-zero.

Finally, run the prepared (or reused) Python script.

ultrathink"""
    elif mode == 'object':
        prompt = """Write (or reuse) and run a Python script that uses YOLOv8 (ultralytics) to detect the most probable object from the device's webcam in real time.

Before writing new code:
- Search the current project directory for existing object detection scripts (files mentioning 'yolo', 'ultralytics', 'detect', etc.). Prefer reusing and minimally editing an existing script over creating a new one.
- Only create a new file if no suitable script exists.

The script must:
- Depend on ultralytics and opencv-python; install only if missing.
- Load a lightweight pretrained YOLOv8 model (e.g., yolov8n.pt). If absent, download via ultralytics.
- Open the default webcam and run inference on frames in real time.
- Track the highest-confidence class observed in the last ~30 frames.
- When confidence for a class exceeds 0.6 for at least 3 frames, print exactly one line to stdout: object <CLASS_NAME>
- Save a JSON file named object_output.json containing at least: {\"object\": \"<CLASS_NAME>\", \"verified\": true, \"confidence\": <float between 0 and 1>}
- Continue for up to 20 seconds or until detection occurs; then exit.
- Be resilient to missing webcam / install errors by printing a clear error and exiting non-zero.

Finally, run the prepared (or reused) Python script.

ultrathink"""

    # Define canonical output file per mode
    filename_map = {'qr': 'uuid.json', 'gesture': 'gesture_output.json', 'object': 'object_output.json'}
    canonical_file = filename_map.get(mode, 'output.json')

    # Run Claude to generate & run the detector (for qr/gesture/object)
    raw_result, claude_session_id = run_claude(prompt, session_id=provided_session_id)
    print(raw_result)

    # Read produced JSON file
    file_payload = {}
    try:
        if os.path.exists(canonical_file):
            with open(canonical_file, "r", encoding="utf-8") as f:
                file_payload = json.load(f) or {}
    except Exception as e:
        file_payload = {}

    # Determine verified
    boolean_verified = False
    extracted_val = None

    if mode == 'qr':
        # Compare uuid.json against reference_uuid.json
        try:
            detected_uuid = file_payload.get('uuid')
            extracted_val = detected_uuid
            with open('reference_uuid.json', 'r', encoding='utf-8') as rf:
                ref = json.load(rf) or {}
            ref_uuid = ref.get('uuid')
            boolean_verified = bool(detected_uuid and ref_uuid and str(detected_uuid) == str(ref_uuid))
        except Exception:
            boolean_verified = False
    elif mode in ('gesture', 'object'):
        # Trust the script's verified flag; default False
        if isinstance(file_payload, dict) and 'verified' in file_payload:
            try:
                boolean_verified = bool(file_payload.get('verified'))
            except Exception:
                boolean_verified = False
        # Extract value for UI
        key_map = {'gesture': 'gesture', 'object': 'object'}
        key = key_map.get(mode)
        if key and key in file_payload:
            extracted_val = file_payload.get(key)

    # Build data payload
    data = {}
    if extracted_val is not None:
        if mode == 'qr':
            data['qr_code'] = extracted_val
        elif mode == 'gesture':
            data['gesture'] = extracted_val
        elif mode == 'object':
            data['object'] = extracted_val

    # Also merge everything from the file for transparency
    if isinstance(file_payload, dict):
        for k, v in file_payload.items():
            if k not in data or data[k] in (None, ""):
                data[k] = v

    response = {
        "verified": boolean_verified,
        "session_id": provided_session_id,
        "data": data,
        "errors": []
    }
    response["session_id"] = claude_session_id or provided_session_id

    return jsonify(response)

@app.route('/recognize-qr-image', methods=['GET'])
def recognize_qr_image():#
    # Deprecated alias → forwards to /validate (qr mode)
    with app.test_request_context('/validate?mode=qr'):
        return validate()

def run_claude(prompt: str, cwd: str = ".", session_id=None):
    """
    Run Claude Code CLI with a given prompt.

    Returns a tuple: (stdout_text, session_id_str or None)
    """
    exe = shutil.which("claude")
    args = [exe, "--output-format", "json"]
    if session_id:
        args += ["-r", session_id]
    args.append(prompt)

    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True
        )
        stdout = result.stdout
        # Try best-effort parse to extract session_id
        sid = None
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                sid = (
                    parsed.get('session_id')
                    or (parsed.get('meta') or {}).get('session_id')
                    or (parsed.get('data') or {}).get('session_id')
                    or (parsed.get('output') or {}).get('session_id')
                )
        except Exception:
            sid = None
        return stdout, sid
    except subprocess.CalledProcessError as e:
        return f"Error: {e.stderr}", None

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False)