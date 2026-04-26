"""
시세 조회 (현재가).

기본: 삼성전자(005930). 다른 종목 조회하려면 종목코드 인자로 전달.

실행:
  python -m src.check_price             # 삼성전자
  python -m src.check_price 035720      # 카카오
  python -m src.check_price 373220      # LG에너지솔루션
"""
from __future__ import annotations

import sys

from . import config
from . import kis_api
from . import kis_auth

_ENDPOINT = "/uapi/domestic-stock/v1/quotations/inquire-price"
_TR_ID = "FHKST01010100"  # 주식 현재가 (실/모의 공통)

DEFAULT_SYMBOL = "005930"  # 삼성전자


def fetch_price(symbol: str, token: str) -> dict:
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",  # J: KRX 주식
        "FID_INPUT_ISCD": symbol,
    }
    return kis_api.get(_ENDPOINT, tr_id=_TR_ID, token=token, params=params)


def _fmt_int(value) -> str:
    try:
        return f"{int(value or 0):,}"
    except (ValueError, TypeError):
        return str(value)


def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SYMBOL

    print("=" * 60)
    print(f"시세 조회: {symbol}")
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

    try:
        data = fetch_price(symbol, token)
    except kis_api.KISAPIError as e:
        print(f"[조회 실패] {e}")
        return 3

    out = data.get("output", {})
    print(f"종목명      : {out.get('hts_kor_isnm', '?')}")
    print(f"현재가      : {_fmt_int(out.get('stck_prpr')):>12} 원")
    print(f"전일대비    : {_fmt_int(out.get('prdy_vrss')):>12} "
          f"({out.get('prdy_ctrt', '0')}%)")
    print(f"시가/고가/저가: {_fmt_int(out.get('stck_oprc'))} / "
          f"{_fmt_int(out.get('stck_hgpr'))} / "
          f"{_fmt_int(out.get('stck_lwpr'))}")
    print(f"누적 거래량 : {_fmt_int(out.get('acml_vol')):>12} 주")
    print(f"누적 거래대금: {_fmt_int(out.get('acml_tr_pbmn')):>12} 원")
    return 0


if __name__ == "__main__":
    sys.exit(main())
