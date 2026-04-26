"""
Telegram Bot 알림.

체결 성공·실패·에러 시 핸드폰으로 푸시. 외출 중에도 자동매매 상태 확인 가능.

.env 에 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 설정 필요.
둘 중 하나라도 없으면 알림 비활성 (조용히 스킵).

실행 (독립 테스트):
  python -m src.notify "테스트 메시지"
"""
from __future__ import annotations

import os
import sys

import requests

# config 모듈 임포트로 .env 로드 트리거 (직접 os.getenv 해도 되지만 확실)
from . import config  # noqa: F401

_TIMEOUT = 8.0


def _creds() -> tuple[str, str]:
    """호출 시점에 환경변수 읽기. .env 리로드 가능성 대비."""
    return (
        os.getenv("TELEGRAM_BOT_TOKEN") or "",
        os.getenv("TELEGRAM_CHAT_ID") or "",
    )


def is_enabled() -> bool:
    token, chat_id = _creds()
    return bool(token) and bool(chat_id)


def send(message: str, silent: bool = False) -> bool:
    """메시지 전송. 성공 여부 반환. 실패해도 예외 안 던짐."""
    token, chat_id = _creds()
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
        print("[스킵] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 미설정")
        print("       .env 파일에 두 값 추가 후 재시도.")
        return 1
    ok = send(text)
    print("전송 성공" if ok else "전송 실패")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
