#!/usr/bin/env bash
# Fill the systemd unit templates with values from deploy.env and print/write them.
#   deploy/render-units.sh [outdir]      default outdir: deploy/rendered/ (git-ignored)
# Then copy to the Pi and install, e.g.:
#   scp deploy/rendered/*.service pi@raspberrypi.local:/tmp/
#   ssh pi@raspberrypi.local 'sudo mv /tmp/nidaa-*.service /etc/systemd/system/ && sudo systemctl daemon-reload'
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f deploy.env ] && source deploy.env
NIDAA_USER="${NIDAA_USER:-pi}"
NIDAA_HOME="${NIDAA_HOME:-/home/pi/nidaa}"
NIDAA_VENV="${NIDAA_VENV:-$NIDAA_HOME/venv}"
OUT="${1:-deploy/rendered}"
mkdir -p "$OUT"
for unit in deploy/nidaa-*.service; do
  sed -e "s|@NIDAA_USER@|$NIDAA_USER|g" -e "s|@NIDAA_HOME@|$NIDAA_HOME|g" -e "s|@NIDAA_VENV@|$NIDAA_VENV|g" \
      -e '/^# Template:/d' "$unit" > "$OUT/$(basename "$unit")"
  echo "wrote $OUT/$(basename "$unit")"
done
