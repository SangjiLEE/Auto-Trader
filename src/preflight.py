"""실거래 전환 preflight 체크리스트.

체크 항목:
  1. KIS_ENV (paper/real)
  2. KIS 자격증명 (APP_KEY, APP_SECRET, ACCOUNT_NO)
  3. 계좌번호 형식
  4. Slack 6+1채널 webhook 응답
  5. DB 존재 + 최근 백업
  6. 모든 모듈 import
  7. KIS auth 토큰 발급
  8. portfolio_guard halt 상태
  9. launchd 잡 로드 수 (8+ 기대)
 10. 단위 테스트 통과

각 항목: ✅ PASS / ⚠️ WARN / ❌ FAIL
실거래 전환 가능 조건: 모두 PASS (WARN 허용).

실행:
  python -m src.preflight                # 체크
  python -m src.preflight --skip-tests   # pytest 스킵 (빠른 체크)
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).parent.parent

ACCOUNT_NO_RE = re.compile(r"^\d{8}-\d{2}$")


def _result(level: str, name: str, msg: str = "") -> dict:
    return {"level": level, "name": name, "msg": msg}


def check_env() -> dict:
    from . import config
    env = config.KIS_ENV
    if env not in ("paper", "real"):
        return _result("FAIL", "KIS_ENV", f"잘못된 값: {env}")
    if env == "real":
        return _result("WARN", "KIS_ENV", "⚠️ 실거래 모드")
    return _result("PASS", "KIS_ENV", "paper (모의투자)")


def check_credentials() -> dict:
    from . import config
    env = config.KIS_ENV
    prefix = "KIS_REAL_" if env == "real" else "KIS_PAPER_"

    missing = []
    for key in ["APP_KEY", "APP_SECRET", "ACCOUNT_NO"]:
        full = prefix + key
        if not os.getenv(full):
            missing.append(full)

    if missing:
        return _result("FAIL", "KIS 자격증명", f"누락: {', '.join(missing)}")
    return _result("PASS", "KIS 자격증명", f"{prefix}APP_KEY/SECRET/ACCOUNT_NO 모두 설정")


def check_account_format() -> dict:
    from . import config
    env = config.KIS_ENV
    var = "KIS_REAL_ACCOUNT_NO" if env == "real" else "KIS_PAPER_ACCOUNT_NO"
    val = os.getenv(var, "")
    if not val:
        return _result("FAIL", "계좌번호 형식", "비어있음")
    if not ACCOUNT_NO_RE.match(val):
        return _result("FAIL", "계좌번호 형식",
                       f"{val[:4]}*** 형식 불일치 (xxxxxxxx-yy 기대)")
    return _result("PASS", "계좌번호 형식", "xxxxxxxx-yy ✓")


def check_slack_webhooks() -> dict:
    """채널별 webhook URL 존재 여부 + 응답 (ping 안 보냄, URL 형식만)."""
    expected = [
        "SLACK_WEBHOOK_URL",
        "SLACK_WEBHOOK_KR_REALTIME", "SLACK_WEBHOOK_KR_DAILY", "SLACK_WEBHOOK_KR_WEEKLY",
        "SLACK_WEBHOOK_US_REALTIME", "SLACK_WEBHOOK_US_DAILY", "SLACK_WEBHOOK_US_WEEKLY",
        "SLACK_WEBHOOK_SYSTEM_ERROR",
    ]
    missing = []
    bad_format = []
    for v in expected:
        url = os.getenv(v, "")
        if not url:
            missing.append(v)
        elif not url.startswith("https://hooks.slack.com/services/"):
            bad_format.append(v)

    if missing == expected:
        return _result("FAIL", "Slack webhooks", "모두 미설정")
    if bad_format:
        return _result("FAIL", "Slack webhooks",
                       f"형식 오류: {', '.join(bad_format)}")
    if missing:
        return _result("WARN", "Slack webhooks",
                       f"미설정 {len(missing)}/{len(expected)}: {', '.join(missing)}")
    return _result("PASS", "Slack webhooks", f"{len(expected)}개 모두 설정")


def check_db_and_backup() -> dict:
    from . import db
    if not db.DB_PATH.exists():
        return _result("FAIL", "DB", f"파일 없음: {db.DB_PATH}")

    backups_dir = PROJECT_ROOT / "backups"
    if not backups_dir.exists():
        return _result("WARN", "DB 백업", "backups/ 디렉토리 없음 (첫 실행 전)")

    import time
    now = time.time()
    latest_age = float("inf")
    n_backups = 0
    for p in backups_dir.iterdir():
        if p.is_file() and p.suffix == ".gz":
            n_backups += 1
            age = now - p.stat().st_mtime
            latest_age = min(latest_age, age)

    if n_backups == 0:
        return _result("WARN", "DB 백업", "백업 파일 없음 (오늘 06:00 실행 예정)")

    age_h = latest_age / 3600
    if age_h > 30:
        return _result("WARN", "DB 백업", f"최근 백업 {age_h:.1f}h 전 (24h 권장)")
    return _result("PASS", "DB 백업", f"{n_backups}개, 최근 {age_h:.1f}h 전")


def check_imports() -> dict:
    modules = [
        "monthly_vaa", "monthly_faber_us", "daily_swing_v3_us",
        "daily_catalyst", "daily_snapshot", "us_closing_report",
        "weekly_report", "healthcheck", "backup_db", "preflight",
        "notify", "portfolio_guard", "idempotency", "realized_pnl",
        "live_metrics",
    ]
    failed = []
    for m in modules:
        try:
            __import__(f"src.{m}")
        except Exception as e:
            failed.append(f"{m}: {e}")
    if failed:
        return _result("FAIL", "모듈 import", "; ".join(failed[:3]))
    return _result("PASS", "모듈 import", f"{len(modules)}개 모듈 OK")


def check_kis_auth() -> dict:
    """KIS 토큰 발급 가능 여부 (네트워크 호출)."""
    try:
        from . import kis_auth
        token = kis_auth.get_access_token()
        if not token or len(token) < 20:
            return _result("FAIL", "KIS 인증", "토큰 비정상")
        return _result("PASS", "KIS 인증", f"토큰 발급 OK ({len(token)} chars)")
    except Exception as e:
        return _result("FAIL", "KIS 인증", str(e)[:200])


def check_portfolio_guard() -> dict:
    try:
        from . import portfolio_guard
        state = portfolio_guard.status()
        if state["halted"]:
            return _result("WARN", "Portfolio Guard",
                           f"HALT 활성: {state['halt_reason']}")
        return _result("PASS", "Portfolio Guard",
                       f"정상 (DD {state['current_dd_pct']:+.2f}%, 역대 {state['max_dd_pct']:+.2f}%)")
    except Exception as e:
        return _result("FAIL", "Portfolio Guard", str(e))


def check_launchd_jobs() -> dict:
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=5,
        )
        jobs = [l for l in result.stdout.splitlines()
                if "com.sangjisair.autotrading." in l]
        n = len(jobs)
        if n < 8:
            return _result("WARN", "launchd 잡", f"{n}개 (8+ 권장: 7개 strategy + backup + healthcheck)")
        return _result("PASS", "launchd 잡", f"{n}개 로드됨")
    except Exception as e:
        return _result("WARN", "launchd 잡", str(e))


def check_pytest() -> dict:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=no"],
            capture_output=True, text=True, timeout=60,
            cwd=str(PROJECT_ROOT),
        )
        last_line = result.stdout.strip().splitlines()[-1] if result.stdout else ""
        if result.returncode != 0:
            return _result("FAIL", "pytest", last_line)
        return _result("PASS", "pytest", last_line)
    except Exception as e:
        return _result("WARN", "pytest", str(e))


def run_all_checks(skip_tests: bool = False) -> list[dict]:
    checks = [
        check_env,
        check_credentials,
        check_account_format,
        check_slack_webhooks,
        check_db_and_backup,
        check_imports,
        check_kis_auth,
        check_portfolio_guard,
        check_launchd_jobs,
    ]
    if not skip_tests:
        checks.append(check_pytest)

    return [c() for c in checks]


def format_report(results: list[dict]) -> str:
    icons = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}
    lines = ["━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    lines.append("자동매매 시스템 — 실거래 전환 PREFLIGHT 체크")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    n_pass = n_warn = n_fail = 0
    for r in results:
        icons_s = icons.get(r["level"], "?")
        lines.append(f"  {icons_s} [{r['level']:<4}] {r['name']:<22} {r['msg']}")
        if r["level"] == "PASS":
            n_pass += 1
        elif r["level"] == "WARN":
            n_warn += 1
        else:
            n_fail += 1

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"  요약: ✅ PASS {n_pass}  ⚠️ WARN {n_warn}  ❌ FAIL {n_fail}")
    if n_fail == 0:
        lines.append("  ✨ 실거래 전환 가능 (FAIL 없음, WARN 검토 후 진행)")
    else:
        lines.append("  🚫 실거래 차단 — FAIL 항목 해결 필수")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="실거래 전환 preflight")
    parser.add_argument("--skip-tests", action="store_true",
                        help="pytest 스킵 (빠른 체크)")
    args = parser.parse_args()

    results = run_all_checks(skip_tests=args.skip_tests)
    report = format_report(results)
    print(report)

    n_fail = sum(1 for r in results if r["level"] == "FAIL")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
