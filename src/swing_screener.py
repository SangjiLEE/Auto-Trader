"""
빠른 스윙 적합 종목 스크리너.

각 종목에 0~10 점 부여:
  - ADX(14) < 20 (저추세): +3
  - ADX 20~25: +2
  - ATR/Price 1.5~3% (스윗스팟): +2
  - ATR/Price 1~1.5% 또는 3~5%: +1
  - MA20 교차 60일 중 8회 이상 (박스권): +2
  - MA20 교차 4~7회: +1
  - 200MA 평탄 (±10%): +1
  - 거래량 충분: +1

점수 6+ = 단타 적합. 5 이하 = 부적합 (BH 가 나음).

실행:
  python -m src.swing_screener                        # 기본 후보 전체
  python -m src.swing_screener --symbols 069500 SPY   # 특정 종목만
  python -m src.swing_screener --backtest             # Top 종목 백테스트도
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from . import db
from . import indicators
from . import load_candles

# 단타 후보 유니버스
KR_CANDIDATES = ["069500", "229200", "005930", "000660", "035420",
                 "005380", "000270", "247540"]
US_CANDIDATES = ["SPY", "QQQ", "IWM", "DIA", "AAPL", "NVDA", "TSLA",
                 "MSFT", "AMZN", "META", "XLF", "XLE", "KRE", "SMH"]


def load_or_skip(symbol: str, market: str) -> pd.DataFrame | None:
    """DB에 있으면 로드, 없으면 FDR 로 받아옴."""
    with db.connection() as conn:
        df = pd.read_sql_query(
            "SELECT date, open, high, low, close, volume FROM daily_candles "
            "WHERE symbol = ? ORDER BY date ASC",
            conn, params=(symbol,), parse_dates=["date"], index_col="date",
        )
    if df.empty or len(df) < 200:
        try:
            print(f"  {symbol}: 데이터 부족, FDR 갱신 중...")
            load_candles.load_symbol(symbol, market, years=3)
            with db.connection() as conn:
                df = pd.read_sql_query(
                    "SELECT date, open, high, low, close, volume FROM daily_candles "
                    "WHERE symbol = ? ORDER BY date ASC",
                    conn, params=(symbol,), parse_dates=["date"], index_col="date",
                )
        except Exception as e:
            print(f"  {symbol}: 로드 실패 - {e}")
            return None
    if df.empty or len(df) < 200:
        return None
    return indicators.attach_all(df)


def score_symbol(symbol: str, df: pd.DataFrame) -> dict:
    """0~10 점 부여."""
    last = df.iloc[-1]
    notes: list[str] = []
    score = 0

    # 1. ADX (추세 강도)
    adx_val = float(last.get("adx14") or 50)
    if adx_val < 20:
        score += 3
        notes.append(f"ADX {adx_val:.0f} 저추세 ★")
    elif adx_val < 25:
        score += 2
        notes.append(f"ADX {adx_val:.0f} 약추세")
    else:
        notes.append(f"ADX {adx_val:.0f} 강추세")

    # 2. ATR/Price (변동성)
    atr_val = float(last.get("atr14") or 0)
    price = float(last["close"])
    atr_pct = atr_val / price if price > 0 else 0
    if 0.015 <= atr_pct <= 0.03:
        score += 2
        notes.append(f"ATR {atr_pct*100:.2f}% 스윗스팟")
    elif 0.01 <= atr_pct < 0.015 or 0.03 < atr_pct <= 0.05:
        score += 1
        notes.append(f"ATR {atr_pct*100:.2f}% OK")
    else:
        notes.append(f"ATR {atr_pct*100:.2f}% {'낮음' if atr_pct < 0.01 else '높음'}")

    # 3. MA20 교차 빈도 (박스권 판정)
    recent = df.iloc[-60:]
    above_ma = recent["close"] > recent["ma20"]
    crossings = int((above_ma.astype(int).diff().abs() == 1).sum())
    if crossings >= 8:
        score += 2
        notes.append(f"MA20 교차 {crossings}회 박스권")
    elif crossings >= 4:
        score += 1
        notes.append(f"MA20 교차 {crossings}회")
    else:
        notes.append(f"MA20 교차 {crossings}회 단방향")

    # 4. 200MA 평탄도
    ma200 = recent["ma200"]
    if ma200.notna().any():
        first_ma = float(ma200.dropna().iloc[0])
        last_ma = float(ma200.dropna().iloc[-1])
        if first_ma > 0:
            change = abs(last_ma - first_ma) / first_ma
            if change < 0.10:
                score += 1
                notes.append(f"200MA 평탄 ({change*100:.1f}%)")

    # 5. 유동성 (단순 거래대금 평균)
    daily_value = (recent["close"] * recent["volume"]).mean()
    # KR 100억원 또는 US $100M = 양호
    if daily_value > 100_000_000_000 or daily_value > 100_000_000:
        score += 1
        notes.append("유동성 OK")

    return {
        "symbol": symbol,
        "score": score,
        "adx": adx_val,
        "atr_pct": atr_pct,
        "crossings": crossings,
        "price": price,
        "notes": notes,
    }


def print_results(results: list[dict]) -> None:
    if not results:
        return
    results.sort(key=lambda r: -r["score"])

    print("\n" + "=" * 90)
    print("빠른 스윙 적합도 점수 (높을수록 적합, 6+ = 추천)")
    print("=" * 90)
    print(f"{'점수':<5} {'종목':<8} {'ADX':>5} {'ATR%':>6} {'교차':>5} {'현재가':>10}  코멘트")
    print("-" * 90)
    for r in results:
        emoji = "★" if r["score"] >= 6 else "·" if r["score"] >= 4 else " "
        print(
            f"{emoji} {r['score']:<3} {r['symbol']:<8} {r['adx']:>5.0f} "
            f"{r['atr_pct']*100:>5.2f}% {r['crossings']:>5} "
            f"{r['price']:>10,.2f}  {' / '.join(r['notes'])}"
        )

    # 결론
    top = [r for r in results if r["score"] >= 6]
    mid = [r for r in results if 4 <= r["score"] < 6]
    low = [r for r in results if r["score"] < 4]
    print("\n" + "=" * 90)
    print(f"  ★ 적합 ({len(top)}종목): {', '.join(r['symbol'] for r in top)}")
    print(f"  · 보통 ({len(mid)}종목): {', '.join(r['symbol'] for r in mid)}")
    print(f"    부적합 ({len(low)}종목): {', '.join(r['symbol'] for r in low)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="단타 적합도 스크리너")
    parser.add_argument(
        "--symbols", nargs="+",
        help="특정 종목만 (생략 시 KR + US 기본 후보 전체)",
    )
    args = parser.parse_args()

    if args.symbols:
        targets = []
        for s in args.symbols:
            market = "US" if s.isalpha() else "KR"
            targets.append((s.upper(), market))
    else:
        targets = [(s, "KR") for s in KR_CANDIDATES] + [(s, "US") for s in US_CANDIDATES]

    print(f"스크리닝 대상 {len(targets)}개...")
    results = []
    for sym, market in targets:
        df = load_or_skip(sym, market)
        if df is None:
            continue
        results.append(score_symbol(sym, df))

    print_results(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
