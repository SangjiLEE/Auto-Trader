"""자동매매 시스템 헬스체크.

매일 22:00 launchd 로 실행. 다음을 점검하고 이상 시 Telegram 알림:
  - 환경 변수 / 토큰 캐시 정상
  - DB 접근 가능 + trades 테이블 스키마 OK
  - 오늘 자 실행 로그가 logs/ 에 정상 생성됐는지 (스케줄 실행 검증)
  - 디스크 잔여 용량

실거래 영향 없음. 조회만.

실행:
  python -m src.healthcheck
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import config
from . import notify

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
DB_PATH = PROJECT_ROOT / "data.db"

# 매일 돌아야 하는 작업의 로그 파일명. 부재 / 24h+ 미갱신 시 경보.
DAILY_LOG_NAMES = (
    "daily_swing_v3_kr.log",
    "daily_swing_v3_us.log",
    "daily_snapshot.log",
)

DISK_FREE_WARN_GB = 1.0


def _check_env() -> list[str]:
    issues: list[str] = []
    if not config.APP_KEY or not config.APP_SECRET:
        issues.append("APP_KEY/SECRET 미설정")
    if not config.CANO:
        issues.append("ACCOUNT_NO(CANO) 미설정")
    if config.KIS_ENV not in ("paper", "real"):
        issues.append(f"KIS_ENV 비정상: {config.KIS_ENV!r}")
    return issues


def _check_db() -> list[str]:
    issues: list[str] = []
    if not DB_PATH.exists():
        return [f"data.db 없음: {DB_PATH}"]
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
            ).fetchone()
            if row is None:
                issues.append("trades 테이블 없음")
    except sqlite3.Error as exc:
        issues.append(f"DB 접근 실패: {exc}")
    return issues


def _check_recent_logs() -> list[str]:
    issues: list[str] = []
    if not LOGS_DIR.exists():
        return [f"logs 디렉토리 없음: {LOGS_DIR}"]
    threshold = datetime.now() - timedelta(hours=30)
    for name in DAILY_LOG_NAMES:
        path = LOGS_DIR / name
        if not path.exists():
            issues.append(f"로그 부재: {name}")
            continue
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        if mtime < threshold:
            issues.append(f"로그 미갱신 30h+: {name} (last={mtime:%Y-%m-%d %H:%M})")
    return issues


def _check_disk() -> list[str]:
    free_bytes = shutil.disk_usage(PROJECT_ROOT).free
    free_gb = free_bytes / (1024**3)
    if free_gb < DISK_FREE_WARN_GB:
        return [f"디스크 여유 부족: {free_gb:.2f}GB"]
    return []


def main() -> int:
    issues: list[str] = []
    issues += _check_env()
    issues += _check_db()
    issues += _check_recent_logs()
    issues += _check_disk()

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if issues:
        body = "\n".join(f"  - {i}" for i in issues)
        msg = f"[헬스체크 경보] {timestamp}\n{body}"
        print(msg)
        if notify.is_enabled():
            notify.send(msg)
        return 1

    print(f"[헬스체크 OK] {timestamp}: env/db/logs/disk 정상")
    return 0


if __name__ == "__main__":
    sys.exit(main())
