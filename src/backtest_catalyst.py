"""
Catalyst-driven 2일 swing 백테스트 (Phase D — 룰 자체 EV 검증).

[Office-hours Phase D]
사용자 룰:
  - 2일 holding period (D+1 매수, D+3 무조건 청산)
  - 익절 +3% (도달 시 즉시)
  - 손절 -3% (도달 시 즉시)
  - 동적 익절: +3% 도달 후 더 갈 것 같으면 +10% 까지 (단순화: 본 룰은 +3% 익절)
  - Catalyst-based stock selection (랜덤 X)

가짜 catalyst (Phase D — 룰 자체 EV 측정용):
  D-1 일에 다음 모두 만족:
    - 거래량이 20일 평균의 2배 이상 (volume spike)
    - 종가가 직전 종가 대비 +3% 이상 (price spike)
  → D 일 시초 매수 → 2일 보유 (또는 ±3% 도달 시 청산)

목적:
  진짜 catalyst (뉴스/실적) 없이 단순 volume+price spike 만으로
  EV 양수인지 확인. 양수면 진짜 catalyst 추가 시 더 강해질 가능성.
  음수면 룰 자체가 동작 X → 폐기.

실행:
  python -m src.backtest_catalyst
  python -m src.backtest_catalyst --symbols NVDA TSLA SPY
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import pandas as pd

from . import cost_model as cm
from . import db
from . import metrics as mt


@dataclass
class CatalystTrade:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: pd.Timestamp
    exit_price: float
    exit_reason: str  # "PROFIT_3PCT" | "STOP_3PCT" | "TIME_EXIT_2D"
    pnl_pct: float
    days_held: int
    catalyst_volume_ratio: float
    catalyst_price_spike: float


def load_with_volume(symbol: str) -> pd.DataFrame | None:
    """심볼의 OHLCV 로드 + 20일 거래량 평균."""
    with db.connection() as conn:
        df = pd.read_sql_query(
            "SELECT date, open, high, low, close, volume FROM daily_candles "
            "WHERE symbol = ? ORDER BY date ASC",
            conn, params=(symbol,), parse_dates=["date"], index_col="date",
        )
    if df.empty or len(df) < 30:
        return None
    df["vol_ma20"] = df["volume"].rolling(window=20, min_periods=20).mean()
    df["prev_close"] = df["close"].shift(1)
    df["price_chg_pct"] = (df["close"] - df["prev_close"]) / df["prev_close"]
    df["vol_ratio"] = df["volume"] / df["vol_ma20"]
    return df


def find_catalyst_days(
    df: pd.DataFrame,
    vol_spike_mult: float = 2.0,
    price_spike_pct: float = 0.03,
) -> pd.DatetimeIndex:
    """가짜 catalyst 신호 발생일.

    조건:
      D-1 거래량이 20일 평균의 N배 이상 + D-1 종가 변화 +N% 이상
    """
    mask = (
        (df["vol_ratio"] >= vol_spike_mult)
        & (df["price_chg_pct"] >= price_spike_pct)
    )
    return df.index[mask]


def simulate_trades(
    df: pd.DataFrame,
    catalyst_dates: pd.DatetimeIndex,
    cost_rt: float = 0.006,
    profit_pct: float = 0.03,
    stop_pct: float = -0.03,
    max_holding_days: int = 2,
    symbol: str = "?",
) -> list[CatalystTrade]:
    """
    Catalyst 발생 후 2일 swing 시뮬레이션.

    매 catalyst 일 (D-1) 다음 거래일 (D) 시초 매수.
    매 거래일 high/low 로 ±3% 도달 체크. 도달 시 즉시 청산.
    2일 후 (D+2) close 로 무조건 청산.
    """
    trades = []
    dates = df.index.tolist()
    date_to_idx = {d: i for i, d in enumerate(dates)}

    for cat_date in catalyst_dates:
        if cat_date not in date_to_idx:
            continue
        cat_idx = date_to_idx[cat_date]
        # D = cat + 1 (다음 거래일 시초 매수)
        if cat_idx + 1 >= len(dates):
            continue
        entry_idx = cat_idx + 1
        entry_date = dates[entry_idx]
        entry_row = df.iloc[entry_idx]
        entry_price = float(entry_row["open"]) if pd.notna(entry_row["open"]) else float(entry_row["close"])
        if entry_price <= 0:
            continue

        catalyst_vol_ratio = float(df.loc[cat_date, "vol_ratio"])
        catalyst_price_spike = float(df.loc[cat_date, "price_chg_pct"])

        # 매 후속 거래일 평가 (D, D+1, D+2)
        exit_price = None
        exit_date = None
        exit_reason = None
        days_held = 0

        for offset in range(0, max_holding_days + 1):
            check_idx = entry_idx + offset
            if check_idx >= len(dates):
                break
            check_row = df.iloc[check_idx]
            high = float(check_row["high"]) if pd.notna(check_row["high"]) else entry_price
            low = float(check_row["low"]) if pd.notna(check_row["low"]) else entry_price
            close = float(check_row["close"]) if pd.notna(check_row["close"]) else entry_price

            high_pct = (high - entry_price) / entry_price
            low_pct = (low - entry_price) / entry_price

            # offset == 0 (D 일) 은 매수 직후라 시초 이후만 평가
            # 단순화: D 일도 high/low 평가 (시초 이후 가격 반영)
            if offset > 0 or True:
                # 익절 (high 가 +3% 이상)
                if high_pct >= profit_pct:
                    exit_price = entry_price * (1 + profit_pct)
                    exit_date = dates[check_idx]
                    exit_reason = "PROFIT_3PCT"
                    days_held = offset
                    break
                # 손절 (low 가 -3% 이하)
                if low_pct <= stop_pct:
                    exit_price = entry_price * (1 + stop_pct)
                    exit_date = dates[check_idx]
                    exit_reason = "STOP_3PCT"
                    days_held = offset
                    break

        # max_holding_days 도달 시 close 매매
        if exit_price is None:
            exit_idx = min(entry_idx + max_holding_days, len(dates) - 1)
            exit_row = df.iloc[exit_idx]
            exit_price = float(exit_row["close"])
            exit_date = dates[exit_idx]
            exit_reason = "TIME_EXIT_2D"
            days_held = max_holding_days

        # P&L (비용 차감)
        pnl_pct = (exit_price - entry_price) / entry_price - cost_rt

        trades.append(CatalystTrade(
            symbol=symbol,
            entry_date=entry_date, entry_price=entry_price,
            exit_date=exit_date, exit_price=exit_price,
            exit_reason=exit_reason, pnl_pct=pnl_pct,
            days_held=days_held,
            catalyst_volume_ratio=catalyst_vol_ratio,
            catalyst_price_spike=catalyst_price_spike,
        ))

    return trades


def analyze_trades(trades: list[CatalystTrade]) -> dict:
    """거래 통계 계산."""
    if not trades:
        return {"n_trades": 0}

    df = pd.DataFrame([{
        "pnl_pct": t.pnl_pct,
        "exit_reason": t.exit_reason,
        "days_held": t.days_held,
    } for t in trades])

    n = len(df)
    wins = df[df["pnl_pct"] > 0]
    losses = df[df["pnl_pct"] <= 0]
    n_profit_exit = len(df[df["exit_reason"] == "PROFIT_3PCT"])
    n_stop_exit = len(df[df["exit_reason"] == "STOP_3PCT"])
    n_time_exit = len(df[df["exit_reason"] == "TIME_EXIT_2D"])

    win_rate = len(wins) / n if n > 0 else 0
    avg_pnl = df["pnl_pct"].mean()
    avg_win = wins["pnl_pct"].mean() if len(wins) > 0 else 0
    avg_loss = losses["pnl_pct"].mean() if len(losses) > 0 else 0
    sum_pnl = df["pnl_pct"].sum()  # 누적 (단순 합산, 복리 X)
    std_pnl = df["pnl_pct"].std()
    sharpe = (avg_pnl / std_pnl) * (252 / 2) ** 0.5 if std_pnl > 0 else 0  # 2일 holding annualized

    return {
        "n_trades": n,
        "n_profit_exit": n_profit_exit,
        "n_stop_exit": n_stop_exit,
        "n_time_exit": n_time_exit,
        "win_rate": win_rate,
        "avg_pnl_pct": avg_pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "sum_pnl_pct": sum_pnl,
        "std_pnl": std_pnl,
        "sharpe_annual": sharpe,
        "ev_per_trade": avg_pnl,
    }


def print_summary(symbol: str, trades: list[CatalystTrade], stats: dict, df: pd.DataFrame) -> None:
    print(f"\n{'='*78}")
    print(f"Catalyst 백테스트: {symbol} ({len(df)}일)")
    print(f"{'='*78}")

    if stats["n_trades"] == 0:
        print("  거래 없음 (catalyst 신호 발생 안 함)")
        return

    n = stats["n_trades"]
    print(f"  Catalyst 발생: {n}회")
    print(f"  거래 결과:")
    print(f"    PROFIT (+3% 익절):  {stats['n_profit_exit']:>4} ({stats['n_profit_exit']/n*100:>5.1f}%)")
    print(f"    STOP   (-3% 손절):  {stats['n_stop_exit']:>4} ({stats['n_stop_exit']/n*100:>5.1f}%)")
    print(f"    TIME   (2일 만료):  {stats['n_time_exit']:>4} ({stats['n_time_exit']/n*100:>5.1f}%)")
    print(f"  Win rate:        {stats['win_rate']*100:.1f}%")
    print(f"  평균 P&L/거래:   {stats['avg_pnl_pct']*100:+.3f}%  ← EV")
    print(f"  평균 승:         {stats['avg_win']*100:+.3f}%")
    print(f"  평균 패:         {stats['avg_loss']*100:+.3f}%")
    print(f"  표준편차:        {stats['std_pnl']*100:.3f}%")
    print(f"  연환산 Sharpe:   {stats['sharpe_annual']:.2f}")
    print(f"  누적 (단순합):   {stats['sum_pnl_pct']*100:+.2f}%")

    # 평가
    if stats["ev_per_trade"] > 0.001:
        verdict = "✅ EV 양수 — Phase B 진행 가치 있음"
    elif stats["ev_per_trade"] > -0.001:
        verdict = "⚠️ EV ≈ 0 — break-even, catalyst 강화 필요"
    else:
        verdict = "❌ EV 음수 — 룰 자체 작동 X, 폐기 권장"
    print(f"  판정: {verdict}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Catalyst 2일 swing 백테스트")
    parser.add_argument("--symbols", nargs="+",
                        default=["NVDA", "TSLA", "AAPL", "SPY", "QQQ",
                                 "069500", "005930", "035420"])
    parser.add_argument("--vol-mult", type=float, default=2.0,
                        help="catalyst: 거래량 평균 대비 N배 (기본 2.0)")
    parser.add_argument("--price-spike", type=float, default=0.03,
                        help="catalyst: D-1 가격 변화 N%% 이상 (기본 0.03=3%%)")
    args = parser.parse_args()

    print("=" * 78)
    print("Catalyst 2일 swing 백테스트 (Phase D — 룰 EV 검증)")
    print("=" * 78)
    print(f"  Catalyst: 거래량 ≥ 20MA × {args.vol_mult} AND 종가 변화 ≥ +{args.price_spike*100:.1f}%")
    print(f"  Entry: catalyst 다음날 시초 매수")
    print(f"  Exit: +3% 익절 / -3% 손절 / 2일 max")

    all_trades = []
    summary_rows = []

    for sym in args.symbols:
        df = load_with_volume(sym)
        if df is None:
            print(f"\n{sym}: 데이터 부족, 스킵")
            continue

        cat_dates = find_catalyst_days(df, args.vol_mult, args.price_spike)
        cm_obj = cm.get_cost_model(sym)
        trades = simulate_trades(
            df, cat_dates, cost_rt=cm_obj.round_trip,
            profit_pct=0.03, stop_pct=-0.03, max_holding_days=2,
            symbol=sym,
        )
        stats = analyze_trades(trades)
        print_summary(sym, trades, stats, df)
        all_trades.extend(trades)

        if stats["n_trades"] > 0:
            summary_rows.append({
                "symbol": sym,
                "n": stats["n_trades"],
                "win_rate": stats["win_rate"],
                "ev": stats["avg_pnl_pct"],
                "sharpe": stats["sharpe_annual"],
                "sum": stats["sum_pnl_pct"],
            })

    # 종합
    if all_trades:
        all_stats = analyze_trades(all_trades)
        print(f"\n{'='*78}")
        print(f"전체 종합 ({len(all_trades)}거래)")
        print(f"{'='*78}")
        print(f"  Win rate:      {all_stats['win_rate']*100:.1f}%")
        print(f"  평균 EV/거래:  {all_stats['avg_pnl_pct']*100:+.3f}%  ← 핵심 지표")
        print(f"  연환산 Sharpe: {all_stats['sharpe_annual']:.2f}")
        print(f"  누적 (단순):   {all_stats['sum_pnl_pct']*100:+.2f}%")

        # Symbol 별 요약 표
        print(f"\n  {'심볼':<10} {'n':>5} {'WR':>7} {'EV/거래':>10} {'Sharpe':>8} {'누적':>10}")
        print("  " + "-" * 60)
        for r in summary_rows:
            print(f"  {r['symbol']:<10} {r['n']:>5} {r['win_rate']*100:>6.1f}% "
                  f"{r['ev']*100:>+9.3f}% {r['sharpe']:>8.2f} {r['sum']*100:>+9.2f}%")

        if all_stats["ev_per_trade"] > 0.001:
            print(f"\n✅ 종합 EV 양수 → Phase B (자동 catalyst LLM news) 진행 가치 있음")
        elif all_stats["ev_per_trade"] > -0.001:
            print(f"\n⚠️ 종합 EV ≈ 0 → 진짜 catalyst (뉴스/실적) 추가 시 양수 가능성")
        else:
            print(f"\n❌ 종합 EV 음수 → 룰 자체 작동 X. 진짜 catalyst 도 못 살릴 가능성")

    return 0


if __name__ == "__main__":
    sys.exit(main())
