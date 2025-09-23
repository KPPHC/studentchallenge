import os
import subprocess
import base64
from flask import Flask, request, send_file, render_template, jsonify
from pathlib import Path

app = Flask(__name__)

HOST = "0.0.0.0"
PORT = 8080
VIDEO_PATH = "output.mp4"  # Change this to your video filename

# Video should be removed later on
@app.route('/')
def index():
    """Serve the main page with video player."""
    if not os.path.exists(VIDEO_PATH):
        return "<h1>Video not found</h1>", 404
    
    return render_template('index.html', video_filename=os.path.basename(VIDEO_PATH))

@app.route('/video')
def video():
    if not os.path.exists(VIDEO_PATH):
        return "Video not found", 404
    return send_file(VIDEO_PATH, mimetype='video/mp4')

@app.route('/qr-code/<filename>')
def serve_qr_code(filename):
    """Serve QR code image file."""
    if not os.path.exists(filename):
        return "QR code not found", 404
    return send_file(filename, mimetype='image/png')

@app.route('/run-claude', methods=['POST'])
def run_claude_endpoint():
    
    prompt = request.get_data(as_text=True)
    
    print(f"\n{'='*50}")
    print(f"POST request received at /run-claude")
    print(f"Prompt: {prompt}")
    print(f"{'='*50}\n")

    if not prompt:
        return jsonify({"error": "No prompt provided"}), 400
    
    result = run_claude(prompt)

    # Check for QR code files in the current directory
    qr_files = []
    for file in Path('.').glob('*.jpg'):
        # Check if file might be a QR code (you can adjust this logic)
        if 'qr' in file.name.lower() or 'code' in file.name.lower():
            qr_files.append(file.name)
    
    # If no QR files found by name, check for any new PNG files
    if not qr_files:
        # Get all PNG files modified recently (within last 5 seconds)
        import time
        current_time = time.time()
        for file in Path('.').glob('*.jpg'):
            if current_time - os.path.getmtime(file) < 5:
                qr_files.append(file.name)
    
    # Also needs to have the UUID
    response_data = {
        "output": result,
        "qr_codes": qr_files
    }

    print(f"Response data: {response_data}")
    
    return jsonify(response_data)

def run_claude(prompt: str, cwd: str = ".") -> str:
    """
    Run Claude Code CLI with a given prompt.
    
    Args:
        prompt: The text to send to Claude Code.
        cwd: Directory where the codebase is (Claude runs in that context).
    
    Returns:
        Claude's output as a string.
    """
    try:
        result = subprocess.run(
            [r"C:\Users\post97\AppData\Roaming\npm\claude.cmd", prompt],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        return f"Error: {e.stderr}"

if __name__ == "__main__":
    print(f"Open http://{HOST}:{PORT}/ to view the video (use your LAN IP on other devices).")
    print(f"Make sure '{VIDEO_PATH}' exists in this directory.")
    app.run(host=HOST, port=PORT, debug=False)