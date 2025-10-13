import os
import shutil
import subprocess
import sys
import uuid
import json
from flask import Flask, request, send_file, render_template, jsonify
from pathlib import Path
import re
from typing import Dict, Any, List, Optional
from camera import register_camera_routes
import threading
import time
from datetime import datetime


app = Flask(__name__)
register_camera_routes(app)

HOST = "0.0.0.0"
PORT = 8080

REGISTRY_PATH = os.path.join(os.getcwd(), "runners_registry.json")
JOBS_DIR = os.path.join(os.getcwd(), "jobs")
os.makedirs(JOBS_DIR, exist_ok=True)



# Registry helpers
def _load_registry() -> Dict[str, Dict[str, str]]:
    if os.path.exists(REGISTRY_PATH):
        try:
            with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
                if isinstance(data, dict):
                    data.setdefault('gesture', {})
                    data.setdefault('object', {})
                    return data
        except Exception:
            pass
    return {'gesture': {}, 'object': {}}

def _save_registry(reg: Dict[str, Dict[str, str]]):
    try:
        with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _filter_result_fields(mode: str, data: dict) -> dict:
    allowed = {
        'qr': {'expected_uuid', 'uuid', 'verified', 'timestamp'},
        'gesture': {'confidence', 'gesture', 'verified', 'timestamp'},
        'object': {'confidence', 'object', 'verified', 'timestamp'},
    }
    if not isinstance(data, dict):
        return {}
    keys = allowed.get(mode, set())
    return {k: v for k, v in data.items() if k in keys}

