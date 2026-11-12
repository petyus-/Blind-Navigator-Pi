#!/usr/bin/env python3
"""Blind Navigator v2.5 — Pi5 walking aid (outdoor street navigation).

SHORT button press  -> scene description (spoken, Hungarian)
LONG press (>=2s)   -> navigation guidance (direction to go)
VOICE (USB mic)     -> speak a command (Hungarian) -> STT -> intent routing:
                        "járd..."  -> sidewalk navigation guidance
                        "hol van"/"merre" -> scene description
                        "menj"/"indul" -> navigation guidance
                        unknown -> "Nem értem, ismételd"
Failure             -> "Sajnos most nem tudom ellenőrizni", loop continues.

Backend: kover blindbox-server POST /ask (multipart: question, photo, lang=hu)
  -> returns audio/mpeg spoken answer (Gemini vision + TTS in one call).
STT: whisper-openai-adapter on kover :8012 (language pinned to hu).

DEBUG LOGGING (LOG=1 in /etc/blindnav.conf): every analyzed camera frame
(.jpg), every spoken audio (.mp3/.wav) and every voice command transcript
are saved under /var/lib/blindnav/log/YYYY-MM-DD/ with a per-day events.log.
A cron uploads the day folder to kover every 15 min; logrotate keeps 7 days.

Test mode (no camera/GPIO):  python3 blindnav.py --server-only --photo /path/to.jpg
                             python3 blindnav.py --voice-test /path/to.wav
"""
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

# ------------------------- config -------------------------
SERVER_IP = os.environ.get("BLINDNAV_SERVER", "100.119.157.3")  # kover Tailscale IP
PORT = 5014
LANG = "hu"
BUTTON_BCM = 17              # BCM 17, internal pull-up, active-low (to GND)
LONG_PRESS_S = 2.0
DEBOUNCE_S = 0.05
REQUEST_TIMEOUT = 90         # gemini vision + tts can take ~10-30s
STT_TIMEOUT = 120            # whisper small/int8 CPU: ~19s for a short clip

CONF_PATH = "/etc/blindnav.conf"
DEFAULTS = {
    "LOG": "1", "LOG_DIR": "/var/lib/blindnav/log",
    "VOICE": "1", "REC_SECONDS": "4",
    "STT_URL": "http://100.119.157.3:8012/v1/audio/transcriptions",
    "STT_LANG": "hu",
}
CFG = dict(DEFAULTS)


