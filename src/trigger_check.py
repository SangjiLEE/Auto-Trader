"""launchd trigger 검증 — 미장 자동매매 작업이 정상 trigger 됐는지 확인.

매일 00:05 KST launchd 로 실행 (5/7 23:50 미장 trigger 후 ~15분 버퍼).

검증 대상:
  - logs/daily_swing_v3_us.log mtime ≥ 어제 23:00 KST → trigger 정상
  - logs/daily_swing_v3_us.err 에 실제 에러 신호 (Traceback / Error 키워드) 있는지

.err 파일은 launchd 의 `bash -c "cd ..."` 가 매번 zsh 의 무해한
`shell-init: error retrieving current directory` 경고를 남기기 때문에
size > 0 만으로는 에러 판정 X. 진짜 에러 키워드 (Traceback, Exception 등)
존재 여부로 판정.

이상 감지 시 Slack #system_error 채널로 알림. 정상 시 stdout 만 (조용함).

알려진 false positive:
  - 미국 공휴일 (~9일/년) 에 정규장 휴장이라 trigger 안 되는 게 정상.
    이 모듈은 휴장일 캘린더 미통합 — 휴장일 다음날 false alarm 가능.
    수동 무시 또는 추후 휴장일 캘린더 통합으로 보강.

실행:
  python -m src.trigger_check                # 검증
  python -m src.trigger_check --verbose      # 정상도 알림 발송
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import notify

PROJECT_ROOT = Path(__file__).parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"

# 검증 대상 작업: (작업 이름, log 파일명, 마지막 trigger 예정 시각 UTC offset 기준 hours)
# 미장 트리거: 23:50 KST → 점검 시점 (00:05 KST) 기준 ~15분 전.
# "어제 23:00 KST 이후 갱신됐어야 함" 으로 단순화.
TARGETS = [
    ("daily_swing_v3_us", "daily_swing_v3_us.log", "daily_swing_v3_us.err"),
]

# log mtime 이 이 시간 이상 지났으면 trigger 실패로 판정 (시간 단위).
STALE_HOURS = 25

# 실제 에러를 가리키는 키워드 (대소문자 무관 매칭)
ERR_KEYWORDS = (
    "traceback",
    "error",
    "exception",
    "failed",
    "❌",
    "errno",
    "오류",  # 한국어 (KISAPIError 메시지 등)
)
# launchd 의 bash -c 호출이 매번 stderr 에 남기는 무해한 경고 — 무시
ZSH_BENIGN_PATTERNS = (
    "shell-init: error retrieving",
    "chdir: error retrieving",
)


def _has_real_error(err_path: Path) -> bool:
    """zsh 무해 경고만 있으면 False, 진짜 에러 키워드 있으면 True.

    .err 파일이 launchd 의 cd 호출 경고 (`shell-init: error retrieving
    current directory`) 로 차 있는 경우가 일반적. 이런 라인을 걸러내고
    실제 에러 키워드가 남는지 확인.
    """
    try:
        text = err_path.read_text(errors="replace")
    except OSError:
        return False
    real_lines = [
        line for line in text.splitlines()
        if line.strip() and not any(p in line for p in ZSH_BENIGN_PATTERNS)
    ]
    if not real_lines:
        return False
    return any(
        kw in line.lower() for line in real_lines for kw in ERR_KEYWORDS
    )


def _check_target(name: str, log_name: str, err_name: str) -> list[str]:
    issues: list[str] = []
    log_path = LOGS_DIR / log_name
    err_path = LOGS_DIR / err_name

    if not log_path.exists():
        issues.append(f"{name}: log 파일 부재 ({log_name})")
        return issues

    age_hours = (datetime.now().timestamp() - log_path.stat().st_mtime) / 3600
    if age_hours > STALE_HOURS:
        issues.append(
            f"{name}: log 미갱신 {age_hours:.1f}h "
            f"(임계 {STALE_HOURS}h, 마지막: "
            f"{datetime.fromtimestamp(log_path.stat().st_mtime):%Y-%m-%d %H:%M})"
        )

    if err_path.exists() and err_path.stat().st_size > 0:
        err_age_hours = (datetime.now().timestamp() - err_path.stat().st_mtime) / 3600
        # err 가 최근 갱신 + 진짜 에러 키워드 있을 때만 이슈로 보고
        if err_age_hours < STALE_HOURS and _has_real_error(err_path):
            issues.append(
                f"{name}: err 파일에 실제 에러 신호 "
                f"(size={err_path.stat().st_size}B, "
                f"갱신 {err_age_hours:.1f}h 전)"
            )

    return issues


def run_check() -> dict:
    issues: list[str] = []
    for name, log_name, err_name in TARGETS:
        issues.extend(_check_target(name, log_name, err_name))

    return {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "n_targets": len(TARGETS),
        "issues": issues,
        "healthy": len(issues) == 0,
    }


def format_report(r: dict) -> str:
    health = "✅ HEALTHY" if r["healthy"] else "⚠️ TRIGGER 실패 의심"
    lines = [
        f"*【Trigger 점검 — {r['checked_at']}】*",
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
        lines.append("*◾️참고*")
        lines.append("　미국 공휴일 다음날엔 정상 false positive 가능 (휴장일 캘린더 미통합).")
    return "\n".join(lines)


@notify.with_error_alert("trigger_check")
def main() -> int:
    parser = argparse.ArgumentParser(description="launchd trigger 검증")
    parser.add_argument("--verbose", action="store_true", help="정상 시에도 알림 발송")
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
