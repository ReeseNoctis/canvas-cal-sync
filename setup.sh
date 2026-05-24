#!/bin/bash
# Canvas Calendar Sync — environment setup
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_SRC="$PROJECT_DIR/com.sjtu.canvassync.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.sjtu.canvassync.plist"

echo "=== Canvas Calendar Sync Setup ==="
echo ""

echo "[1/3] Checking Python..."
python3 --version

echo "[2/3] Installing dependencies..."
pip3 install -r "$PROJECT_DIR/requirements.txt" --quiet

echo "[3/3] Configuring auto-sync job..."
sed "s|PROJECT_DIR|$PROJECT_DIR|g" "$PLIST_SRC" > "$PLIST_DST"
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

echo ""
echo "=== Setup complete ==="
echo ""
echo "Auto-sync: runs every 2 days in background"
echo "Manual run: cd $PROJECT_DIR && python3 sync.py"
echo "View logs:  tail -f $PROJECT_DIR/data/sync.log"
echo "Stop sync:  launchctl unload $PLIST_DST"