def read_conf():
    try:
        with open(CONF_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() in CFG:
                    CFG[k.strip()] = v.strip()
    except OSError:
        pass
    return CFG


read_conf()

Q_SCENE = ("Ez a vak ember kamerájának képe. Írd le röviden, 2-3 mondatban, "
           "magyarul: mi van előtte?")
Q_NAV = ("Ez a vak ember kamerájának képe. Mondd röviden magyarul: mi van előtte, "
         "és merre menjen — fordulj balra, fordulj jobbra, vagy menjen egyenesen?")
Q_SIDEWALK = (
    "Egy vak idős embernek segítesz a járdán közlekedni. Röviden, max 2 mondat, "
    "veszélyt először. Ez a kamerakép: folytatódik-e a járda és merre? "
    "Van-e akadály előtte (gödör, kerékpár, kuka, autó, lépcső)? Közel van-e az út? "
    "Mit tegyen: menjen egyenesen, forduljon balra vagy jobbra?")
FALLBACK = "Sajnos most nem tudom ellenőrizni"
NOT_UNDERSTOOD = "Nem értem, ismételd"

VOICE_UNKNOWN = ("unknown", "scene", "nav", "sidewalk")
# ------------------------- logging -------------------------


def _day_dir():
    d = os.path.join(CFG["LOG_DIR"], time.strftime("%Y-%m-%d"))
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        return None


def log_event(etype, text=""):
    """Append one line to the per-day events.log. Never raises."""
    if CFG.get("LOG") != "1":
        return
    d = _day_dir()
    if not d:
        return
    try:
        with open(os.path.join(d, "events.log"), "a") as f:
            f.write("%s\t%s\t%s\n" % (time.strftime("%H:%M:%S"), etype, text))
    except OSError:
        pass


def log_bytes(data: bytes, kind: str, ext: str) -> str:
    """Save a frame (.jpg) or audio (.mp3/.wav) into the day dir. Returns path."""
    if CFG.get("LOG") != "1":
        return ""
    d = _day_dir()
    if not d:
        return ""
    path = os.path.join(d, "%s_%s.%s" % (time.strftime("%H%M%S"), kind, ext))
    try:
        with open(path, "wb") as f:
            f.write(data)
        return path
    except OSError:
        return ""


# ------------------------- audio I/O -------------------------

PLAY_CHAIN = ["mpv", "paplay", "aplay"]


def capture_frame() -> bytes:
    """Auto-detect rpicam-still (CSI) vs ffmpeg v4l2 (USB). Returns JPEG bytes."""
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        path = f.name
    try:
        r = subprocess.run(["rpicam-still", "-n", "-o", path, "--width", "1280",
                            "--height", "720", "--quality", "85", "-t", "2000"],
                           capture_output=True, timeout=20)
        if r.returncode == 0 and os.path.getsize(path) > 5000:
            return open(path, "rb").read()
        # USB webcam fallback: probe /dev/video0..3
        for dev in [f"/dev/video{i}" for i in range(4)]:
            if not os.path.exists(dev):
                continue
            r = subprocess.run(
                ["ffmpeg", "-y", "-f", "v4l2", "-i", dev, "-frames:v", "1",
                 "-q:v", "4", path],
                capture_output=True, timeout=20)
            if r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 5000:
                return open(path, "rb").read()
        raise RuntimeError("no camera captured a frame (rpicam-still + v4l2 failed)")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def ask(question: str, jpeg: bytes) -> bytes:
    """POST question+photo to blindbox /ask -> mp3 bytes."""
    boundary = "----blindnav%s" % int(time.time() * 1000)
    parts = []
    for name, value in (("question", question), ("lang", LANG)):
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"'
                      '\r\n\r\n%s\r\n' % (boundary, name, value)).encode())
    parts.append(('--%s\r\nContent-Disposition: form-data; name="photo"; '
                  'filename="frame.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'
                  % boundary).encode() + jpeg + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        f"http://{SERVER_IP}:{PORT}/ask", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        if resp.status != 200:
            raise RuntimeError("server status %s" % resp.status)
        return resp.read()


def play_mp3(mp3: bytes, log_kind: str = "answer"):
    if log_kind:
        log_bytes(mp3, log_kind, "mp3")
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(mp3)
        path = f.name
    for player in (["mpv", "--really-quiet", "--no-video"],
                   ["ffplay", "-autoexit", "-nodisp", "-loglevel", "quiet"],
                   ["paplay"], ["aplay"]):
        try:
            if subprocess.run(player + [path], capture_output=True,
                              timeout=120).returncode == 0:
                break
        except Exception:
            continue
    try:
        os.unlink(path)
    except OSError:
        pass


def speak(mp3: bytes, log_kind: str = "answer"):
    play_mp3(mp3, log_kind)


def speak_text_via_server(text: str, log_kind: str = "answer"):
    """TTS-only fallback: text question, no photo -> mp3."""
    log_event("TTS", text)
    boundary = "----blindnav%s" % int(time.time() * 1000)
    body = b""
    for name, value in (("question", text), ("lang", LANG)):
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                 f'\r\n\r\n{value}\r\n').encode()
    body += (f"--{boundary}--\r\n").encode()
    req = urllib.request.Request(
        f"http://{SERVER_IP}:{PORT}/ask", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        play_mp3(resp.read(), log_kind)


# ------------------------- voice commands -------------------------

def find_mic():
    """Return arecord hw device string of first USB capture device, else None."""
    try:
        r = subprocess.run(["arecord", "-l"], capture_output=True, timeout=10)
        for line in r.stdout.decode(errors="replace").splitlines():
            if line.startswith("card"):
                parts = line.split()
                return "hw:%s,%s" % (parts[1].rstrip(","), parts[3].rstrip(":"))
    except Exception:
        pass
    return None


def record_wav(seconds: int, path: str, device: str) -> bool:
    try:
        r = subprocess.run(["arecord", "-D", device, "-f", "S16_LE", "-r",
                            "16000", "-c", "1", "-d", str(seconds), path],
                           capture_output=True, timeout=seconds + 15)
        return r.returncode == 0 and os.path.getsize(path) > 1000
    except Exception:
        return False


def transcribe(wav_path: str) -> str:
    """POST wav to whisper-openai-adapter (language pinned hu) -> text."""
    boundary = "----blindnavstt%s" % int(time.time() * 1000)
    parts = []
    for name, value in (("model", "whisper-1"), ("language", CFG.get("STT_LANG", "hu"))):
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"'
                      '\r\n\r\n%s\r\n' % (boundary, name, value)).encode())
    audio = open(wav_path, "rb").read()
    parts.append(('--%s\r\nContent-Disposition: form-data; name="file"; '
                  'filename="cmd.wav"\r\nContent-Type: audio/wav\r\n\r\n'
                  % boundary).encode() + audio + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode())
    req = urllib.request.Request(
        CFG["STT_URL"], data=b"".join(parts), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=STT_TIMEOUT) as resp:
        import json
        return json.loads(resp.read().decode()).get("text", "").strip()


