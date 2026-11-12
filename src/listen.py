#!/usr/bin/env python3
"""listen.py — Blind Navigator v2.6 ALWAYS-LISTEN voice mode (Pi5, CPU-cheap).

Continuous arecord from the USB mic (16 kHz mono) -> cheap RMS energy VAD in
Python -> on speech end (~1.5 s of silence, 12 s cap) -> wav ->
blindnav.transcribe() (whisper :8012, hu) -> blindnav.route_intent() ->
blindnav.handle-style vision ask -> spoken answer.

CPU notes (powerbank!):
  * arecord does the capture; Python only reads small chunks and computes RMS
    with the stdlib `audioop`-style math on bytes (audioop: avgpp/rms).
  * During silence we sleep between chunk reads; nothing heavy runs.
  * No numpy needed on the Pi.

Config in /etc/blindnav.conf (all optional):
  ALWAYS_LISTEN=1        master switch
  VAD_THRESHOLD_MULT=3.0 speech if chunk RMS >= noise_floor * MULT
  VAD_THRESHOLD=0.03     secondary relative bump rule (floor * (1+rel))
  MAX_UTT=12             max utterance seconds (hard cap)
VAD internals: calibration 3 s at startup -> noise floor = mean RMS of quiet.
Re-calibrate whenever the room stays quiet for 600 s.

Self-test (kover, no mic):  python3 listen.py --vad-test /path/to.wav
Intent self-test:           python3 listen.py --intent-test
"""
import os
import struct
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blindnav  # noqa: E402  (reuse transcribe/route_intent/ask/speak/logging)

# ------------------------- config -------------------------
CHUNK_S = 0.1               # 100 ms chunks (1600 frames @16 kHz)
RATE = 16000
SILENCE_END_S = 1.5         # trailing silence that closes an utterance
MAX_UTT_S = 12.0
MIN_UTT_S = 0.8             # shorter buffers = noise, skip STT
CALIB_S = 3.0               # initial noise-floor calibration
RECALIB_QUIET_S = 600       # re-calibrate after 10 min of quiet
ANSWER_DEBOUNCE_S = 1.0     # ignore audio right after speaking an answer

DEFAULTS = {
    "ALWAYS_LISTEN": "1",
    "VAD_THRESHOLD": "0.03",   # relative bump above floor that counts as speech
    "VAD_THRESHOLD_MULT": "3.0",  # speech if rms >= floor * MULT (primary rule)
    "MAX_UTT": "12",
}
CFG = dict(DEFAULTS)

# re-use blindnav's conf (same /etc/blindnav.conf) and overlay listen-specific
CFG.update({k: v for k, v in blindnav.CFG.items() if k in CFG})


def _speech_rms_threshold(floor):
    mult = float(CFG.get("VAD_THRESHOLD_MULT", "3.0"))
    rel = float(CFG.get("VAD_THRESHOLD", "0.03"))
    return max(floor * mult, floor * (1.0 + rel))


# ------------------------- cheap RMS VAD -------------------------

def rms_of(chunk: bytes) -> float:
    """RMS of S16_LE mono samples, stdlib only."""
    n = len(chunk) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack("<%dh" % n, chunk)
    acc = 0
    for s in samples:
        acc += s * s
    return (acc / n) ** 0.5 / 32768.0


def wav_chunks(path: str, chunk_frames: int = int(16000 * 0.1)):
    """Yield S16_LE mono chunks from a 16 kHz wav (test harness only)."""
    import wave
    w = wave.open(path, "rb")
    try:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        bytes_per = 2 * chunk_frames
        while True:
            data = w.readframes(chunk_frames)
            if not data:
                break
            yield data
    finally:
        w.close()


def detect_segments(chunks, chunk_s=CHUNK_S, calib_s=CALIB_S,
                    silence_end_s=SILENCE_END_S, max_utt_s=MAX_UTT_S,
                    min_utt_s=MIN_UTT_S):
    """Core VAD state machine over an iterable of audio chunks.

    Returns list of (start_s, end_s) speech segments. Pure logic — unit-
    testable without a mic or arecord.
    """
    floor_n = max(1, int(calib_s / chunk_s))
    buf = []
    floor_vals = []
    seg_start = None
    last_voice = None
    segments = []
    quiet_since = None
    idx = 0
    for data in chunks:
        t0 = idx * chunk_s
        idx += 1
        r = rms_of(data)
        if seg_start is None and len(floor_vals) < floor_n:
            # calibration: quiet chunks shape the floor; a chunk clearly above
            # the running floor is speech -> end calibration immediately
            if floor_vals and r > _speech_rms_threshold(
                    sum(floor_vals) / len(floor_vals)):
                floor_n = len(floor_vals)  # short calibration, start listening
                continue
            floor_vals.append(r)
            continue
        floor = sum(floor_vals) / len(floor_vals)
        thr = _speech_rms_threshold(floor)
        if r >= thr:
            if seg_start is None:
                seg_start = t0
            last_voice = t0 + chunk_s
            quiet_since = None
        else:
            if seg_start is not None:
                if last_voice is not None and t0 + chunk_s - last_voice >= silence_end_s:
                    end = last_voice
                    if end - seg_start >= min_utt_s:
                        segments.append((round(seg_start, 2), round(end, 2)))
                    seg_start = None
                elif t0 + chunk_s - seg_start >= max_utt_s:
                    segments.append((round(seg_start, 2),
                                     round(seg_start + max_utt_s, 2)))
                    seg_start = None
            quiet_since = quiet_since or t0
            if quiet_since and t0 - quiet_since >= RECALIB_QUIET_S:
                floor_vals = [r]
                quiet_since = t0
    if seg_start is not None and last_voice is not None \
            and last_voice - seg_start >= min_utt_s:
        segments.append((round(seg_start, 2), round(last_voice, 2)))
    return segments


