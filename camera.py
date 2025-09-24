# app.py (simplified)
from flask import Flask, Response
import cv2, platform, time

app = Flask(__name__)

def open_capture(index=0):
    system = platform.system().lower()
    fallback = {
        "windows": cv2.CAP_DSHOW,        # or cv2.CAP_MSMF
        "darwin":  cv2.CAP_AVFOUNDATION,
        "linux":   cv2.CAP_V4L2,
    }.get(system, None)

    cap = cv2.VideoCapture(index)
    if cap.isOpened():
        return cap
    cap.release()

    if fallback is not None:
        cap = cv2.VideoCapture(index, fallback)
        if cap.isOpened():
            return cap
        cap.release()

    raise RuntimeError(f"Could not open webcam at index {index} on {system}")

cap = open_capture(0)

@app.route("/")
def home():
    # Minimal HTML that embeds the MJPEG stream
    return """<!doctype html><meta charset="utf-8">
    <title>Webcam</title>
    <body style="margin:2rem;font-family:system-ui">
      <h1>Server Webcam</h1>
      <img src="/video" style="max-width:100%;border-radius:12px">
      <p>Press Ctrl+C to stop.</p>
    </body>"""

def frames():
    while True:
        ok, frame = cap.read()
        if not ok: 
            time.sleep(0.01); continue
        ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok: 
            continue
        yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" +
               jpg.tobytes() + b"\r\n")

def register_camera_routes(app):
    @app.route("/video_feed")
    def video_feed():
        return Response(frames(), mimetype="multipart/x-mixed-replace; boundary=frame")

if __name__ == "__main__":
    try:
        app.run(host="0.0.0.0", port=8080, threaded=True)
    finally:
        cap.release()