def route_intent(text: str) -> str:
    """Keyword routing on the Hungarian transcript."""
    t = text.lower()
    if not t:
        return "unknown"
    if "járd" in t or "járda" in t:
        return "sidewalk"
    if "hol van" in t or "merre" in t or "mi van" in t or "mi ez" in t:
        return "scene"
    if "menj" in t or "indul" in t or "indulj" in t:
        return "nav"
    return "unknown"


INTENT_PROMPTS = {
    "sidewalk": Q_SIDEWALK,
    "scene": Q_SCENE,
    "nav": Q_NAV,
}


def handle_voice_command(device: str):
    """Record -> log -> STT -> log -> route -> vision ask -> speak. Never crashes."""
    wav = os.path.join(tempfile.gettempdir(), "blindnav_cmd.wav")
    try:
        if not record_wav(int(CFG.get("REC_SECONDS", "4")), wav, device):
            log_event("VOICE", "record failed")
            return
        if CFG.get("LOG") == "1":
            try:
                log_bytes(open(wav, "rb").read(), "voice_in", "wav")
            except OSError:
                pass
        try:
            text = transcribe(wav)
        except Exception as e:
            log_event("STT", "error: %s" % e)
            text = ""
        log_event("STT", text or "(empty)")
        intent = route_intent(text)
        log_event("INTENT", intent)
        if intent == "unknown":
            speak_text_via_server(NOT_UNDERSTOOD)
            return
        try:
            jpeg = capture_frame()
            log_bytes(jpeg, "voice_frame", "jpg")
            speak(ask(INTENT_PROMPTS[intent], jpeg), "voice_answer")
        except Exception as e:
            log_event("ERROR", "vision: %s" % e)
            speak_text_via_server(FALLBACK)
    except Exception as e:
        log_event("ERROR", "voice: %s" % e)
        try:
            speak_text_via_server(FALLBACK)
        except Exception:
            pass


def voice_loop():
    device = find_mic()
    if not device:
        print("no USB mic found — voice commands disabled", flush=True)
        log_event("VOICE", "no mic, disabled")
        return
    print("voice loop ready (mic %s)" % device, flush=True)
    while True:
        handle_voice_command(device)
        time.sleep(1)


def handle_press(long_press: bool):
    q = Q_NAV if long_press else Q_SCENE
    try:
        jpeg = capture_frame()
        log_bytes(jpeg, "button_frame", "jpg")
        speak(ask(q, jpeg), "button_answer")
    except Exception as e:
        print("error: %s" % e, flush=True)
        log_event("ERROR", "press: %s" % e)
        try:
            speak_text_via_server(FALLBACK)
        except Exception:
            pass


def main():
    if "--server-only" in sys.argv:
        photo = sys.argv[sys.argv.index("--photo") + 1] if "--photo" in sys.argv \
            else "/home/administrator/pi3-cam-live.jpg"
        for label, q in (("SCENE", Q_SCENE), ("NAV", Q_NAV),
                         ("SIDEWALK", Q_SIDEWALK)):
            print("[%s] asking: %s" % (label, q), flush=True)
            mp3 = ask(q, open(photo, "rb").read())
            out = "/tmp/blindnav_%s.mp3" % label.lower()
            open(out, "wb").write(mp3)
            print("[%s] audio: %s (%d bytes)" % (label, out, len(mp3)), flush=True)
        return

    if "--voice-test" in sys.argv:
        wav = sys.argv[sys.argv.index("--voice-test") + 1]
        text = transcribe(wav)
        intent = route_intent(text)
        print("STT: %r" % text, flush=True)
        print("INTENT: %s -> prompt: %s" % (intent, INTENT_PROMPTS.get(intent, "Nem értem")),
              flush=True)
        return

    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(BUTTON_BCM, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    if CFG.get("VOICE") == "1":
        threading.Thread(target=voice_loop, daemon=True).start()
    print("blindnav ready: button BCM%d, server %s:%d" % (BUTTON_BCM, SERVER_IP, PORT),
          flush=True)
    try:
        while True:
            GPIO.wait_for_edge(BUTTON_BCM, GPIO.FALLING)
            time.sleep(DEBOUNCE_S)
            if GPIO.input(BUTTON_BCM) != 0:
                continue
            t0 = time.time()
            long_press = False
            while GPIO.input(BUTTON_BCM) == 0:
                if not long_press and time.time() - t0 >= LONG_PRESS_S:
                    long_press = True
                time.sleep(0.02)
            if time.time() - t0 < DEBOUNCE_S:
                continue
            print("press: %s" % ("NAV" if long_press else "SCENE"), flush=True)
            log_event("BUTTON", "NAV" if long_press else "SCENE")
            handle_press(long_press)
    finally:
        GPIO.cleanup()


if __name__ == "__main__":
    main()
