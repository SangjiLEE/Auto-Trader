"""
KIS 해외주식 API 공통 헬퍼.

국내주식과의 차이:
  - 엔드포인트: /uapi/overseas-stock/* 또는 /uapi/overseas-price/*
  - tr_id 별도 (시장 × 매수/매도 × 환경)
  - 거래소 코드 (NASD / NYSE / AMEX) 필요
  - 통화: USD
  - 주문은 지정가 기본 (시장가는 별도 코드)
"""
from __future__ import annotations

from . import config

# 종목별 거래소 매핑
EXCHANGE_MAP: dict[str, str] = {
    # 광역 ETF
    "SPY": "AMEX",
    "QQQ": "NASD",
    "VTI": "AMEX",
    "VOO": "AMEX",
    "IWM": "AMEX",
    "DIA": "AMEX",
    "EFA": "AMEX",
    # 섹터 ETF
    "SOXX": "NASD",
    "SMH": "NASD",
    "XLK": "AMEX",
    "XLF": "AMEX",
    "XLE": "AMEX",
    "XLV": "AMEX",
    "XLY": "AMEX",
    # 메가캡 종목
    "AAPL": "NASD",
    "MSFT": "NASD",
    "NVDA": "NASD",
    "GOOG": "NASD",
    "GOOGL": "NASD",
    "AMZN": "NASD",
    "META": "NASD",
    "TSLA": "NASD",
    "NFLX": "NASD",
    "AMD": "NASD",
    "INTC": "NASD",
    "AVGO": "NASD",
    # 레버리지 (위험, 참고용)
    "TQQQ": "NASD",
    "SQQQ": "NASD",
    "SOXL": "AMEX",
    "TSLL": "NASD",
    "SPXL": "AMEX",
}

# 모든 거래소 (잔고 조회 시 순회)
US_EXCHANGES = ["NASD", "NYSE", "AMEX"]

# 시세 API용 짧은 거래소 코드 매핑
# 주문/잔고: NASD/NYSE/AMEX
# 시세:      NAS / NYS / AMS
_PRICE_EXCD_MAP = {
    "NASD": "NAS",
    "NYSE": "NYS",
    "AMEX": "AMS",
}


def get_exchange(symbol: str) -> str:
    """종목코드 → 주문/잔고용 거래소 코드. 모르면 NASD 기본값."""
    return EXCHANGE_MAP.get(symbol.upper(), "NASD")


def get_price_excd(symbol: str) -> str:
    """종목코드 → 시세 API용 짧은 거래소 코드 (NAS/NYS/AMS)."""
    return _PRICE_EXCD_MAP[get_exchange(symbol)]


def order_tr_id(side: str) -> str:
    """주문 tr_id (모의 vs 실거래)."""
    if config.KIS_ENV == "paper":
        return "VTTT1002U" if side == "buy" else "VTTT1001U"
    return "TTTT1002U" if side == "buy" else "TTTT1001U"


def balance_tr_id() -> str:
    """잔고 조회 tr_id."""
    return "VTTS3012R" if config.KIS_ENV == "paper" else "TTTS3012R"


def price_tr_id() -> str:
    """현재가 조회 tr_id (실/모의 공통)."""
    return "HHDFS00000300"


def daily_price_tr_id() -> str:
    """일봉 시세 tr_id (실/모의 공통)."""
    return "HHDFS76240000"
