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
import threading
import time
from datetime import datetime

app = Flask(__name__)
register_camera_routes(app)

HOST = "0.0.0.0"
PORT = 8080

JOBS_DIR = os.path.join(os.getcwd(), "jobs")
os.makedirs(JOBS_DIR, exist_ok=True)


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

def _job_dir(job_id: str) -> str:
    d = os.path.join(JOBS_DIR, job_id)
    os.makedirs(d, exist_ok=True)
    return d

def _write_json(path: str, payload: Dict[str, Any]):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        pass

def _read_job_status(job_id: str) -> Dict[str, Any]:
    d = _job_dir(job_id)
    status_path = os.path.join(d, "status.json")
    script_status_path = os.path.join(d, "script_status.json")
    result_files = {
        'qr': 'uuid.json',
        'gesture': 'gesture_output.json',
        'object': 'object_output.json'
    }
    status = {"job_id": job_id, "phase": "queued", "updated_at": datetime.utcnow().isoformat() + "Z"}
    # status.json (written by background thread)
    try:
        if os.path.exists(status_path):
            with open(status_path, "r", encoding="utf-8") as f:
                s = json.load(f) or {}
                status.update(s)
    except Exception:
        pass
    # script_status.json (written by generated script)
    try:
        if os.path.exists(script_status_path):
            with open(script_status_path, "r", encoding="utf-8") as f:
                s = json.load(f) or {}
                status.update(s)
    except Exception:
        pass
    # merge result json if present
    for fname in result_files.values():
        try:
            p = os.path.join(os.getcwd(), fname)
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    r = json.load(f) or {}
                    if isinstance(r, dict):
                        status.setdefault('data', {})
                        for k, v in r.items():
                            if k not in status['data']:
                                status['data'][k] = v
        except Exception:
            pass
    return status

