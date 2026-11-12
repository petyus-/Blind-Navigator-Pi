#!/usr/bin/env python3
"""Blind Navigator v2 — continuous CPU-only object detection (Pi5).

YOLOv8n via ultralytics (CPU), ~1 fps (detect_fps in /etc/blindnav.conf).
Speaks Hungarian announcements ONLY when:
  - a new relevant object appears, or
  - an object's distance class changes (near/mid/far from bbox height fraction).
Same object max once per 30s (unless its distance class changed).

Runs silently alongside blindnav.py (LLM button features) — separate process.

Test mode (no camera): python3 detect.py --test /path/to.jpg
"""
import os
import sys
import time

# ------------------------- config -------------------------
CONF_PATH = "/etc/blindnav.conf"
DEFAULTS = {"DETECT_FPS": "1", "CONF": "0.45", "IMGZ": "320"}
# COCO -> Hungarian (only common, useful classes; unmapped detections skipped)
HU = {
    "person": "ember", "bicycle": "kerékpár", "car": "autó", "motorcycle":
    "motor", "bus": "busz", "truck": "teherautó", "traffic light":
    "közlekedési lámpa", "stop sign": "stoptábla", "bench": "pad", "bird":
    "madár", "cat": "macska", "dog": "kutya", "horse": "ló", "backpack":
    "hátizsák", "umbrella": "esernyő", "handbag": "táska", "suitcase":
    "bőrönd", "bottle": "palack", "cup": "pohár", "bowl": "tál", "banana":
    "banán", "apple": "alma", "chair": "szék", "couch": "kanapé", "bed":
    "ágy", "dining table": "asztal", "toilet": "wc", "tv": "tévé", "laptop":
    "laptop", "mouse": "egér", "keyboard": "billentyűzet", "cell phone":
    "telefon", "book": "könyv", "clock": "óra", "scissors": "olló", "door":
    "ajtó",
}
DIST_FAR, DIST_MID, DIST_NEAR = 0, 1, 2
DIST_HU = {DIST_NEAR: "közvetlenül előttem",
           DIST_MID: "előttem kb. 2-3 méterre",
           DIST_FAR: "távolabb"}
COOLDOWN_S = 30.0


def read_conf():
    cfg = dict(DEFAULTS)
    try:
        with open(CONF_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() in cfg:
                    cfg[k.strip()] = v.strip()
    except OSError:
        pass
    return cfg


def distance_class(bbox_h_frac: float) -> int:
    if bbox_h_frac > 0.55:
        return DIST_NEAR
    if bbox_h_frac > 0.25:
        return DIST_MID
    return DIST_FAR


def load_camera_source():
    """Auto-detect camera like blindnav.py. Returns ffmpeg read command or None."""
    import subprocess
    if subprocess.run(["which", "rpicam-still"], capture_output=True).returncode == 0:
        return None  # handled by libcamera via picamera2/OpenCV index fallback
    for i in range(4):
        if os.path.exists(f"/dev/video{i}"):
            return i
    return None


def open_capture():
    import cv2
    src = load_camera_source()
    if src is None:
        cap = cv2.VideoCapture(0)
    else:
        cap = cv2.VideoCapture(src)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
    return cap


def main_test(photo):
    cfg = read_conf()
    from ultralytics import YOLO
    model = YOLO("yolov8n.pt")
    fps0 = time.time()
    results = model(photo, imgsz=int(cfg["IMGZ"]), conf=float(cfg["CONF"]),
                    verbose=False)
    dt = time.time() - fps0
    print("inference time: %.2fs (~%.1f fps on this CPU; Pi5 CPU expect ~1-2 fps)"
          % (dt, 1.0 / dt))
    for r in results:
        fh = r.orig_shape[0]
        for box in r.boxes:
            name = model.names[int(box.cls)]
            if name not in HU:
                continue
            frac = float((box.xyxy[0][3] - box.xyxy[0][1])) / fh
            print("ANNOUNCE: %s %s" % (HU[name], DIST_HU[distance_class(frac)]))
            print("  (eng=%s conf=%.2f bbox_h_frac=%.2f)" %
                  (name, float(box.conf), frac))


def main_loop():
    cfg = read_conf()
    interval = 1.0 / float(cfg["DETECT_FPS"])
    from ultralytics import YOLO
    import cv2
    import blindnav  # reuse speak_text_via_server for TTS
    model = YOLO("yolov8n.pt")
    cap = open_capture()
    last = {}   # name -> (dist_class, spoken_time)
    print("detect loop ready (fps=%s, conf=%s, imgsz=%s)" %
          (cfg["DETECT_FPS"], cfg["CONF"], cfg["IMGZ"]), flush=True)
    while True:
        ok, frame = cap.read()
        if not ok:
            time.sleep(1)
            cap = open_capture()
            continue
        res = model(frame, imgsz=int(cfg["IMGZ"]), conf=float(cfg["CONF"]),
                    verbose=False)[0]
        fh = res.orig_shape[0]
        seen = {}
        for box in res.boxes:
            name = model.names[int(box.cls)]
            if name in HU:
                frac = float(box.xyxy[0][3] - box.xyxy[0][1]) / fh
                seen[name] = distance_class(frac)
        now = time.time()
        for name, dist in seen.items():
            prev = last.get(name)
            if prev and prev[0] == dist and now - prev[1] < COOLDOWN_S:
                continue
            last[name] = (dist, now)
            text = "%s %s" % (HU[name], DIST_HU[dist])
            print("ANNOUNCE: %s" % text, flush=True)
            try:
                import blindnav
                ok, jpg = cv2.imencode(".jpg", frame)
                if ok:
                    blindnav.log_bytes(jpg.tobytes(), "detect_frame", "jpg")
                blindnav.log_event("DETECT", text)
                blindnav.speak_text_via_server(text)
            except Exception as e:
                print("tts error: %s" % e, flush=True)
        for name in list(last):
            if name not in seen:
                del last[name]
        time.sleep(interval)


if __name__ == "__main__":
    if "--test" in sys.argv:
        main_test(sys.argv[sys.argv.index("--test") + 1])
    else:
        main_loop()
