import os
import shutil
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

def _parse_claude_output(raw: str, key: Optional[str] = None):
    """Parse Claude CLI JSON (if any), extract session_id, key value, and boolean verified.
    Returns (extracted_val, boolean_verified, session_id)."""
    session_id = None
    extracted_val = None
    boolean_verified = None
    try:
        import json
        parsed = json.loads(raw)
        # session id
        session_id = (
            (parsed.get('session_id') if isinstance(parsed, dict) else None) or
            ((parsed.get('meta') or {}).get('session_id') if isinstance(parsed, dict) else None) or
            ((parsed.get('data') or {}).get('session_id') if isinstance(parsed, dict) else None) or
            ((parsed.get('output') or {}).get('session_id') if isinstance(parsed, dict) else None)
        )
        # extract value by key
        if key and isinstance(parsed, dict):
            if key in parsed:
                extracted_val = parsed.get(key)
            else:
                for container_key in ('result', 'data', 'output'):
                    container = parsed.get(container_key) or {}
                    if isinstance(container, dict) and key in container:
                        extracted_val = container.get(key)
                        break
        # script-reported boolean verified
        if isinstance(parsed, dict) and 'verified' in parsed:
            boolean_verified = bool(parsed.get('verified'))
    except Exception:
        parsed = None
    # fallback to regex when needed
    if extracted_val is None and key:
        extracted_val = _extract_by_key(raw, key)
    return extracted_val, boolean_verified, session_id

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
    ## TODO: We can add system prompt.

    print(f"\n{'='*50}")
    print(f"POST request received at /generate")
    print(f"Prompt: {prompt}")
    print(f"{'='*50}\n")

    if not prompt:
        return jsonify({"error": "No prompt provided"}), 400

    result = run_claude(prompt)

    # Try to parse session id and possible values from the output
    qr_code_value, _, session_id = _parse_claude_output(result, 'qr_code')
    fallback_text_value, _, _ = _parse_claude_output(result, 'text') if not qr_code_value else (None, None, None)

    qr_files = _list_recent_qr_images()

    response_kind = 'mixed'
    display_text = None

    if qr_files or qr_code_value:
        response_kind = 'qr'
        display_text = qr_code_value or fallback_text_value
    elif fallback_text_value:
        response_kind = 'text'
        display_text = fallback_text_value

    data = {"qr_codes": qr_files}
    if display_text:
        data["text"] = display_text

    return _json_response(response_kind, result, data, session_id=session_id)

@app.route('/validate', methods=['POST'])
def validate():
    mode = request.args.get('mode', 'qr')
    expected_override = request.args.get('expected')  # optional expected token from client

    # Accept session_id via query or JSON body (POST)
    provided_session_id = request.args.get('session_id')
    print(provided_session_id)
    body = request.get_json(silent=True) or {}
    provided_session_id = body.get('session_id', provided_session_id)
    # allow body to override mode/expected if provided
    mode = body.get('mode', mode)
    expected_override = body.get('expected', expected_override)

## TODO: Change the prompts
    if mode == 'qr':
        prompt = """Write a Python script that captures a 10 second video file from this devices webcamera (e.g., input.mp4), scans its frames, and detects the most probable QR code that appears in the video, and saves the QR code as a JSON file.
The JSON output filepath is 'C:\\Users\\post97\\OneDrive - Tartu Ülikool\\PycharmProjects\\studentchallenge\\output.json'.

Requirements: 
1. Output the result as a JSON file to standard output, like this qr_code <QR_CODE_CONTENT>
2. Run the created Python script.
3. Python script saves output as JSON file.

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

## TODO: When it's object detection, gesture detection, validation step skipped. Check it. 
## maybe in the generate step, claude is now running the code and return the result. 
## but we want to align the step. change the UI and let the user push the start button. 
## can we somehow feed the live-streaming video there?  


    # Run Claude (resume if session_id provided)
    raw_result = run_claude(prompt, session_id=provided_session_id)

    # Parse output uniformly
    key_map = {'qr': 'qr_code', 'gesture': 'gesture', 'object': 'object'}
    key = key_map.get(mode)
    extracted_val, boolean_verified, parsed_session_id = _parse_claude_output(raw_result, key)

    # Prefer provided session id; otherwise use parsed
    session_id = provided_session_id or parsed_session_id

    # Determine expected value
    expected_val = None
    if expected_override:
        expected_val = expected_override.strip()
    if mode == 'qr':
        if os.path.exists("uuid.txt"):
            with open("uuid.txt", "r", encoding='utf-8') as f:
                expected_val = f.read().strip()

    # Verification policy:
    # 1) If boolean 'verified' provided by the script, respect it unless an explicit expected is provided (then must match too).
    # 2) If expected provided, require equality with extracted value.
    # 3) Otherwise, presence of a non-empty extracted value counts as success.
    verified = False
    reason = ""

    if expected_val:
        verified = (extracted_val is not None and str(extracted_val).strip() == expected_val)
        reason = f"Compared extracted {key} '{extracted_val}' with expected '{expected_val}'."
        if boolean_verified is not None:
            reason += f" Script-reported verified={boolean_verified}."
    else:
        if boolean_verified is not None:
            verified = bool(boolean_verified)
            reason = f"Script reported verified={boolean_verified}."
            if extracted_val:
                reason += f" Extracted {key}='{extracted_val}'."
        else:
            verified = bool(extracted_val)
            reason = (f"No expected value provided; found {key} '{extracted_val}'."
                      if extracted_val else f"No expected value provided and no {key} found.")

    data = {}
    if extracted_val is not None and key:
        data[key] = extracted_val
    if mode == 'qr':
        data["qr_codes"] = _list_recent_qr_images()

    response = {
        "verified": verified,
        "reason": reason,
        "session_id": session_id,
        "data": data,
        "errors": []
    }

    return jsonify(response)

@app.route('/recognize-qr-image', methods=['GET'])
def recognize_qr_image():#
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

    exe = shutil.which("claude")
    args = [exe, "--output-format", "json"]
    if session_id:
        args += ["-r", session_id]
    args.append(prompt)
 
    try:
        #r"C:\Users\post97\AppData\Roaming\npm\claude.cmd       
        result = subprocess.run(
            args,
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