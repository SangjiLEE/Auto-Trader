"""
Sprint 1 Week 1 마지막 마일스톤: 모의투자 주문 전송.

기본: 삼성전자(005930) 1주 시장가 매수.
실거래 모드에선 실행 차단.

실행:
  python -m src.place_order                      # 삼성 1주 매수
  python -m src.place_order 005930 2             # 삼성 2주 매수
  python -m src.place_order 373220 1 sell        # LG엔솔 1주 매도 (보유 있을 때)

주의:
  - 국내 정규장(평일 09:00~15:30 KST) 외 시간엔 거부되거나 예약 걸릴 수 있음.
  - 모의투자도 실제 시세 기준으로 체결됨.
"""
from __future__ import annotations

import sys

from . import check_price
from . import config
from . import kis_api
from . import kis_auth

_ENDPOINT = "/uapi/domestic-stock/v1/trading/order-cash"


def _tr_id(side: str) -> str:
    # 매수/매도 × 모의/실거래 조합
    if config.KIS_ENV == "paper":
        return "VTTC0802U" if side == "buy" else "VTTC0801U"
    return "TTTC0802U" if side == "buy" else "TTTC0801U"


def place_market_order(
    symbol: str,
    quantity: int,
    side: str,
    token: str,
) -> dict:
    """시장가 주문 전송. side는 'buy' 또는 'sell'."""
    body = {
        "CANO": config.CANO,
        "ACNT_PRDT_CD": config.ACNT_PRDT_CD,
        "PDNO": symbol,
        "ORD_DVSN": "01",   # 01: 시장가 (00: 지정가, 03: 최유리 등)
        "ORD_QTY": str(quantity),
        "ORD_UNPR": "0",    # 시장가는 0
    }
    return kis_api.post(_ENDPOINT, tr_id=_tr_id(side), token=token, body=body)


def main() -> int:
    if config.KIS_ENV != "paper":
        print("[차단] 실거래 모드에선 이 스크립트 실행 불가.")
        print(".env의 KIS_ENV=paper 확인하세요.")
        return 1

    symbol = sys.argv[1] if len(sys.argv) > 1 else "005930"
    quantity = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    side = sys.argv[3] if len(sys.argv) > 3 else "buy"

    if side not in ("buy", "sell"):
        print(f"[오류] side는 'buy' 또는 'sell'. 받은 값: {side}")
        return 1

    label = "매수" if side == "buy" else "매도"
    print("=" * 60)
    print(f"[모의] 시장가 {label}: {symbol} x {quantity}주")
    print("=" * 60)

    try:
        config.validate()
    except ValueError as e:
        print(f"[설정 오류] {e}")
        return 1

    try:
        token = kis_auth.get_access_token()
    except kis_auth.KISAuthError as e:
        print(f"[인증 실패] {e}")
        return 2

    # 참고용 현재가 표시 (주문 전 sanity check)
    try:
        price_data = check_price.fetch_price(symbol, token)
        out = price_data.get("output", {})
        name = out.get("hts_kor_isnm", "?")
        price = int(out.get("stck_prpr", "0") or 0)
        print(f"현재가: {name} {price:,}원")
        print(f"예상 체결금액(참고): {price * quantity:,}원")
        print()
    except kis_api.KISAPIError as e:
        print(f"[가격 조회 실패, 주문은 계속] {e}")

    print("주문 전송 중...")
    try:
        result = place_market_order(symbol, quantity, side, token)
    except kis_api.KISAPIError as e:
        print(f"[주문 실패] {e}")
        print("힌트:")
        print("  - 장 시간(평일 09:00~15:30) 아닐 수 있음")
        print("  - 매도 시 보유 수량 부족일 수 있음")
        print("  - 주문 단위/금액 제한 확인")
        return 3

    out = result.get("output", {})
    print("주문 접수 성공.")
    print(f"  주문번호 (ODNO)    : {out.get('ODNO', '?')}")
    print(f"  거래소 조직번호    : {out.get('KRX_FWDG_ORD_ORGNO', '?')}")
    print(f"  주문시각           : {out.get('ORD_TMD', '?')}")
    print(f"  서버 메시지        : {result.get('msg1', '')}")
    print()
    print("체결 확인 → python -m src.check_balance 재실행")
    return 0


if __name__ == "__main__":
    sys.exit(main())
