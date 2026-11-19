#!/usr/bin/env bash
# Copy app/ code to the Pi and validate it there. Never restarts anything.
#
#   ./deploy.sh                 code only -> $NIDAA_HOME/app (the Pi's config.json is NOT touched)
#   ./deploy.sh --with-config   also copy app/config.json (explicit opt-in; first install)
#   ./deploy.sh --staging       code + config -> $NIDAA_HOME/staging (safe test copy)
#   ./deploy.sh --dry-run ...   show what rsync would copy, change nothing
#
# Settings come from deploy.env (copy deploy.env.example). NIDAA_PI=local rsyncs to a local
# folder instead (testing): NIDAA_PI=local NIDAA_HOME=/tmp/x ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"
[ -f deploy.env ] && source deploy.env

PI="${NIDAA_PI:-pi@raspberrypi.local}"
HOME_DIR="${NIDAA_HOME:-/home/pi/nidaa}"
PY="${NIDAA_VENV:-$HOME_DIR/venv}/bin/python"
DEST="$HOME_DIR/app/"
WITH_CONFIG=0
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --with-config) WITH_CONFIG=1 ;;
    --staging)     WITH_CONFIG=1; DEST="$HOME_DIR/staging/" ;;
    --dry-run|-n)  DRY_RUN=1 ;;
    -h|--help)     sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [ "$WITH_CONFIG" -eq 1 ] && [ ! -f app/config.json ]; then
  echo "app/config.json not found. Create it from app/config.example.json first." >&2
  exit 2
fi

# Runtime files belong to the Pi and are never copied in either direction.
EXCLUDES=(
  --exclude 'state.json' --exclude 'skips.json' --exclude 'history.jsonl'
  --exclude 'status.json' --exclude 'audit.jsonl' --exclude 'commands/'
  --exclude '__pycache__/' --exclude '*.tmp' --exclude '*.lock' --exclude 'config.json.bak-*'
  --exclude '.env' --exclude '*.env' --exclude '.DS_Store'
)
if [ "$WITH_CONFIG" -eq 0 ]; then
  EXCLUDES+=(--exclude 'config.json')
else
  echo ">>> config.json WILL be copied to $DEST"
fi

# Validate locally first so a broken file never leaves the Mac.
PYTHON="${PYTHON:-$( [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3 )}"
"$PYTHON" -m py_compile app/*.py

RSYNC_FLAGS=(-av)
[ "$DRY_RUN" -eq 1 ] && RSYNC_FLAGS+=(-n)

if [ "$PI" = "local" ]; then
  TARGET="$DEST"
  [ "$DRY_RUN" -eq 0 ] && mkdir -p "$DEST"
else
  TARGET="$PI:$DEST"
  [ "$DRY_RUN" -eq 0 ] && ssh "$PI" "mkdir -p '$DEST' '$HOME_DIR/audio'"
fi

rsync "${RSYNC_FLAGS[@]}" "${EXCLUDES[@]}" app/ "$TARGET"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "Dry run only; nothing copied."
  exit 0
fi

if [ "$PI" != "local" ]; then
  ssh "$PI" "cd '$DEST' && $PY -m py_compile nidaa_scheduler.py && $PY nidaa_scheduler.py --check"
fi

echo
echo "Copied and validated. Nothing was restarted."
if [[ "$DEST" == */staging/ ]]; then
  echo "Staging only: run it by hand there (cd $DEST && $PY nidaa_scheduler.py). Live services are untouched."
else
  echo "Check the next prayer first:  $PY ${DEST%/}/nidaa_scheduler.py --today"
  echo "Then, in a quiet window:      sudo systemctl restart nidaa-scheduler.service"
fi
