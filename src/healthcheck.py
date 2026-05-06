"""launchd 헬스체크.

매일 22:00 KST 실행 (launchd):
  - 오늘 trades INSERT 건수
  - daily_snapshot 기록 여부
  - DB 백업 24h 이내 존재 여부
  - logs/*.err 오늘 수정된 파일 (에러 발생 표시)
  - launchd 로드된 잡 수

이상 감지 시 #시스템-에러 채널, 정상 시 stdout 만 (조용함).

실행:
  python -m src.healthcheck                # 헬스체크
  python -m src.healthcheck --verbose      # 정상도 알림 발송
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config
from . import db
from . import notify

PROJECT_ROOT = Path(__file__).parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
BACKUPS_DIR = PROJECT_ROOT / "backups"


def is_market_day_kr(d: date | None = None) -> bool:
    """단순 평일 판정. 한국 휴장일 캘린더는 미통합 (false positive 가능)."""
    if d is None:
        d = date.today()
    return d.weekday() < 5  # Mon-Fri


def trades_today() -> int:
    """오늘 INSERT 된 trades 건수 (env 무관 전체)."""
    today_str = date.today().isoformat()
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM trades
            WHERE date(executed_at) = ?
            """,
            (today_str,),
        ).fetchone()
    return int(row["n"]) if row else 0


def snapshot_today() -> bool:
    today_str = date.today().isoformat()
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM daily_snapshots
            WHERE date = ? AND env = ?
            LIMIT 1
            """,
            (today_str, config.KIS_ENV),
        ).fetchone()
    return row is not None


def latest_backup_age_hours() -> float | None:
    """가장 최근 백업의 age (시간). 없으면 None."""
    if not BACKUPS_DIR.exists():
        return None
    latest_mtime = 0.0
    for p in BACKUPS_DIR.iterdir():
        if p.is_file() and p.suffix == ".gz":
            latest_mtime = max(latest_mtime, p.stat().st_mtime)
    if latest_mtime == 0:
        return None
    age_seconds = datetime.now().timestamp() - latest_mtime
    return age_seconds / 3600


def err_files_today() -> list[Path]:
    """오늘 수정된 *.err 로그 파일 (size > 0)."""
    if not LOGS_DIR.exists():
        return []
    today = date.today()
    out = []
    for p in LOGS_DIR.iterdir():
        if p.is_file() and p.suffix == ".err":
            try:
                if p.stat().st_size == 0:
                    continue
                mtime = datetime.fromtimestamp(p.stat().st_mtime).date()
                if mtime == today:
                    out.append(p)
            except OSError:
                pass
    return out


def env_issues() -> list[str]:
    """KIS .env 설정 무결성 점검. .env 깨졌는데 다른 체크가 정상 보고하는 메타-실패 방지."""
    issues: list[str] = []
    if not config.APP_KEY or not config.APP_SECRET:
        issues.append("APP_KEY/SECRET 미설정")
    if not config.CANO:
        issues.append("ACCOUNT_NO(CANO) 미설정")
    if config.KIS_ENV not in ("paper", "real"):
        issues.append(f"KIS_ENV 비정상: {config.KIS_ENV!r}")
    return issues


def disk_free_gb() -> float:
    """프로젝트 루트 디스크 여유 (GB). backups/ 누적으로 차는 경우 감지."""
    return shutil.disk_usage(PROJECT_ROOT).free / (1024**3)


def loaded_launchd_jobs() -> list[str]:
    """현재 로드된 com.sangjisair.autotrading.* 잡 목록."""
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        jobs = []
        for line in result.stdout.splitlines():
            if "com.sangjisair.autotrading." in line:
                parts = line.split()
                if len(parts) >= 3:
                    jobs.append(parts[-1])
        return sorted(jobs)
    except (subprocess.SubprocessError, OSError):
        return []


def run_check() -> dict:
    """헬스체크 실행 → 결과 dict."""
    today = date.today()
    is_weekday = is_market_day_kr(today)

    n_trades = trades_today()
    has_snapshot = snapshot_today()
    backup_age = latest_backup_age_hours()
    err_files = err_files_today()
    jobs = loaded_launchd_jobs()
    free_gb = disk_free_gb()
    env_problems = env_issues()

    issues: list[str] = []
    issues.extend(env_problems)

    if is_weekday and not has_snapshot:
        issues.append("daily_snapshot 오늘 기록 없음 (평일)")

    if backup_age is None:
        issues.append("DB 백업 파일 없음")
    elif backup_age > 30:  # 30시간 = 1일 + 6시간 버퍼
        issues.append(f"DB 백업 오래됨 ({backup_age:.1f}h, 30h 초과)")

    if err_files:
        names = [p.name for p in err_files]
        issues.append(f"오늘 에러 로그 {len(err_files)}개: {', '.join(names)}")

    if len(jobs) < 7:  # 기본 7개 잡 + backup_db = 8 기대
        issues.append(f"launchd 잡 부족 ({len(jobs)}개, 7+ 기대)")

    if free_gb < 1.0:
        issues.append(f"디스크 여유 부족: {free_gb:.2f}GB")

    return {
        "date": today.isoformat(),
        "is_weekday": is_weekday,
        "n_trades": n_trades,
        "has_snapshot": has_snapshot,
        "backup_age_h": backup_age,
        "n_err_files": len(err_files),
        "err_file_names": [p.name for p in err_files],
        "n_jobs": len(jobs),
        "jobs": jobs,
        "free_gb": free_gb,
        "issues": issues,
        "healthy": len(issues) == 0,
    }


def format_report(r: dict) -> str:
    today = r["date"]
    weekday = "평일" if r["is_weekday"] else "휴장일"
    health = "✅ HEALTHY" if r["healthy"] else "⚠️ ISSUES"

    lines = [
        f"*【시스템 헬스체크 — {today} ({weekday})】*",
        "",
        "*◾️상태*",
        f"　{health}",
    ]

    if r["issues"]:
        lines.append("")
        lines.append("*◾️감지된 이슈*")
        for i in r["issues"]:
            lines.append(f"　🚨 {i}")

    lines.append("")
    lines.append("*◾️상세*")
    lines.append(f"　오늘 거래: {r['n_trades']}건")
    lines.append(f"　스냅샷: {'✅ 기록' if r['has_snapshot'] else '❌ 없음'}")
    age = r["backup_age_h"]
    backup_str = f"{age:.1f}h 전" if age is not None else "없음"
    lines.append(f"　DB 백업: {backup_str}")
    lines.append(f"　에러 로그: {r['n_err_files']}건")
    lines.append(f"　launchd 잡: {r['n_jobs']}개")
    lines.append(f"　디스크 여유: {r['free_gb']:.1f}GB")

    return "\n".join(lines)


@notify.with_error_alert("healthcheck")
def main() -> int:
    parser = argparse.ArgumentParser(description="시스템 헬스체크")
    parser.add_argument("--verbose", action="store_true",
                        help="정상 시에도 알림 발송")
    args = parser.parse_args()

    result = run_check()
    report = format_report(result)
    print(report)

    if not result["healthy"] or args.verbose:
        if notify.is_enabled():
            notify.send(report, channel=notify.CHANNEL_SYSTEM_ERROR)

    return 0 if result["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
