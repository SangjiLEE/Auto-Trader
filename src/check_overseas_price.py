"""
해외주식 현재가 조회.

실행:
  python -m src.check_overseas_price                # AAPL 기본
  python -m src.check_overseas_price NVDA
  python -m src.check_overseas_price SPY
"""
from __future__ import annotations

import sys

from . import config
from . import kis_api
from . import kis_auth
from . import kis_overseas

_ENDPOINT = "/uapi/overseas-price/v1/quotations/price"


def fetch_price(symbol: str, token: str, excd: str | None = None) -> dict:
    """현재가 조회. excd 는 짧은 거래소 코드 (NAS/NYS/AMS)."""
    if excd is None:
        excd = kis_overseas.get_price_excd(symbol)

    params = {
        "AUTH": "",
        "EXCD": excd,
        "SYMB": symbol.upper(),
    }
    return kis_api.get(
        _ENDPOINT,
        tr_id=kis_overseas.price_tr_id(),
        token=token,
        params=params,
    )


def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "AAPL"

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

    try:
        data = fetch_price(symbol, token)
    except kis_api.KISAPIError as e:
        print(f"[조회 실패] {e}")
        return 3

    output = data.get("output", {})
    excd = kis_overseas.get_price_excd(symbol)
    print(f"=== {symbol} ({excd}) ===")
    print(f"현재가     : ${output.get('last', '?')}")
    print(f"전일종가   : ${output.get('base', '?')}")
    print(f"고가/저가  : ${output.get('high', '?')} / ${output.get('low', '?')}")
    print(f"거래량     : {output.get('tvol', '?')}")
    print(f"거래대금   : ${output.get('tamt', '?')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
