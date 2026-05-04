"""
멀티 채널 알림 (Telegram + Slack).

체결 성공·실패·에러 시 핸드폰으로 푸시. 외출 중에도 자동매매 상태 확인 가능.

[채널 우선순위]
  - Slack 활성 → Slack 으로 전송 (기본)
  - Telegram 만 활성 → Telegram (이전 호환)
  - 둘 다 활성 → Slack + Telegram 병렬 전송
  - 둘 다 미설정 → 알림 비활성 (조용히 스킵)

[환경변수]
  Telegram:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
  Slack:
    SLACK_WEBHOOK_URL  (Incoming Webhook URL)

[Slack Webhook 만들기 가이드]
  1. https://api.slack.com/apps 접속
  2. "Create New App" → "From scratch" → 이름 + workspace 선택
  3. 좌측 "Features" → "Incoming Webhooks" → "Activate Incoming Webhooks" ON
  4. "Add New Webhook to Workspace" → 채널 선택 → 권한 허용
  5. 발급된 Webhook URL 복사 (예: https://hooks.slack.com/services/T.../B.../xxx)
  6. .env 에 SLACK_WEBHOOK_URL=<url> 추가

실행 (독립 테스트):
  python -m src.notify "테스트 메시지"
"""
from __future__ import annotations

import os
import re
import sys

import requests

# config 모듈 임포트로 .env 로드 트리거 (직접 os.getenv 해도 되지만 확실)
from . import config  # noqa: F401

_TIMEOUT = 8.0


def _telegram_creds() -> tuple[str, str]:
    """호출 시점에 환경변수 읽기."""
    return (
        os.getenv("TELEGRAM_BOT_TOKEN") or "",
        os.getenv("TELEGRAM_CHAT_ID") or "",
    )


def _slack_webhook() -> str:
    return os.getenv("SLACK_WEBHOOK_URL") or ""


def _telegram_enabled() -> bool:
    t, c = _telegram_creds()
    return bool(t) and bool(c)


def _slack_enabled() -> bool:
    return bool(_slack_webhook())


def is_enabled() -> bool:
    """Telegram 또는 Slack 중 하나라도 설정되어 있으면 True."""
    return _telegram_enabled() or _slack_enabled()


# HTML → 평문 변환 (Slack 은 HTML 미지원, Telegram 만 지원)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """HTML 태그 제거 — Slack 용."""
    return _HTML_TAG_RE.sub("", text)


def _send_telegram(message: str, silent: bool = False) -> bool:
    token, chat_id = _telegram_creds()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_notification": silent,
    }
    try:
        response = requests.post(url, json=payload, timeout=_TIMEOUT)
        return response.status_code == 200
    except requests.RequestException:
        return False


def _send_slack(message: str) -> bool:
    """Slack Incoming Webhook 으로 전송. HTML 태그 제거."""
    webhook = _slack_webhook()
    if not webhook:
        return False
    plain = _strip_html(message)
    payload = {"text": plain}
    try:
        response = requests.post(webhook, json=payload, timeout=_TIMEOUT)
        return response.status_code in (200, 204)
    except requests.RequestException:
        return False


def send(message: str, silent: bool = False) -> bool:
    """
    멀티 채널 전송.
    - Slack 활성: Slack 전송
    - Telegram 활성: Telegram 전송
    - 둘 다 활성: 병렬 전송, 하나라도 성공하면 True
    """
    slack_ok = _send_slack(message) if _slack_enabled() else False
    telegram_ok = _send_telegram(message, silent) if _telegram_enabled() else False
    return slack_ok or telegram_ok


def notify_rebalance_start(env: str, target: str, plan_summary: str) -> bool:
    msg = (
        f"<b>🔄 월간 리밸런싱 시작</b>\n"
        f"환경: {env}\n"
        f"타겟: <code>{target}</code>\n\n"
        f"<b>계획:</b>\n<pre>{plan_summary}</pre>"
    )
    return send(msg)


def notify_rebalance_success(
    env: str, orders_executed: int, summary: str
) -> bool:
    msg = (
        f"<b>✅ 리밸런싱 완료</b>\n"
        f"환경: {env}\n"
        f"체결: {orders_executed}건\n\n"
        f"<pre>{summary}</pre>"
    )
    return send(msg)


def notify_rebalance_failure(env: str, error: str) -> bool:
    msg = (
        f"<b>❌ 리밸런싱 실패</b>\n"
        f"환경: {env}\n\n"
        f"<pre>{error}</pre>"
    )
    return send(msg)


def notify_order_result(
    env: str,
    side: str,
    symbol: str,
    qty: int,
    price: int,
    success: bool,
    detail: str,
) -> bool:
    icon = "✅" if success else "❌"
    action = "매수" if side == "buy" else "매도"
    msg = (
        f"{icon} <b>{action} {symbol}</b> {qty}주 @ {price:,}원\n"
        f"환경: {env}\n"
        f"{detail}"
    )
    return send(msg)


def main() -> int:
    text = sys.argv[1] if len(sys.argv) > 1 else "Hello from auto-trading-bot"
    if not is_enabled():
        print("[스킵] 알림 채널 미설정")
        print("       .env 파일에 다음 중 하나 이상 추가:")
        print("         SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...")
        print("         TELEGRAM_BOT_TOKEN=... + TELEGRAM_CHAT_ID=...")
        return 1

    channels = []
    if _slack_enabled():
        channels.append("Slack")
    if _telegram_enabled():
        channels.append("Telegram")
    print(f"[채널] {' + '.join(channels)} 활성")

    ok = send(text)
    print("✅ 전송 성공" if ok else "❌ 전송 실패")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
