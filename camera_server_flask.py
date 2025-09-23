import os
import subprocess
import base64
from flask import Flask, request, send_file, render_template, jsonify
from pathlib import Path
import re
from typing import Dict, Any, List, Optional

app = Flask(__name__)

HOST = "0.0.0.0"
PORT = 8080

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

def _extract_by_key(text: str, key: str) -> Optional[str]:
    if not isinstance(text, str):
        return None
    # patterns: "key <VALUE>", "key: VALUE", "key=VALUE", optional quotes
    m = re.search(rf"{re.escape(key)}\s*[:=]?\s*[\"']?([A-Za-z0-9_.\-]+)[\"']?", text, re.IGNORECASE)
    return m.group(1) if m else None

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

    print(f"\n{'='*50}")
    print(f"POST request received at /generate")
    print(f"Prompt: {prompt}")
    print(f"{'='*50}\n")

    if not prompt:
        return jsonify({"error": "No prompt provided"}), 400

    result = run_claude(prompt)

    qr_files = _list_recent_qr_images()

    kind = 'mixed'
    extracted_text = None

    if qr_files:
        kind = 'qr'
    else:
        qr_code_val = _extract_by_key(result, 'qr_code')
        if qr_code_val:
            kind = 'qr'
            extracted_text = qr_code_val
        else:
            text_val = _extract_by_key(result, 'text')
            if text_val:
                kind = 'text'
                extracted_text = text_val

    data = {"qr_codes": qr_files}
    if extracted_text:
        data["text"] = extracted_text

    print(f"Response data kind: {kind}, data: {data}")

    return _json_response(kind, result, data)

@app.route('/run-claude', methods=['POST'])
def run_claude_endpoint():
    # Deprecated alias → forwards to /generate
    return generate()

@app.route('/validate', methods=['GET'])
def validate():
    mode = request.args.get('mode', 'qr')

    if mode == 'qr':
        prompt = """Write a Python script that captures a 10 second video file from this devices webcamera (e.g., input.mp4), scans its frames, and detects the most probable QR code that appears in the video, and saves the QR code as a JSON file.
The JSON output filepath is 'C:\\Users\\post97\\OneDrive - Tartu Ülikool\\PycharmProjects\\studentchallenge\\output.json'.

Requirements: 
1.	Output the result as a JSON file to standard output, like this qr_code <QR_CODE_CONTENT> 
2.  Run the created Python script.
3.  Python script saves output as JSON file.

ultrathink"""
    elif mode == 'gesture':
        prompt = """Write a Python script that captures a short video from the device's webcam, analyzes frames to detect a gesture, and outputs a line like 'gesture <NAME>' to stdout and saves the result to a JSON file.

Requirements:
1. Output the gesture detection result as a JSON file to standard output.
2. Run the created Python script.

ultrathink"""
    elif mode == 'object':
        prompt = """Write a Python script that captures a short video or frame from the device's webcam, detects the most probable object, and outputs a line like 'object <NAME>' to stdout and saves the result to a JSON file.

Requirements:
1. Output the object detection result as a JSON file to standard output.
2. Run the created Python script.

ultrathink"""
    else:
        return jsonify({"error": f"Unsupported mode: {mode}"}), 400

    result = run_claude(prompt)

    key_map = {
        'qr': 'qr_code',
        'gesture': 'gesture',
        'object': 'object'
    }
    key = key_map.get(mode, None)
    extracted_val = _extract_by_key(result, key) if key else None

    expected_val = None
    errors = []
    if mode == 'qr':
        if os.path.exists("uuid.txt"):
            with open("uuid.txt", "r") as f:
                expected_val = f.read().strip()
    elif mode in ('gesture', 'object'):
        if os.path.exists("expected_token.txt"):
            with open("expected_token.txt", "r") as f:
                expected_val = f.read().strip()

    if expected_val:
        verified = (extracted_val == expected_val)
        reason = f"Compared extracted {key} '{extracted_val}' with expected '{expected_val}'."
    else:
        verified = bool(extracted_val)
        if verified:
            reason = f"No expected value provided; found {key} '{extracted_val}'."
        else:
            reason = f"No expected value provided and no {key} found."

    data = {}
    if extracted_val:
        data[key] = extracted_val
    if mode == 'qr':
        data["qr_codes"] = _list_recent_qr_images()

    response = {
        "verified": verified,
        "reason": reason,
        "session_id": None,
        "data": data,
        "errors": errors
    }

    return jsonify(response)

@app.route('/recognize-qr-image', methods=['GET'])
def recognize_qr_image():
    # Deprecated alias → forwards to /validate (qr mode)
    with app.test_request_context('/validate?mode=qr'):
        return validate()

def run_claude(prompt: str, cwd: str = ".", session_id=None) -> str:
    """
    Run Claude Code CLI with a given prompt.
    
    Args:
        prompt: The text to send to Claude Code.
        cwd: Directory where the codebase is (Claude runs in that context).
    
    Returns:
        Claude's output as a string.
    """
 
    try:
        if session_id:
            result = subprocess.run(
                [r"C:\Users\post97\AppData\Roaming\npm\claude.cmd", prompt, "-r", session_id, "--output-format", "json"],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=True
            )
            return result.stdout
        else:
            result = subprocess.run(
                [r"C:\Users\post97\AppData\Roaming\npm\claude.cmd", prompt, "--output-format", "json"],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=True
            )
            return result.stdout
    except subprocess.CalledProcessError as e:
        return f"Error: {e.stderr}"

if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False)