#!/bin/bash
# tts.sh "szöveg" — text -> spoken Hungarian audio via kover blindbox (text-mode /ask).
# Server IP configurable: TTS_SERVER env or /etc/blindnav.conf
set -u
SERVER="${TTS_SERVER:-$(grep -oP 'SERVER=\K.*' /etc/blindnav.conf 2>/dev/null || echo 100.119.157.3)}"
PORT="${TTS_PORT:-5014}"
TEXT="${1:?usage: tts.sh <hungarian text>}"
OUT="$(mktemp /tmp/tts-XXXXXX.mp3)"
CODE=$(curl -s -o "$OUT" -w '%{http_code}' --max-time 90 \
  -F "question=$TEXT" -F "lang=hu" "http://$SERVER:$PORT/ask")
if [ "$CODE" != "200" ] || [ "$(stat -c%s "$OUT" 2>/dev/null || echo 0)" -lt 1000 ]; then
  echo "tts.sh: server failed (HTTP $CODE)" >&2; rm -f "$OUT"; exit 1
fi
for player in "mpv --really-quiet --no-video" "ffplay -autoexit -nodisp -loglevel quiet" "paplay" "aplay"; do
  # shellcheck disable=SC2086
  if command -v "${player%% *}" >/dev/null 2>&1; then
    $player "$OUT" && { rm -f "$OUT"; exit 0; }
  fi
done
echo "tts.sh: no audio player found (install mpv)" >&2
echo "$OUT"   # leave the file for debugging
exit 1
