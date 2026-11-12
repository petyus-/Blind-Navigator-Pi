#!/bin/bash
# install.sh — run ON the Pi5 (Raspberry Pi OS) as root or with sudo.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
SERVER="${BLINDNAV_SERVER:-100.119.157.3}"

echo "== installing packages =="
apt-get update -qq
apt-get install -y alsa-utils ffmpeg mpv curl python3-venv || true   # alsa-utils: arecord (USB mic) + aplay

echo "== v2: python venv + ultralytics (CPU YOLOv8n) ="
python3 -m venv /opt/blindnav-venv
/opt/blindnav-venv/bin/pip install --quiet ultralytics onnxruntime

echo "== copying files =="
install -m 644 "$SRC/blindnav.py" /usr/local/bin/blindnav.py
install -m 644 "$SRC/listen.py" /usr/local/bin/listen.py
install -m 755 "$SRC/tts.sh" /usr/local/bin/tts.sh
install -m 644 "$SRC/detect.py" /usr/local/bin/detect.py
install -m 644 "$SRC/blindnav.conf" /etc/blindnav.conf

echo "== v2.5: debug log dir (7-day retention via cron cleanup) ="
install -d -o pi -g pi -m 755 /var/lib/blindnav/log
# 03:00 daily: delete day folders older than 7 days (SD card protection)
cat > /etc/cron.d/blindnav-log <<'EOF'
3 * * * * pi  find /var/lib/blindnav/log -maxdepth 1 -type d -name '20*' -mtime +7 -exec rm -rf {} + 2>/dev/null || true
EOF

echo "== v2.5: upload today's logs to kover every 15 min + at boot ="
cat > /usr/local/bin/blindnav-log-upload.sh <<EOF
#!/bin/bash
# rsync today's debug logs to kover; failures are silent (log dir persists).
D=\$(date +%F)
rsync -a --timeout=60 -e "ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15" \\
  /var/lib/blindnav/log/\$D/ administrator@$SERVER:/home/administrator/blindnav-logs/\$D/ 2>/dev/null || true
EOF
chmod 755 /usr/local/bin/blindnav-log-upload.sh
cat > /etc/cron.d/blindnav-upload <<EOF
*/15 * * * * pi  /usr/local/bin/blindnav-log-upload.sh
@reboot pi       /usr/local/bin/blindnav-log-upload.sh
EOF

echo "== mic check (voice commands need a USB mic; none = feature disabled gracefully) ="
if arecord -l 2>/dev/null | grep -q '^card'; then
  echo "USB mic found:"
  arecord -l | grep '^card'
else
  echo "WARNING: no capture device found — plug in a USB mic for voice commands."
fi

echo "== installing systemd service ="
cat > /etc/systemd/system/blindnav.service <<EOF
[Unit]
Description=Blind Navigator v2.5 (button/voice -> camera -> vision LLM -> spoken Hungarian)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/blindnav.py
Restart=always
RestartSec=5
User=pi
Environment=BLINDNAV_SERVER=$SERVER

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now blindnav.service

echo "== v2: detect service (continuous CPU YOLO, headless, no X needed) ="
cat > /etc/systemd/system/blindnav-detect.service <<EOF
[Unit]
Description=Blind Navigator v2 continuous object detection (CPU YOLOv8n)
After=network-online.target blindnav.service
Wants=network-online.target

[Service]
ExecStart=/opt/blindnav-venv/bin/python3 /usr/local/bin/detect.py
Restart=always
RestartSec=10
User=pi

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now blindnav-detect.service

echo "== v2.6: always-listen voice service (VAD in listen.py, mic exclusive) ="
cat > /etc/systemd/system/blindnav-listen.service <<EOF
[Unit]
Description=Blind Navigator v2.6 always-listen voice mode (RMS VAD -> whisper -> intent)
After=network-online.target blindnav.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/listen.py
Restart=always
RestartSec=5
User=pi
Environment=BLINDNAV_SERVER=$SERVER
# audio group: mic permission on Raspberry Pi OS
SupplementaryGroups=audio

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable blindnav-listen.service
usermod -aG audio pi 2>/dev/null || true
# NOTE: blindnav.service's own VOICE loop (fixed-length arecord) and listen.py's
# arecord both open the mic; ALWAYS_LISTEN=1 assumes voice-only mode. Set VOICE=0
# in /etc/blindnav.conf to keep the mic exclusive to listen.py.
if arecord -l 2>/dev/null | grep -q '^card'; then
  systemctl start blindnav-listen.service
fi

sleep 2
systemctl --no-pager status blindnav.service || true

echo "== quick server test (text->speech, no button) ="
tts.sh "A vak navigátor telepítése sikeres volt." && echo "INSTALL OK" || echo "WARNING: server test failed — check network/tailscale"