def _run_claude_background(prompt: str, job_id: str, session_id: Optional[str], mode: str, script_path: str):
    d = _job_dir(job_id)
    log_path = os.path.join(d, "claude.log")
    status_path = os.path.join(d, "status.json")
    _write_json(status_path, {"phase": "generating"})
    stdout, sid = run_claude(prompt, session_id=session_id)
    # write Claude generation log
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(stdout or "")
    except Exception:
        pass

    # After code generation, launch the generated script ourselves (unbuffered)
    try:
        proc_log_path = os.path.join(d, "script.log")
        proc_out = open(proc_log_path, "w", encoding="utf-8")
        proc = subprocess.Popen([sys.executable, "-u", script_path], stdout=proc_out, stderr=subprocess.STDOUT)
        _write_json(status_path, {"phase": "running", "session_id": sid or session_id, "pid": proc.pid, "script": script_path, "mode": mode})
    except Exception as e:
        _write_json(status_path, {"phase": "error", "message": f"Failed to start script: {e}"})

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

    script_name_map = {'qr': 'qr_runner.py', 'gesture': 'gesture_runner.py', 'object': 'object_runner.py'}
    script_path = os.path.join(os.getcwd(), script_name_map.get(mode, 'runner.py'))

    if mode == 'qr':
        prompt = f"""Write (or reuse) a Python script saved exactly as: {script_path}
that captures a livestream from the device's webcam, scans its frames in real time, and detects the most probable QR code that appears in the stream.

Before writing new code:
- Search the current project directory for existing QR detection scripts or utilities (files whose names or contents mention 'qr', 'detect', 'zbar', 'pyzbar', 'opencv', etc.). Prefer reusing and minimally editing an existing script over creating a new one.
- Only create a new file if no suitable script exists. If modifying, keep the filename exactly as above.

Requirements:
- When a QR is detected, print a single line to stdout in the exact format: qr_code <QR_CODE_CONTENT>
- Save a JSON file named uuid.json in the current working directory with JSON content: {{\"uuid\": <QR_CODE_CONTENT>}}
- Exit non-zero with a clear message on failure.
- The script should stop after successful detection or after ~15 seconds.
- Use Python. If dependencies (e.g., opencv-python, pyzbar, numpy, pillow) are missing, install them programmatically; skip install if already available.

IMPORTANT: Do not run the script yourself; only write/update the file at the exact path above.

ultrathink"""
    elif mode == 'gesture':
        prompt = f"""Write (or reuse) a Python script saved exactly as: {script_path}
that uses MediaPipe to detect a thumbs-up gesture from the device's webcam in real time.

Before writing new code:
- Search the current project directory for existing gesture/hand detection scripts (files mentioning 'mediapipe', 'hands', 'gesture', 'thumb', etc.). Prefer reusing and minimally editing an existing script over creating a new one.
- Only create a new file if no suitable script exists. If modifying, keep the filename exactly as above.

The script must:
- Depend on mediapipe and opencv-python; install only if missing.
- Open the default webcam and process frames continuously.
- Use MediaPipe Hands to detect hand landmarks. Consider a 'thumbs_up' gesture when the thumb tip is above the thumb IP joint and the other four fingertips are folded (y-coordinate greater than their respective PIP joints), with a stable detection over ~5 consecutive frames to reduce jitter.
- When detected, print exactly: gesture thumbs_up
- Save a JSON file named gesture_output.json containing at least: {{\"gesture\": \"thumbs_up\", \"verified\": true, \"confidence\": <float between 0 and 1>}}
- Continue running for up to 15 seconds or until detection occurs; then exit.
- Be resilient to missing webcam / install errors by printing a clear error and exiting non-zero.

IMPORTANT: Do not run the script yourself; only write/update the file at the exact path above.

ultrathink"""
    elif mode == 'object':
        prompt = f"""Write (or reuse) a Python script saved exactly as: {script_path}
that uses YOLOv8 (ultralytics) to detect the most probable object from the device's webcam in real time.

Before writing new code:
- Search the current project directory for existing object detection scripts (files mentioning 'yolo', 'ultralytics', 'detect', etc.). Prefer reusing and minimally editing an existing script over creating a new one.
- Only create a new file if no suitable script exists. If modifying, keep the filename exactly as above.

The script must:
- Depend on ultralytics and opencv-python; install only if missing.
- Load a lightweight pretrained YOLOv8 model (e.g., yolov8n.pt). If absent, download via ultralytics.
- Open the default webcam and run inference on frames in real time.
- Track the highest-confidence class observed in the last ~30 frames.
- When confidence for a class exceeds 0.6 for at least 3 frames, print exactly one line to stdout: object <CLASS_NAME>
- Save a JSON file named object_output.json containing at least: {{\"object\": \"<CLASS_NAME>\", \"verified\": true, \"confidence\": <float between 0 and 1>}}
- Continue for up to 20 seconds or until detection occurs; then exit.
- Be resilient to missing webcam / install errors by printing a clear error and exiting non-zero.

IMPORTANT: Do not run the script yourself; only write/update the file at the exact path above.

ultrathink"""

    # Define canonical output file per mode
    filename_map = {'qr': 'uuid.json', 'gesture': 'gesture_output.json', 'object': 'object_output.json'}
    canonical_file = filename_map.get(mode, 'output.json')

    # Create async job and return immediately
    job_id = str(uuid.uuid4())
    d = _job_dir(job_id)
    _write_json(os.path.join(d, "status.json"), {"phase": "queued"})

    # Absolute path to job dir for the generated script to write hooks
    job_dir_abs = d

    # Inject hook instructions into the prompt for READY/DETECTED/DONE phases
    hook_instructions = f"""
\nHook requirements (DO NOT SKIP):
- The server will execute the script at: {script_path}. Do NOT execute it yourself.
- Ensure the script writes the following signals to "{job_dir_abs}/script_status.json":
  * As soon as the webcam is opened and the main processing loop is about to start, do both:
    1) print("PHASE READY", flush=True)
    2) write JSON {{"phase":"ready"}}
  * On successful detection, write JSON with details, e.g. for gesture: {{"phase":"detected","gesture":"thumbs_up","confidence": <0..1>}}
    and also print the one-line stdout key (e.g., "gesture thumbs_up", "object <CLASS>", "qr_code <VALUE>") with flush=True.
  * On normal finish, write {{"phase":"done","verified": <true|false>}}; on fatal error, write {{"phase":"error","message":"..."}} and exit non-zero.
- All prints must use flush=True to avoid buffering.
"""
    prompt_with_hooks = prompt + hook_instructions

    t = threading.Thread(target=_run_claude_background, args=(prompt_with_hooks, job_id, provided_session_id, mode, script_path), daemon=True)
    t.start()

    return jsonify({
        "status": "accepted",
        "job_id": job_id,
        "session_id": provided_session_id,
        "poll_url": f"/job-status?job_id={job_id}"
    }), 202

@app.route('/recognize-qr-image', methods=['GET'])
def recognize_qr_image():#
    # Deprecated alias → forwards to /validate (qr mode)
    with app.test_request_context('/validate?mode=qr'):
        return validate()

@app.route('/job-status', methods=['GET'])
def job_status():
    job_id = request.args.get('job_id')
    if not job_id:
        return jsonify({"error": "job_id is required"}), 400
    s = _read_job_status(job_id)
    return jsonify(s)

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