def _slugify(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", name.strip().lower()).strip("_")
    s = re.sub(r"_+", "_", s)
    return s or "item"

def _register_runner(mode: str, display_name: str, source_script: Optional[str]) -> Optional[str]:
    """Copy the current runner to a named file and register it. Returns target script path if success."""
    if mode not in ("gesture", "object"):
        return None
    if not display_name:
        return None
    reg = _load_registry()
    slug = _slugify(display_name)
    suffix = "_gesture.py" if mode == "gesture" else "_object.py"
    target = os.path.join(os.getcwd(), f"{slug}{suffix}")
    # Copy source if available and different
    try:
        if source_script and os.path.exists(source_script) and os.path.abspath(source_script) != os.path.abspath(target):
            shutil.copyfile(source_script, target)
    except Exception:
        # best-effort; if copy fails, still register path
        pass
    # Register
    reg.setdefault(mode, {})
    reg[mode][display_name] = os.path.basename(target)
    _save_registry(reg)
    return target

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

# --- Canonical output file per mode
def _canonical_result_file(mode: str) -> str:
    mapping = {'qr': 'uuid.json', 'gesture': 'gesture_output.json', 'object': 'object_output.json'}
    return mapping.get(mode, 'output.json')


# --- Background watcher for QR validation
def _start_qr_watcher(job_id: str, timeout_sec: int = 30):
    """Background watcher for QR: waits for uuid.json, compares to reference_uuid.json,
    then updates jobs/<job_id>/status.json with detected/done and verified.
    """
    def _watch():
        d = _job_dir(job_id)
        status_path = os.path.join(d, 'status.json')
        script_status_path = os.path.join(d, 'script_status.json')
        result_path_job = os.path.join(d, _canonical_result_file('qr'))      # prefer job-local uuid.json
        result_path_cwd = os.path.join(os.getcwd(), _canonical_result_file('qr'))  # fallback to CWD
        start_ts = time.time()
        deadline = start_ts + timeout_sec
        published_detected = False
        while time.time() < deadline:
            # If the script already finalized, stop.
            try:
                if os.path.exists(script_status_path):
                    with open(script_status_path, 'r', encoding='utf-8') as f:
                        s = json.load(f) or {}
                        if s.get('phase') in ('done', 'error'):
                            return
                        if s.get('phase') == 'detected':
                            published_detected = True
            except Exception:
                pass
            # Prefer job-local result; fall back to CWD
            candidate = result_path_job if os.path.exists(result_path_job) else (result_path_cwd if os.path.exists(result_path_cwd) else None)
            if candidate:
                try:
                    mtime = os.path.getmtime(candidate)
                    if mtime < start_ts:
                        # stale file from previous run; ignore until it is updated
                        raise RuntimeError('stale uuid.json (mtime < start_ts)')
                    # basic stability check: wait a short moment to avoid partial writes
                    size1 = os.path.getsize(candidate)
                    time.sleep(0.1)
                    size2 = os.path.getsize(candidate)
                    if size2 != size1:
                        # still being written; skip this iteration
                        raise RuntimeError('uuid.json still growing')

                    detected = ''
                    with open(candidate, 'r', encoding='utf-8') as f:
                        out = json.load(f) or {}
                        detected = (out.get('uuid') or '').strip()
                    expected = ''
                    try:
                        ref_job = os.path.join(d, 'reference_uuid.json')
                        if os.path.exists(ref_job):
                            with open(ref_job, 'r', encoding='utf-8') as rf:
                                ref = json.load(rf) or {}
                                expected = (ref.get('uuid') or '').strip()
                        elif os.path.exists('reference_uuid.json'):
                            with open('reference_uuid.json', 'r', encoding='utf-8') as rf:
                                ref = json.load(rf) or {}
                                expected = (ref.get('uuid') or '').strip()
                    except Exception:
                        expected = ''
                    verified = bool(expected) and bool(detected) and (expected == detected)
                    # If the script already marked DONE, we'll finalize immediately. Otherwise, proceed with our own finalize.
                    try:
                        if os.path.exists(script_status_path):
                            with open(script_status_path, 'r', encoding='utf-8') as f:
                                s2 = json.load(f) or {}
                                if s2.get('phase') == 'done':
                                    published_detected = True
                    except Exception:
                        pass
                    # write detected first (if not yet)
                    if not published_detected:
                        _write_json(status_path, {"phase": "detected", "mode": "qr"})
                        published_detected = True
                    # then mark done with data
                    cur = {}
                    try:
                        if os.path.exists(status_path):
                            with open(status_path, 'r', encoding='utf-8') as f:
                                cur = json.load(f) or {}
                    except Exception:
                        cur = {}
                    cur['phase'] = 'done'
                    cur['mode'] = 'qr'
                    cur.setdefault('data', {})
                    ts_iso = datetime.utcfromtimestamp(mtime).isoformat() + "Z"
                    payload = {
                        'uuid': detected,
                        'expected_uuid': expected,
                        'verified': verified,
                        'timestamp': ts_iso
                    }
                    cur['data'].update(_filter_result_fields('qr', payload))
                    _write_json(status_path, cur)
                    return
                except Exception:
                    pass
            time.sleep(0.5)
        # timeout: if not finalized, mark error
        try:
            cur = {}
            if os.path.exists(status_path):
                with open(status_path, 'r', encoding='utf-8') as f:
                    cur = json.load(f) or {}
            if cur.get('phase') not in ('done', 'error'):
                cur['phase'] = 'error'
                cur['mode'] = 'qr'
                cur['message'] = 'Timeout waiting for uuid.json'
                _write_json(status_path, cur)
        except Exception:
            pass
    t = threading.Thread(target=_watch, daemon=True)
    t.start()

def _read_job_status(job_id: str) -> Dict[str, Any]:
    d = _job_dir(job_id)
    status_path = os.path.join(d, "status.json")
    script_status_path = os.path.join(d, "script_status.json")
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
    # merge only the canonical result json for this job's mode
    try:
        mode = status.get('mode') or request.args.get('mode') or 'qr'
        result_path_job = os.path.join(d, _canonical_result_file(mode))
        result_path_cwd = os.path.join(os.getcwd(), _canonical_result_file(mode))
        chosen = result_path_job if os.path.exists(result_path_job) else (result_path_cwd if os.path.exists(result_path_cwd) else None)
        if chosen:
            with open(chosen, "r", encoding="utf-8") as f:
                r = json.load(f) or {}
                if isinstance(r, dict):
                    status.setdefault('data', {})
                    status['data'].update(_filter_result_fields(mode, r))
    except Exception:
        pass

    # QR fallback: if we have uuid but missing expected/verified, compute them here (JOB_DIR first)
    try:
        mode_eff = status.get('mode') or request.args.get('mode') or 'qr'
        if mode_eff == 'qr' and isinstance(status.get('data'), dict):
            data = status['data']
            has_uuid = isinstance(data.get('uuid'), str) and len(data.get('uuid')) > 0
            needs_expected = 'expected_uuid' not in data
            needs_verified = 'verified' not in data
            if has_uuid and (needs_expected or needs_verified):
                expected = ''
                ref_job = os.path.join(d, 'reference_uuid.json')
                if os.path.exists(ref_job):
                    try:
                        with open(ref_job, 'r', encoding='utf-8') as rf:
                            ref = json.load(rf) or {}
                            expected = (ref.get('uuid') or '').strip()
                    except Exception:
                        expected = ''
                elif os.path.exists('reference_uuid.json'):
                    try:
                        with open('reference_uuid.json', 'r', encoding='utf-8') as rf:
                            ref = json.load(rf) or {}
                            expected = (ref.get('uuid') or '').strip()
                    except Exception:
                        expected = ''
                if needs_expected:
                    data['expected_uuid'] = expected
                if needs_verified:
                    data['verified'] = bool(expected) and (expected == data.get('uuid'))
                if 'timestamp' not in data:
                    # infer timestamp from the job-local uuid.json mtime if available
                    try:
                        rp_job = os.path.join(d, _canonical_result_file('qr'))
                        if os.path.exists(rp_job):
                            mt = os.path.getmtime(rp_job)
                            data['timestamp'] = datetime.utcfromtimestamp(mt).isoformat() + 'Z'
                    except Exception:
                        pass
                # sanitize after adding
                status['data'] = _filter_result_fields('qr', data)
    except Exception:
        pass

    # sanitize any pre-existing data fields to the allowed set for this mode
    try:
        mode = status.get('mode') or request.args.get('mode') or 'qr'
        if isinstance(status.get('data'), dict):
            status['data'] = _filter_result_fields(mode, status['data'])
    except Exception:
        pass
    # Auto-register newly verified gesture/object into registry (idempotent)
    try:
        data = status.get('data') or {}
        verified = bool(data.get('verified')) or (status.get('phase') == 'done' and data)
        mode = status.get('mode') or ''
        source_script = (status.get('script') or '').strip() or None
        if verified and mode in ('gesture', 'object'):
            name_key = 'gesture' if mode == 'gesture' else 'object'
            display_name = data.get(name_key)
            if display_name:
                reg = _load_registry()
                already = reg.get(mode, {}).get(display_name)
                if not already:
                    target = _register_runner(mode, display_name, source_script)
                    if target:
                        status.setdefault('registry', {})
                        status['registry'][name_key] = os.path.basename(target)
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
        env = os.environ.copy()
        env['JOB_ID'] = job_id
        env['JOB_DIR'] = d
        proc = subprocess.Popen([sys.executable, "-u", script_path],
                                 stdout=proc_out, stderr=subprocess.STDOUT, env=env)
        _write_json(status_path, {"phase": "running", "session_id": sid or session_id, "pid": proc.pid, "script": script_path, "mode": mode})
    except Exception as e:
        _write_json(status_path, {"phase": "error", "message": f"Failed to start script: {e}"})

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/qr-code/<filename>')
def serve_qr_code(filename):
    """Serve QR code image file."""
    # Only allow files in the current working directory with safe extensions
    safe_exts = {'.png', '.jpg', '.jpeg'}
    name = os.path.basename(filename)
    if name != filename:
        return "Invalid filename", 400
    ext = os.path.splitext(name)[1].lower()
    if ext not in safe_exts:
        return "Unsupported file type", 400
    path = os.path.join(os.getcwd(), name)
    if not os.path.exists(path):
        return "QR code not found", 404
    return send_file(path)

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
- Create (or overwrite) a file named reference_uuid.json in the current working directory with JSON content: {"uuid": "<GENERATED_UUID>", "timestamp": "<ISO8601 UTC>"}.
- Additionally, IF the environment variable JOB_DIR is present, also write the same JSON to os.path.join(os.environ["JOB_DIR"], "reference_uuid.json") so that a per-job copy exists.
- Create (or overwrite) a QR code image file named qr_image.jpeg (JPEG) that encodes exactly the same <GENERATED_UUID> string. IF JOB_DIR is present, optionally also copy/save the same image under os.path.join(os.environ["JOB_DIR"], "qr_image.jpeg").
- Use Python. If libraries are missing, install them programmatically (e.g., qrcode[pil], pillow). Avoid reinstalling if already present.
- On completion, print a single line to stdout in the exact format: qr_ready <GENERATED_UUID>

Finally, run the prepared Python script (or the reused script) to produce the outputs.

ultrathink"""
        result, claude_session_id = run_claude(prompt)

        # After Claude runs, enumerate QR images and read the reference UUID if present
        qr_files = _list_recent_qr_images()
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


# --- Fast path: /reuse (no Claude, just reuse existing runner scripts) ---
@app.route('/instant-run', methods=['POST'])
def instant_run():
    """Fast path that reuses existing code without Claude.
    Expects JSON: {
      mode: 'qr'|'gesture'|'object',
      action: 'generate'|'validate',
      displayName?: str,   # human-friendly name of preset, e.g., 'thumb up'
      scriptName?: str,    # exact python filename, e.g., 'thumb_up_gesture.py'
      prompt?: str
    }
    """

    body = request.get_json(silent=True) or {}
    mode = body.get('mode', 'qr')
    action = body.get('action', 'generate')  # 'generate' or 'validate'

    if action == 'generate':
        display_name = (body.get('displayName') or '').strip() or None
        script_name = (body.get('scriptName') or '').strip() or None
        script_path = os.path.join(os.getcwd(), script_name) if script_name else None

        if mode == 'qr':
            # For QR: run qr_generator.py if provided and exists; otherwise, generate UUID+QR here.
            try:
                uid = None
                # Execute the generator script; expect it to create reference_uuid.json and qr_image.jpeg
                proc = subprocess.run([sys.executable, script_path], capture_output=True, text=True)
                # Best-effort read of reference_uuid.json
                try:
                    if os.path.exists('reference_uuid.json'):
                        with open('reference_uuid.json', 'r', encoding='utf-8') as f:
                            ref = json.load(f) or {}
                            uid = ref.get('uuid')
                except Exception:
                    uid = None
                if proc.returncode != 0 and not uid:
                    # Fallback to in-process generation if script failed
                    raise RuntimeError(proc.stderr or 'qr_generator failed')
                return jsonify({
                    'status': 'ok',
                    'kind': 'qr',
                    'data': {'qr_codes': _list_recent_qr_images(), 'script_name': "qr_runner.py"},
                    'stream_url': '/video_feed'
                }), 200
            except Exception as e:
                return jsonify({'status': 'error', 'message': f'QR instant-run generate failed: {e}'}), 500

        # Non-QR (gesture/object): just return live stream info
        return jsonify({
            'status': 'ok',
            'kind': 'live',
            'data': {'script_name': script_name},
            'stream_url': '/video_feed'
        }), 200
    else:
        display_name = (body.get('displayName') or '').strip() or None
        script_name = (body.get('scriptName') or '').strip() or None
        script_path = os.path.join(os.getcwd(), script_name) if script_name else None

        if mode == 'qr':
            job_id = str(uuid.uuid4())
            d = _job_dir(job_id)
            _write_json(os.path.join(d, 'status.json'), {
                'phase': 'running',
                'script': script_path,
                'mode': mode,
                'displayName': display_name,
                'scriptName': os.path.basename(script_path) if script_path else None
            })
            # Touch READY immediately so UI can switch; the runner should later write detected/done
            _write_json(os.path.join(d, 'script_status.json'), {'phase': 'ready'})
            # Ensure reference_uuid.json is available in JOB_DIR for consistent comparison
            try:
                src_ref = 'reference_uuid.json'
                dst_ref = os.path.join(d, 'reference_uuid.json')
                if os.path.exists(src_ref):
                    shutil.copyfile(src_ref, dst_ref)
            except Exception:
                pass
            # start asynchronous watcher that will compare uuid.json vs reference_uuid.json
            _start_qr_watcher(job_id, timeout_sec=30)
            try:
                proc_log_path = os.path.join(d, 'script.log')
                proc_out = open(proc_log_path, 'w', encoding='utf-8')
                env = os.environ.copy()
                env['JOB_ID'] = job_id
                env['JOB_DIR'] = d
                subprocess.Popen([sys.executable, "-u", script_path],
                                 stdout=proc_out, stderr=subprocess.STDOUT, env=env)
            except Exception as e:
                return jsonify({
                    'status': 'error',
                    'kind': 'qr-validate',
                    'message': f'Failed to start qr_runner: {e}'
                }), 500

            return jsonify({ 'status': 'accepted', 'job_id': job_id, 'poll_url': f'/job-status?job_id={job_id}' }), 202

        # gesture/object: async run of the provided runner script
        job_id = str(uuid.uuid4())
        d = _job_dir(job_id)
        _write_json(os.path.join(d, 'status.json'), {
            'phase': 'running',
            'script': script_path,
            'mode': mode,
            'displayName': display_name,
            'scriptName': os.path.basename(script_path) if script_path else None
        })
        # Touch READY immediately so UI can switch; the runner should later write detected/done
        _write_json(os.path.join(d, 'script_status.json'), {'phase': 'ready'})

        try:
            if not os.path.exists(script_path):
                raise FileNotFoundError(f"Runner not found: {os.path.basename(script_path)}")
            proc_log_path = os.path.join(d, 'script.log')
            proc_out = open(proc_log_path, 'w', encoding='utf-8')
            env = os.environ.copy()
            env['JOB_ID'] = job_id
            env['JOB_DIR'] = d
            proc = subprocess.Popen([sys.executable, "-u", script_path],
                                    stdout=proc_out, stderr=subprocess.STDOUT, env=env)
            return jsonify({ 'status': 'accepted', 'job_id': job_id, 'poll_url': f'/job-status?job_id={job_id}' }), 202
        except Exception as e:
            _write_json(os.path.join(d, 'status.json'), {'phase': 'error', 'message': f'Failed to start runner: {e}'})
            return jsonify({'status': 'error', 'message': str(e)}), 500

    
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

    # Get user prompt from JSON body
    user_prompt = body.get('prompt', '')

    script_name_map = {'qr': 'qr_runner.py', 'gesture': 'gesture_runner.py', 'object': 'object_runner.py'}
    script_path = os.path.join(os.getcwd(), script_name_map.get(mode, 'runner.py'))

    if mode == 'qr':
        prompt = f"""
\nWrite (or reuse) a Python script saved exactly as: {script_path}
that captures a livestream from the device's webcam, scans its frames in real time, and detects the most probable QR code that appears in the stream.

Before writing new code:
- Search the current project directory for existing QR detection scripts or utilities (files whose names or contents mention 'qr', 'detect', 'zbar', 'pyzbar', 'opencv', etc.). Prefer reusing and minimally editing an existing script over creating a new one.
- Only create a new file if no suitable script exists. If modifying, keep the filename exactly as above.

Requirements:
- When a QR is detected, print a single line to stdout in the exact format: qr_code <QR_CODE_CONTENT>
- Save a JSON file named uuid.json into os.path.join(os.environ.get("JOB_DIR","."), "uuid.json") with JSON content: {{\"uuid\": <QR_CODE_CONTENT>, \"timestamp\": \"<ISO8601 UTC>\"}}
- Exit non-zero with a clear message on failure.
- The script should stop after successful detection or after ~15 seconds.
- Use Python. If dependencies (e.g., opencv-python, pyzbar, numpy, pillow) are missing, install them programmatically; skip install if already available.

IMPORTANT: Do not run the script yourself; only write/update the file at the exact path above.

ultrathink"""
    elif mode == 'gesture':
        prompt = (user_prompt or "") + f"""
\nWrite (or reuse) a Python script saved exactly as: {script_path}
that uses MediaPipe to detect the user-specified gesture from the device's webcam in real time.

Before writing new code:
- Search the current project directory for existing gesture/hand detection scripts (files mentioning 'mediapipe', 'hands', 'gesture', 'thumb', etc.). Prefer reusing and minimally editing an existing script over creating a new one.
- Only create a new file if no suitable script exists. If modifying, keep the filename exactly as above.

The script must:
- Depend on mediapipe and opencv-python; install only if missing.
- Open the default webcam and process frames continuously.
- Implement the gesture logic based on the user's prompt (e.g., hand landmarks configuration). Make the condition parametrizable so it can adapt to different gestures; avoid hardcoding a specific gesture name.
- When the specified gesture is detected, print exactly one line: gesture <GESTURE_NAME>
- Save a JSON file named gesture_output.json containing at least: {{\"gesture\": \"<GESTURE_NAME>\", \"verified\": true, \"confidence\": <float between 0 and 1>, \"timestamp\": \"<ISO8601 UTC>\"}} into os.path.join(os.environ.get("JOB_DIR","."), "gesture_output.json")
- Continue running for up to 20 seconds or until detection occurs; then exit.
- Be resilient to missing webcam / install errors by printing a clear error and exiting non-zero.

IMPORTANT: Do not run the script yourself; only write/update the file at the exact path above.

ultrathink"""
        print(prompt)
    elif mode == 'object':
        prompt = (user_prompt or "") + f"""
\nWrite (or reuse) a Python script saved exactly as: {script_path}
that uses YOLOv8 (ultralytics) to detect the user-specified object from the device's webcam in real time.

Before writing new code:
- Search the current project directory for existing object detection scripts (files mentioning 'yolo', 'ultralytics', 'detect', etc.). Prefer reusing and minimally editing an existing script over creating a new one.
- Only create a new file if no suitable script exists. If modifying, keep the filename exactly as above.

The script must:
- Depend on ultralytics and opencv-python; install only if missing.
- Load a lightweight pretrained YOLOv8 model (e.g., yolov8n.pt). If absent, download via ultralytics.
- Open the default webcam and run inference on frames in real time.
- Track the highest-confidence class observed over a short sliding window (e.g., ~30 frames) and implement class matching per the user prompt.
- When confidence for a class exceeds 0.6 for at least 3 frames and matches the user-specified target, print exactly one line: object <CLASS_NAME>
- Save a JSON file named object_output.json containing at least: {{\"object\": \"<CLASS_NAME>\", \"verified\": true, \"confidence\": <float between 0 and 1>, \"timestamp\": \"<ISO8601 UTC>\"}} into os.path.join(os.environ.get("JOB_DIR","."), "object_output.json")
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
- Environment variables provided by the server:
  * JOB_ID  → current job id (string)
  * JOB_DIR → absolute path to jobs/<job_id>
  * TARGET_NAME → optional target to detect (e.g., 'thumb up', 'STOP', 'apple'). Prefer this over parsing prompt.
- The script MUST write phase updates to os.path.join(os.environ.get('JOB_DIR', '.'), 'script_status.json') as UTF-8 JSON:
  * READY  → as soon as webcam opens and the main loop is about to start:
      - print("PHASE READY", flush=True)
      - write {{"phase":"ready"}}
  * DETECTED → on successful detection, write one of:
      - gesture: {{"phase":"detected","gesture":"<NAME>","confidence": <0..1>, "timestamp":"<ISO8601Z>"}}
      - object : {{"phase":"detected","object":"<CLASS>","confidence": <0..1>, "timestamp":"<ISO8601Z>"}}
      - qr     : {{"phase":"detected","uuid":"<VALUE>", "timestamp":"<ISO8601Z>"}}
    And also print exactly one line to stdout (e.g., "gesture <NAME>", "object <CLASS>", "qr_code <VALUE>") with flush=True.
  * DONE / ERROR → on normal finish write {{"phase":"done","verified": <true|false>}}; on fatal error write {{"phase":"error","message":"..."}} and exit non-zero.
- Canonical outputs (write under JOB_DIR so the server can always pick them up per job):
  - gesture → os.path.join(JOB_DIR, 'gesture_output.json')   e.g., {{"gesture":"<NAME>","verified":true,"confidence":0.xx,"timestamp":"<ISO8601Z>"}}
  - object  → os.path.join(JOB_DIR, 'object_output.json')    e.g., {{"object":"<CLASS>","verified":true,"confidence":0.xx,"timestamp":"<ISO8601Z>"}}
  - qr      → os.path.join(JOB_DIR, 'uuid.json')             e.g., {{"uuid":"<VALUE>","timestamp":"<ISO8601Z>"}}
- Parameterization for reuse:
  - Determine the target to detect in this order:
      1) os.environ.get("TARGET_NAME")      # highest priority
      2) A JSON file at os.path.join(os.environ.get("JOB_DIR","."), "target.json")
         with {{"gesture":"..."}} or {{"object":"..."}}
      3) Fallback to parsing from the prompt text
  - Do NOT hardcode paths or the target; make the detection logic reusable.
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
def recognize_qr_image():
    # Deprecated alias → forwards to /validate (qr mode) using POST
    with app.test_request_context('/validate?mode=qr', method='POST', json={}):
        return validate()

@app.route('/job-status', methods=['GET'])
def job_status():
    job_id = request.args.get('job_id')
    if not job_id:
        return jsonify({"error": "job_id is required"}), 400
    s = _read_job_status(job_id)
    return jsonify(s)

@app.route('/presets', methods=['GET'])
def presets():
    reg = _load_registry()
    return jsonify(reg)


def run_claude(prompt: str, cwd: str = ".", session_id=None):
    """
    Run Claude Code CLI with a given prompt.

    Returns a tuple: (stdout_text, session_id_str or None)
    """
    exe = shutil.which("claude")
    if not exe:
        return "Error: Claude CLI not found. Please install the 'claude' CLI or adjust configuration.", None
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
