#!/bin/bash
# deploy-watch.sh — kover side. Wait for Pi5 to appear on Tailscale, then
# rsync the package and run install.sh over ssh. Run: nohup ./deploy-watch.sh &
set -u
PKG=/home/administrator/blindbox/pi/blind-nav
PI5_IP=192.0.2.20
PI_PASS_FILE=/home/administrator/blindbox/pi/blind-nav/.pi5_pass   # chmod 600
LOG=/home/administrator/blindbox/pi/blind-nav/deploy-watch.log

log(){ echo "$(date '+%F %T') $*" >> "$LOG"; }
log "watcher started, waiting for $PI5_IP"
while true; do
  if ping -c1 -W3 "$PI5_IP" >/dev/null 2>&1; then
    log "pi5 reachable — deploying"
    sshpass -f "$PI_PASS_FILE" \
      rsync -av -o StrictHostKeyChecking=no "$PKG/" pi@"$PI5_IP":/home/pi/blind-nav/ >> "$LOG" 2>&1 \
    && sshpass -f "$PI_PASS_FILE" \
      ssh -o StrictHostKeyChecking=no pi@"$PI5_IP" \
      'cd ~/blind-nav && sudo BLINDNAV_SERVER=100.119.157.3 bash install.sh' >> "$LOG" 2>&1 \
    && { log "deploy OK"; exit 0; } || log "deploy FAILED (see log)"
  fi
  sleep 300
done
