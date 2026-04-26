"""
해외주식 주문 (모의투자 전용 가드).

KIS 미국주식은 기본 지정가. 시장가 효과를 내려면 현재가 ± 버퍼 로 limit.
  - 매수: 현재가 × 1.005  (0.5% 위로)
  - 매도: 현재가 × 0.995  (0.5% 아래로)

이러면 정규장 시간에 거의 즉시 체결됨.

실행:
  python -m src.place_overseas_order AAPL 1 buy
"""
from __future__ import annotations

import sys

from . import check_overseas_price
from . import config
from . import kis_api
from . import kis_auth
from . import kis_overseas

_ENDPOINT = "/uapi/overseas-stock/v1/trading/order"

PRICE_BUFFER = 0.005  # 0.5% 버퍼 (시장가 효과)


def place_limit_order(
    symbol: str,
    qty: int,
    side: str,
    price_usd: float,
    token: str,
    exchange: str | None = None,
) -> dict:
    """지정가 주문 1건."""
    if exchange is None:
        exchange = kis_overseas.get_exchange(symbol)

    body = {
        "CANO": config.CANO,
        "ACNT_PRDT_CD": config.ACNT_PRDT_CD,
        "OVRS_EXCG_CD": exchange,
        "PDNO": symbol.upper(),
        "ORD_QTY": str(qty),
        "OVRS_ORD_UNPR": f"{price_usd:.4f}",
        "ORD_SVR_DVSN_CD": "0",
        "ORD_DVSN": "00",  # 00: 지정가
    }
    return kis_api.post(
        _ENDPOINT,
        tr_id=kis_overseas.order_tr_id(side),
        token=token,
        body=body,
    )


def place_market_like_order(
    symbol: str,
    qty: int,
    side: str,
    token: str,
    buffer: float = PRICE_BUFFER,
) -> dict:
    """현재가 기준 ± 버퍼 limit 주문 (시장가 효과)."""
    price_data = check_overseas_price.fetch_price(symbol, token)
    out = price_data.get("output", {})
    last = out.get("last")
    if not last:
        raise RuntimeError(f"{symbol} 현재가 조회 불가")

    cur_price = float(last)
    if side == "buy":
        order_price = cur_price * (1 + buffer)
    else:
        order_price = cur_price * (1 - buffer)

    return place_limit_order(symbol, qty, side, order_price, token)


def main() -> int:
    if config.KIS_ENV != "paper":
        print("[차단] 실거래 모드. .env에서 KIS_ENV=paper 확인.")
        return 1

    if len(sys.argv) < 2:
        print("Usage: python -m src.place_overseas_order <SYMBOL> [QTY] [buy|sell]")
        return 1

    symbol = sys.argv[1].upper()
    qty = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    side = sys.argv[3] if len(sys.argv) > 3 else "buy"

    if side not in ("buy", "sell"):
        print(f"[오류] side는 buy 또는 sell. 받은 값: {side}")
        return 1

    print("=" * 60)
    print(f"[모의] 미국주식 {side.upper()} {symbol} x {qty}주")
    print("=" * 60)

    try:
        config.validate()
        token = kis_auth.get_access_token()
    except Exception as e:
        print(f"[연결 실패] {e}")
        return 2

    print("주문 전송 중...")
    try:
        result = place_market_like_order(symbol, qty, side, token)
    except (kis_api.KISAPIError, RuntimeError) as e:
        print(f"[실패] {e}")
        print("\n흔한 원인:")
        print("  - 미국 정규장(KST 22:30~05:00 DST) 외 시간")
        print("  - USD 외화 잔고 부족 (모의도 환전 필요할 수 있음)")
        print("  - 종목코드/거래소 불일치")
        return 3

    out = result.get("output", {})
    print("주문 접수 성공.")
    print(f"  주문번호 (KRX_FWDG_ORD_ORGNO): {out.get('KRX_FWDG_ORD_ORGNO', '?')}")
    print(f"  주문번호 (ODNO): {out.get('ODNO', '?')}")
    print(f"  주문시각: {out.get('ORD_TMD', '?')}")
    print(f"  서버 메시지: {result.get('msg1', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
