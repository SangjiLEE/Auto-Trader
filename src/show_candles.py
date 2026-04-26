"""
저장된 일봉 데이터 조회·검증.

실행:
  python -m src.show_candles                # 종목별 건수·기간 요약
  python -m src.show_candles 005930         # 삼성 최근 10일 상세
  python -m src.show_candles SPY --limit 20 # SPY 최근 20일
"""
from __future__ import annotations

import argparse
import sys

from . import db


def show_summary() -> None:
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT symbol, market, COUNT(*) AS n,
                   MIN(date) AS first_date, MAX(date) AS last_date
            FROM daily_candles
            GROUP BY symbol, market
            ORDER BY market, symbol
            """
        ).fetchall()

    if not rows:
        print("저장된 데이터 없음. python -m src.load_candles 먼저 실행하세요.")
        return

    print(f"{'종목':<10} {'시장':<5} {'건수':>8}  {'시작':<12} {'마지막':<12}")
    print("-" * 55)
    for r in rows:
        print(
            f"{r['symbol']:<10} {r['market']:<5} {r['n']:>8}  "
            f"{r['first_date']:<12} {r['last_date']:<12}"
        )


def show_symbol(symbol: str, limit: int) -> None:
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT date, open, high, low, close, volume, change_rate
            FROM daily_candles
            WHERE symbol = ?
            ORDER BY date DESC
            LIMIT ?
            """,
            (symbol, limit),
        ).fetchall()

    if not rows:
        print(f"{symbol}: 데이터 없음. 수집 먼저 해주세요.")
        return

    print(f"\n{symbol} 최근 {len(rows)}일 (최신 순)")
    print(
        f"{'날짜':<12} {'시가':>10} {'고가':>10} {'저가':>10} "
        f"{'종가':>10} {'거래량':>14} {'등락률':>8}"
    )
    print("-" * 82)
    for r in rows:
        pct_str = ""
        if r["change_rate"] is not None:
            pct_str = f"{r['change_rate'] * 100:+.2f}%"
        print(
            f"{r['date']:<12} {r['open']:>10,.2f} {r['high']:>10,.2f} "
            f"{r['low']:>10,.2f} {r['close']:>10,.2f} "
            f"{r['volume']:>14,} {pct_str:>8}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="저장된 일봉 조회")
    parser.add_argument("symbol", nargs="?", help="종목코드 (생략 시 전체 요약)")
    parser.add_argument("--limit", type=int, default=10, help="최근 몇 일 (기본 10)")
    args = parser.parse_args()

    if args.symbol:
        show_symbol(args.symbol, args.limit)
    else:
        show_summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
