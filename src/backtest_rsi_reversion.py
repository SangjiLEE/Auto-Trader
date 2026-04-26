"""
RSI 평균회귀 백테스트.

실행:
  python -m src.backtest_rsi_reversion           # 기본 유니버스
  python -m src.backtest_rsi_reversion AAPL
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

import pandas as pd

from . import db
from . import indicators
from . import market_regime as mr
from . import strategy_rsi_reversion as rsi

INITIAL_CAPITAL = 5_000_000
COMMISSION = 0.003
POSITION_PCT = 1.00  # 단일종목 백테스트는 100% 투입

DEFAULT_UNIVERSE = ["069500", "005930", "035420", "AAPL", "NVDA", "TSLA"]


@dataclass
class BacktestState:
    cash: float
    position: rsi.Position | None = None
    completed_trades: list[dict] = field(default_factory=list)
    equity_curve: dict[pd.Timestamp, float] = field(default_factory=dict)
    last_exit_date: pd.Timestamp | None = None


def equity(state: BacktestState, price: float) -> float:
    qty = state.position.qty if state.position else 0
    return state.cash + qty * price


def enter(state: BacktestState, qty: int, price: float, date: pd.Timestamp) -> None:
    cost = qty * price * (1 + COMMISSION / 2)
    if cost > state.cash:
        qty = int(state.cash / (price * (1 + COMMISSION / 2)))
        if qty <= 0:
            return
        cost = qty * price * (1 + COMMISSION / 2)
    state.cash -= cost
    state.position = rsi.Position(entry_date=date, entry_price=price, qty=qty)


def exit_pos(state: BacktestState, price: float, date: pd.Timestamp, reason: str, regime: str) -> None:
    if state.position is None:
        return
    pos = state.position
    state.cash += pos.qty * price * (1 - COMMISSION / 2)
    pnl = (price - pos.entry_price) * pos.qty - (pos.qty * pos.entry_price * COMMISSION)
    pnl_pct = pnl / (pos.qty * pos.entry_price)
    state.completed_trades.append({
        "entry_date": pos.entry_date,
        "exit_date": date,
        "entry_price": pos.entry_price,
        "exit_price": price,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "days_held": (date - pos.entry_date).days,
        "reason": reason,
        "regime": regime,
    })
    state.last_exit_date = date
    state.position = None


def run_backtest(df: pd.DataFrame, initial_capital: float = INITIAL_CAPITAL) -> BacktestState:
    state = BacktestState(cash=initial_capital)
    dates = df.index.tolist()

    for i, date in enumerate(dates):
        row = df.iloc[i]
        price = float(row["close"]) if pd.notna(row["close"]) else None
        if price is None:
            state.equity_curve[date] = equity(state, state.position.entry_price if state.position else 0)
            continue

        # 청산
        if state.position is not None:
            ex = rsi.check_exit(row, state.position, date)
            if ex.should_exit:
                regime = mr.detect_regime(row)
                exit_pos(state, price, date, ex.reason, regime)

        # 진입
        if state.position is None:
            # 쿨다운
            if state.last_exit_date is not None:
                days_since = (date - state.last_exit_date).days
                if days_since < rsi.REENTRY_COOLDOWN_DAYS:
                    state.equity_curve[date] = equity(state, price)
                    continue

            if i >= 1:
                prev_row = df.iloc[i - 1]
                sig = rsi.check_entry(prev_row)
                if sig.valid:
                    target_alloc = state.cash * POSITION_PCT
                    qty = int(target_alloc / price)
                    if qty > 0:
                        enter(state, qty, price, date)

        state.equity_curve[date] = equity(state, price)

    # 강제 청산
    if state.position is not None and dates:
        last_date = dates[-1]
        last_price = float(df.iloc[-1]["close"])
        regime = mr.detect_regime(df.iloc[-1])
        exit_pos(state, last_price, last_date, "백테스트 종료", regime)

    return state


def load_symbol(symbol: str) -> pd.DataFrame | None:
    with db.connection() as conn:
        df = pd.read_sql_query(
            "SELECT date, open, high, low, close, volume FROM daily_candles "
            "WHERE symbol = ? ORDER BY date ASC",
            conn, params=(symbol,), parse_dates=["date"], index_col="date",
        )
    if df.empty:
        return None
    return indicators.attach_all(df)


def print_summary(symbol: str, state: BacktestState, df: pd.DataFrame, initial: float) -> None:
    eq = pd.Series(state.equity_curve)
    if eq.empty:
        print(f"{symbol}: 결과 없음")
        return

    final = float(eq.iloc[-1])
    total_return = (final - initial) / initial
    days = len(eq)
    years = days / 252 if days > 0 else 1
    cagr = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0
    returns = eq.pct_change().fillna(0)
    sharpe = float((returns.mean() * 252) / (returns.std() * (252 ** 0.5))) if returns.std() > 0 else 0
    running_max = eq.cummax()
    mdd = float(((eq - running_max) / running_max).min())

    bh_return = float(df["close"].iloc[-1] / df["close"].iloc[0] - 1)
    diff = total_return - bh_return

    trades = state.completed_trades
    wins = [t for t in trades if t["pnl"] > 0]
    win_rate = len(wins) / len(trades) if trades else 0
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    losses = [t for t in trades if t["pnl"] <= 0]
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    avg_days = sum(t["days_held"] for t in trades) / len(trades) if trades else 0

    # 청산 사유 분포
    reasons = {}
    for t in trades:
        key = t["reason"].split()[0] if t["reason"] else "?"
        reasons[key] = reasons.get(key, 0) + 1

    # 체제별 거래
    by_regime = {mr.REGIME_BULL: 0, mr.REGIME_RANGE: 0, mr.REGIME_BEAR: 0}
    for t in trades:
        if t["regime"] in by_regime:
            by_regime[t["regime"]] += 1

    print("\n" + "=" * 76)
    print(f"RSI 평균회귀 백테스트: {symbol}")
    print("=" * 76)
    print(f"  기간          : {df.index[0].date()} → {df.index[-1].date()} ({len(df)}일)")
    print(f"  최종 자본     : {final:>12,.0f} (초기 {initial:,.0f})")
    print(f"  누적 수익     : {total_return*100:>+11.2f}%")
    print(f"  CAGR          : {cagr*100:>+11.2f}%")
    print(f"  Sharpe        : {sharpe:>12.2f}")
    print(f"  MDD           : {mdd*100:>+11.2f}%")
    print(f"  Buy & Hold    : {bh_return*100:>+11.2f}%")
    print(f"  vs BH         : {diff*100:>+11.2f}%p [{'WIN' if diff > 0 else 'LOSE'}]")
    print(f"  거래 횟수     : {len(trades)} (승률 {win_rate*100:.1f}%)")
    if trades:
        print(f"  평균 수익거래 : {avg_win*100:+.2f}% | 평균 손실거래: {avg_loss*100:+.2f}%")
        print(f"  평균 보유일수 : {avg_days:.1f}일")
        print(f"  체제 분포 (거래 시점): BULL {by_regime[mr.REGIME_BULL]}, "
              f"RANGE {by_regime[mr.REGIME_RANGE]}, BEAR {by_regime[mr.REGIME_BEAR]}")
        print(f"  청산 사유: {dict(sorted(reasons.items(), key=lambda x: -x[1]))}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("symbol", nargs="?")
    parser.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    args = parser.parse_args()

    symbols = [args.symbol] if args.symbol else DEFAULT_UNIVERSE
    print(f"백테스트: {symbols}")

    for sym in symbols:
        df = load_symbol(sym)
        if df is None:
            print(f"  [경고] {sym}: 데이터 없음")
            continue
        state = run_backtest(df, args.capital)
        print_summary(sym, state, df, args.capital)

    return 0


if __name__ == "__main__":
    sys.exit(main())
