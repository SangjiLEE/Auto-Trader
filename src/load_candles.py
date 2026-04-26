"""
과거 일봉 데이터 수집 (FinanceDataReader).

1차 소스로 FDR 사용. 인증 없이 한국·미국 주식 10년+ 일봉을 빠르게 가져온다.
KIS API는 실거래·모의·실시간 시세용으로 그대로 유지.

실행:
  python -m src.load_candles                       # 기본 유니버스 10년치
  python -m src.load_candles 005930                # 삼성 한 종목
  python -m src.load_candles AAPL --market US      # 애플
  python -m src.load_candles --years 5             # 최근 5년만
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

import FinanceDataReader as fdr
import pandas as pd

from . import db

# 첫 백테스트용 소규모 유니버스
DEFAULT_UNIVERSE: list[tuple[str, str]] = [
    ("005930", "KR"),  # 삼성전자
    ("373220", "KR"),  # LG에너지솔루션
    ("069500", "KR"),  # KODEX 200
    ("SPY", "US"),     # S&P 500 ETF
    ("QQQ", "US"),     # Nasdaq 100 ETF
]


def fetch_candles(
    symbol: str, market: str, start: date, end: date
) -> pd.DataFrame:
    """FDR로 일봉 가져오기. 반환 DataFrame의 인덱스는 Date."""
    # FDR은 symbol만으로 한국·미국 자동 판별하지만 명시적으로 전달 가능
    return fdr.DataReader(symbol, start, end)


def save_candles(symbol: str, market: str, df: pd.DataFrame) -> int:
    """SQLite에 UPSERT. (symbol, date) 충돌 시 덮어쓴다."""
    if df.empty:
        return 0

    rows = []
    for idx, row in df.iterrows():
        d = idx.date() if hasattr(idx, "date") else idx

        close = float(row["Close"])

        # Adj Close 없는 시장(한국주)은 close로 대체
        adj_close = close
        if "Adj Close" in df.columns and pd.notna(row["Adj Close"]):
            adj_close = float(row["Adj Close"])

        change_rate = 0.0
        if "Change" in df.columns and pd.notna(row["Change"]):
            change_rate = float(row["Change"])

        volume = 0
        if pd.notna(row["Volume"]):
            volume = int(row["Volume"])

        rows.append(
            (
                symbol,
                market,
                d.isoformat(),
                float(row["Open"]),
                float(row["High"]),
                float(row["Low"]),
                close,
                volume,
                adj_close,
                change_rate,
            )
        )

    with db.connection() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO daily_candles
                (symbol, market, date, open, high, low, close,
                 volume, adj_close, change_rate)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return len(rows)


def load_symbol(symbol: str, market: str, years: int) -> int:
    end = date.today()
    start = end - timedelta(days=years * 365 + 30)
    print(f"  {symbol:<8} ({market}): {start} → {end} ...", end=" ", flush=True)
    try:
        df = fetch_candles(symbol, market, start, end)
    except Exception as e:
        print(f"실패: {e}")
        return 0
    n = save_candles(symbol, market, df)
    print(f"{n}개 저장")
    return n


def main() -> int:
    parser = argparse.ArgumentParser(description="일봉 수집")
    parser.add_argument("symbol", nargs="?", help="종목코드 (생략 시 기본 유니버스)")
    parser.add_argument("--market", choices=["KR", "US"], default="KR")
    parser.add_argument("--years", type=int, default=10, help="몇 년치 (기본 10)")
    args = parser.parse_args()

    db.init_schema()
    print(f"DB: {db.DB_PATH}")

    if args.symbol:
        targets = [(args.symbol, args.market)]
    else:
        targets = DEFAULT_UNIVERSE

    print(f"수집 대상 {len(targets)}개, 최근 {args.years}년:")
    total = 0
    for sym, mkt in targets:
        total += load_symbol(sym, mkt, args.years)
    print(f"\n총 {total}개 캔들 저장.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