# ------------------------- live capture -------------------------

class ArecordStream:
    """Long-running arecord process; read fixed-size chunks from its stdout."""

    def __init__(self, device):
        self.device = device
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            ["arecord", "-D", self.device, "-f", "S16_LE", "-r", str(RATE),
             "-c", "1", "-t", "raw", "-q"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return self.proc

    def chunk(self):
        return self.proc.stdout.read(int(RATE * CHUNK_S) * 2)


def handle_utterance(wav_path):
    """Transcribe -> route -> vision ask -> speak. Mirrors blindnav intent flow."""
    try:
        blindnav.log_bytes(open(wav_path, "rb").read(), "al_voice_in", "wav")
    except OSError:
        pass
    try:
        text = blindnav.transcribe(wav_path)
    except Exception as e:
        blindnav.log_event("STT", "listen error: %s" % e)
        return
    blindnav.log_event("STT", text or "(empty)")
    intent = blindnav.route_intent(text)
    blindnav.log_event("INTENT", intent)
    if intent == "unknown":
        blindnav.speak_text_via_server(blindnav.NOT_UNDERSTOOD)
        return
    try:
        jpeg = blindnav.capture_frame()
        blindnav.log_bytes(jpeg, "al_voice_frame", "jpg")
        blindnav.speak(blindnav.ask(blindnav.INTENT_PROMPTS[intent], jpeg),
                       "al_voice_answer")
    except Exception as e:
        blindnav.log_event("ERROR", "listen vision: %s" % e)
        blindnav.speak_text_via_server(blindnav.FALLBACK)


def listen_loop():
    if CFG.get("ALWAYS_LISTEN") != "1":
        print("ALWAYS_LISTEN=0 — listen loop disabled", flush=True)
        return
    device = blindnav.find_mic()
    if not device:
        print("no USB mic found — always-listen disabled", flush=True)
        blindnav.log_event("VOICE", "no mic, listen disabled")
        return
    stream = ArecordStream(device)
    if not stream.start() or stream.proc.poll() is not None:
        print("arecord failed to start on %s" % device, flush=True)
        return
    print("always-listen ready (mic %s, calibrating %.0fs)..." %
          (device, CALIB_S), flush=True)
    floor_vals = []
    floor_n = int(CALIB_S / CHUNK_S)
    seg_start = None
    last_voice = None
    frames = []
    quiet_since = None
    ignore_until = 0.0
    try:
        while True:
            data = stream.chunk()
            if not data:
                time.sleep(0.05)
                continue
            r = rms_of(data)
            now = time.time()
            if seg_start is None and len(floor_vals) < floor_n:
                floor_vals.append(r)
                continue
            floor = sum(floor_vals) / len(floor_vals)
            thr = _speech_rms_threshold(floor)
            if r >= thr and now >= ignore_until:
                if seg_start is None:
                    seg_start = now
                    frames = []
                    print("speech started", flush=True)
                frames.append(data)
                last_voice = now
                quiet_since = None
                if now - seg_start >= float(CFG.get("MAX_UTT", "12")):
                    _finish(frames, seg_start, now)
                    frames, seg_start, ignore_until = _reset()
                continue
            if seg_start is not None:
                frames.append(data)  # keep trailing silence for natural audio
                if now - last_voice >= SILENCE_END_S:
                    _finish(frames, seg_start, last_voice)
                    frames, seg_start, ignore_until = _reset()
                continue
            quiet_since = quiet_since or now
            if now - quiet_since >= RECALIB_QUIET_S:
                floor_vals = [r]
                quiet_since = now
            time.sleep(0.02)  # cheap idle
    finally:
        if stream.proc:
            stream.proc.terminate()


def _finish(frames, seg_start, seg_end):
    dur = seg_end - seg_start
    blindnav.log_event("VAD", "utterance %.1fs" % dur)
    if dur < MIN_UTT_S:
        print("utterance too short (%.1fs) — skip" % dur, flush=True)
        return
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
    try:
        import wave
        w = wave.open(path, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(frames))
        w.close()
        print("utterance %.1fs -> %s" % (dur, path), flush=True)
        handle_utterance(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _reset():
    """Post-answer state: drop buffers, set 1s answer debounce."""
    blindnav.log_event("VAD", "answer done")
    return [], None, time.time() + ANSWER_DEBOUNCE_S


# ------------------------- self-tests (kover, no mic) -------------------------

def _vad_test(wav_path):
    segs = detect_segments(wav_chunks(wav_path))
    print("VAD segments in %s:" % wav_path)
    for s, e in segs:
        print("  speech %.2fs - %.2fs (%.2fs)" % (s, e, e - s))
    if not segs:
        print("  (none detected)")


def _intent_test():
    cases = [
        ("Hová menjek, ha a járdát elzárják?", "sidewalk"),
        ("Hol van én? Merre menjek innen?", "scene"),
        ("Menjünk, induljunk!", "nav"),
        ("asdasd", "unknown"),
    ]
    ok = True
    for text, want in cases:
        got = blindnav.route_intent(text)
        status = "OK" if got == want else "FAIL"
        ok = ok and got == want
        print("  %-45r -> %-8s (want %s) %s" % (text, got, want, status))
    print("INTENT TEST: %s" % ("PASS" if ok else "FAIL"))
    return ok


def main():
    if "--vad-test" in sys.argv:
        _vad_test(sys.argv[sys.argv.index("--vad-test") + 1])
        return
    if "--intent-test" in sys.argv:
        sys.exit(0 if _intent_test() else 1)
    listen_loop()


if __name__ == "__main__":
    main()
