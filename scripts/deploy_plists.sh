#!/usr/bin/env bash
# deploy/*.plist 를 ~/Library/LaunchAgents/ 로 동기화 + 각 plist reload.
#
# 운영 중인 plist (StandardOutPath / StartCalendarInterval / ProgramArguments 등)
# 변경 후 단일 명령으로 OS 적용.
#
# Usage: scripts/deploy_plists.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/deploy"
LA_DIR="$HOME/Library/LaunchAgents"

if [ ! -d "$DEPLOY_DIR" ]; then
    echo "ERROR: $DEPLOY_DIR 디렉토리 없음" >&2
    exit 1
fi

mkdir -p "$LA_DIR"

count=0
for plist in "$DEPLOY_DIR"/com.sangjisair.autotrading.*.plist; do
    [ -e "$plist" ] || continue
    name="$(basename "$plist")"
    target="$LA_DIR/$name"

    cp "$plist" "$target"
    launchctl unload "$target" 2>/dev/null || true
    if launchctl load "$target" 2>&1; then
        echo "  ✓ $name"
    else
        echo "  ✗ $name (load 실패)" >&2
    fi
    count=$((count + 1))
done

echo ""
echo "동기화 + reload 완료: $count 개"
active=$(launchctl list | grep -c autotrading || true)
echo "현재 active: $active 개"
