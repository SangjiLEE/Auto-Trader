#!/usr/bin/env bash
# 단발성 catch-up 작업 등록. trigger 후 자기 unload + 자기 plist 삭제.
#
# 누락된 monthly / daily 작업을 한 번 더 실행하고 싶을 때 사용.
# Claude Code 가 PR#10 시리즈에서 monthly_rebalance 누락 복구를 위해 만든 패턴.
#
# Usage:
#   scripts/schedule_makeup.sh <module> <YYYY-MM-DD> <HH:MM>
#
# 예:
#   scripts/schedule_makeup.sh monthly_rebalance 2026-05-08 09:30
#   scripts/schedule_makeup.sh daily_swing_v3_kr 2026-05-09 09:30
set -euo pipefail

if [ $# -ne 3 ]; then
    cat <<EOF
Usage: $0 <module> <YYYY-MM-DD> <HH:MM>

예:
  $0 monthly_rebalance 2026-05-08 09:30
  $0 daily_swing_v3_kr 2026-05-09 09:30

trigger 후 자동으로 unload + 자기 plist 삭제됨.
EOF
    exit 1
fi

MODULE="$1"
DATE_STR="$2"
TIME_STR="$3"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LA_DIR="$HOME/Library/LaunchAgents"
LABEL="com.sangjisair.autotrading.${MODULE}_makeup"
PLIST="$LA_DIR/$LABEL.plist"

# YYYY-MM-DD / HH:MM 파싱 + leading zero 제거 (launchd plist 는 integer 요구)
YEAR="${DATE_STR%%-*}"
REST="${DATE_STR#*-}"
MONTH="${REST%%-*}"
DAY="${REST##*-}"
HOUR="${TIME_STR%%:*}"
MINUTE="${TIME_STR##*:}"
MONTH=$((10#$MONTH))
DAY=$((10#$DAY))
HOUR=$((10#$HOUR))
MINUTE=$((10#$MINUTE))

mkdir -p "$LA_DIR"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>cd "$REPO_ROOT" &amp;&amp; "$REPO_ROOT/.venv/bin/python" -m src.$MODULE --execute --yes; launchctl unload "$PLIST"; rm "$PLIST"</string>
    </array>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Month</key><integer>$MONTH</integer>
        <key>Day</key><integer>$DAY</integer>
        <key>Hour</key><integer>$HOUR</integer>
        <key>Minute</key><integer>$MINUTE</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>$REPO_ROOT/logs/${MODULE}_makeup.log</string>
    <key>StandardErrorPath</key>
    <string>$REPO_ROOT/logs/${MODULE}_makeup.err</string>

    <key>RunAtLoad</key>
    <false/>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>LANG</key>
        <string>ko_KR.UTF-8</string>
    </dict>

    <key>WorkingDirectory</key>
    <string>$REPO_ROOT</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "✓ $LABEL 등록"
echo "  실행 예정: $DATE_STR $TIME_STR (Year $YEAR/$MONTH/$DAY $HOUR:$MINUTE)"
echo "  trigger 후 자동 unload + 자기 plist 삭제"
echo ""
echo "취소하려면:"
echo "  launchctl unload \"$PLIST\" && rm \"$PLIST\""
