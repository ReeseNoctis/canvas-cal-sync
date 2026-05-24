#!/bin/bash
# Canvas Calendar Sync — 环境初始化
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_SRC="$PROJECT_DIR/com.sjtu.canvassync.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.sjtu.canvassync.plist"

echo "=== Canvas Calendar Sync Setup ==="
echo ""

echo "[1/3] 检查 Python..."
python3 --version

echo "[2/3] 安装依赖..."
pip3 install -r "$PROJECT_DIR/requirements.txt" --quiet

echo "[3/3] 配置定时任务..."
sed "s|PROJECT_DIR|$PROJECT_DIR|g" "$PLIST_SRC" > "$PLIST_DST"
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

echo ""
echo "=== 设置完成 ==="
echo ""
echo "定时任务: 每 2 小时自动同步"
echo "手动运行: cd $PROJECT_DIR && python3 sync.py"
echo "查看日志: tail -f $PROJECT_DIR/data/sync.log"
echo "停止定时: launchctl unload $PLIST_DST"
