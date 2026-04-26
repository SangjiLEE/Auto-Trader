"""
빠른 스윙(1~2일 보유) 전략 백테스트.

기존 swing_backtest 와 동일 구조, 다른 점:
  - swing_strategy_fast 사용 (4 AND / 5 OR)
  - Position 에 initial_atr 추적 (ATR 기반 동적 손절)
  - 더 짧은 보유, 더 작은 손절폭

실행:
  python -m src.swing_backtest_fast NVDA              # 단일 종목
  python -m src.swing_backtest_fast TSLA --capital 5000000
  python -m src.swing_backtest_fast                   # 기본 유니버스 (NVDA, TSLA)
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

import pandas as pd

from . import db
from . import indicators
from . import swing_strategy_fast as fast

# 빠른 스윙 백테스트 설정
INITIAL_CAPITAL = 5_000_000   # 500만원 (단타 슬롯 가정)
POSITION_PCT = 1.00           # 단일 종목 백테스트는 100% 투입 (집중)
MAX_POSITIONS = 1             # 빠른 스윙은 1포지션 집중
COOLDOWN_DAYS = 2             # 빠른 회복
DAILY_LOSS_LIMIT = -0.05      # 일일 -5%
MONTHLY_LOSS_LIMIT = -0.15    # 월간 -15%
LOSING_STREAK_LIMIT = 4       # 4연속 손실
COMMISSION = 0.003

DEFAULT_UNIVERSE = ["NVDA", "TSLA"]


@dataclass
class BacktestState:
    cash: float
    positions: dict[str, fast.Position] = field(default_factory=dict)
    completed_trades: list[dict] = field(default_factory=list)
    equity_curve: dict[pd.Timestamp, float] = field(default_factory=dict)
    cooldown_until: pd.Timestamp | None = None
    last_n_results: list[bool] = field(default_factory=list)
    last_exit_per_symbol: dict[str, pd.Timestamp] = field(default_factory=dict)


def compute_equity(
    state: BacktestState, prices_today: dict[str, float]
) -> float:
    total = state.cash
    for sym, pos in state.positions.items():
        price = prices_today.get(sym, pos.entry_price)
        total += pos.qty * price
    return total


def enter_position(
    state: BacktestState,
    symbol: str,
    date: pd.Timestamp,
    price: float,
    equity: float,
    initial_atr: float,
) -> None:
    alloc = equity * POSITION_PCT
    qty = int(alloc / price)
    if qty <= 0:
        return
    cost = qty * price * (1 + COMMISSION / 2)
    if cost > state.cash:
        qty = int(state.cash / (price * (1 + COMMISSION / 2)))
        if qty <= 0:
            return
        cost = qty * price * (1 + COMMISSION / 2)

    state.cash -= cost
    state.positions[symbol] = fast.Position(
        entry_date=date,
        entry_price=price,
        qty=qty,
        initial_atr=initial_atr,
    )


def exit_position(
    state: BacktestState,
    symbol: str,
    date: pd.Timestamp,
    price: float,
    reason: str,
) -> None:
    position = state.positions.pop(symbol)
    proceeds = position.qty * price * (1 - COMMISSION / 2)
    state.cash += proceeds

    pnl = proceeds - (position.qty * position.entry_price * (1 + COMMISSION / 2))
    pnl_pct = pnl / (position.qty * position.entry_price)
    is_win = pnl > 0

    state.completed_trades.append({
        "symbol": symbol,
        "entry_date": position.entry_date,
        "exit_date": date,
        "entry_price": position.entry_price,
        "exit_price": price,
        "qty": position.qty,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "reason": reason,
        "days_held": (date - position.entry_date).days,
    })

    state.last_exit_per_symbol[symbol] = date
    state.last_n_results.append(is_win)
    state.last_n_results = state.last_n_results[-LOSING_STREAK_LIMIT:]
    if len(state.last_n_results) == LOSING_STREAK_LIMIT and not any(state.last_n_results):
        state.cooldown_until = date + pd.Timedelta(days=COOLDOWN_DAYS * 2)
        state.last_n_results = []


def run_backtest(
    symbol_data: dict[str, pd.DataFrame],
    initial_capital: float = INITIAL_CAPITAL,
) -> BacktestState:
    common_dates = None
    for df in symbol_data.values():
        if common_dates is None:
            common_dates = set(df.index)
        else:
            common_dates &= set(df.index)
    if not common_dates:
        return BacktestState(cash=initial_capital)

    dates = sorted(common_dates)
    state = BacktestState(cash=initial_capital)

    prev_equity = initial_capital
    month_start_equity = initial_capital
    current_month = dates[0].to_period("M")

    for date in dates:
        prices_today = {
            sym: float(df.loc[date, "close"])
            for sym, df in symbol_data.items()
            if date in df.index and pd.notna(df.loc[date, "close"])
        }

        this_month = date.to_period("M")
        if this_month != current_month:
            current_month = this_month
            month_start_equity = prev_equity

        # 청산
        for sym in list(state.positions.keys()):
            if sym not in symbol_data:
                continue
            df = symbol_data[sym]
            if date not in df.index:
                continue
            row = df.loc[date]
            position = state.positions[sym]
            ex = fast.check_exit(row, position, date)
            if ex.should_exit:
                exit_position(state, sym, date, float(row["close"]), ex.reason)

        today_equity = compute_equity(state, prices_today)

        # 리스크 한도 체크
        block_new = False
        if state.cooldown_until is not None and date < state.cooldown_until:
            block_new = True
        elif prev_equity > 0 and (today_equity - prev_equity) / prev_equity <= DAILY_LOSS_LIMIT:
            block_new = True
        elif month_start_equity > 0 and (today_equity - month_start_equity) / month_start_equity <= MONTHLY_LOSS_LIMIT:
            block_new = True

        # 진입
        if not block_new and len(state.positions) < MAX_POSITIONS:
            for sym in symbol_data:
                if sym in state.positions:
                    continue
                if len(state.positions) >= MAX_POSITIONS:
                    break
                # 재진입 쿨다운
                last_exit = state.last_exit_per_symbol.get(sym)
                if last_exit and (date - last_exit).days < fast.REENTRY_COOLDOWN_DAYS:
                    continue
                df = symbol_data[sym]
                prev_idx = df.index.get_loc(date) - 1 if date in df.index else -1
                if prev_idx < 0:
                    continue
                prev_row = df.iloc[prev_idx]
                sig = fast.check_entry(prev_row)
                if sig.valid:
                    price = float(df.loc[date, "close"])
                    atr_val = df.iloc[prev_idx].get("atr14")
                    init_atr = float(atr_val) if pd.notna(atr_val) else 0.0
                    enter_position(state, sym, date, price, today_equity, init_atr)

        end_eq = compute_equity(state, prices_today)
        state.equity_curve[date] = end_eq
        prev_equity = end_eq

    # 마지막 강제 청산
    if state.positions and dates:
        last_date = dates[-1]
        for sym in list(state.positions.keys()):
            if sym in symbol_data and last_date in symbol_data[sym].index:
                price = float(symbol_data[sym].loc[last_date, "close"])
                exit_position(state, sym, last_date, price, "백테스트 종료")

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


def print_summary(symbol: str, state: BacktestState, initial: float) -> None:
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
    sharpe = 0.0
    if returns.std() > 0:
        sharpe = float((returns.mean() * 252) / (returns.std() * (252 ** 0.5)))
    running_max = eq.cummax()
    mdd = float(((eq - running_max) / running_max).min())

    trades = state.completed_trades
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / n if n else 0
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    avg_hold = sum(t["days_held"] for t in trades) / n if n else 0

    # Buy and Hold 비교
    bh_eq = symbol_data_global[symbol]["close"]
    bh_return = float(bh_eq.iloc[-1] / bh_eq.iloc[0] - 1)

    # 청산 사유 분포
    reasons: dict[str, int] = {}
    for t in trades:
        key = t["reason"].split()[0] if t["reason"] else "?"
        reasons[key] = reasons.get(key, 0) + 1

    print("\n" + "=" * 64)
    print(f"빠른 스윙 백테스트: {symbol}")
    print("=" * 64)
    print(f"  기간         : {eq.index[0].date()} → {eq.index[-1].date()}")
    print(f"  초기 자본    : {initial:>15,.0f}")
    print(f"  최종 자본    : {final:>15,.0f}")
    print(f"  누적 수익    : {total_return*100:>+14.2f}%")
    print(f"  CAGR         : {cagr*100:>+14.2f}%")
    print(f"  Sharpe       : {sharpe:>15.2f}")
    print(f"  최대 낙폭    : {mdd*100:>+14.2f}%")
    print()
    print(f"  거래 횟수    : {n}")
    print(f"  승률         : {win_rate*100:.1f}% ({len(wins)} 승 {len(losses)} 패)")
    print(f"  평균 수익거래: {avg_win*100:+.2f}%")
    print(f"  평균 손실거래: {avg_loss*100:+.2f}%")
    print(f"  평균 보유일수: {avg_hold:.1f}일")
    print(f"  Buy & Hold   : {bh_return*100:+.2f}%  (단순 보유 비교)")
    vs_bh = total_return - bh_return
    print(f"  전략 vs BH   : {vs_bh*100:+.2f}%p [{'WIN' if vs_bh > 0 else 'LOSE'}]")
    print()
    if reasons:
        print("  청산 사유 분포:")
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    {reason:<14}: {count}")


def print_recent_trades(symbol: str, state: BacktestState, n: int = 10) -> None:
    trades = state.completed_trades
    if not trades:
        return
    print(f"\n{symbol} 최근 {min(n, len(trades))} 거래:")
    print(f"  {'진입':<12} {'청산':<12} {'수익':>8} {'일':>4} {'사유':<22}")
    for t in trades[-n:]:
        print(
            f"  {t['entry_date'].strftime('%Y-%m-%d'):<12} "
            f"{t['exit_date'].strftime('%Y-%m-%d'):<12} "
            f"{t['pnl_pct']*100:>+7.2f}% "
            f"{t['days_held']:>3}d "
            f"{t['reason']:<22}"
        )


symbol_data_global: dict[str, pd.DataFrame] = {}


def main() -> int:
    parser = argparse.ArgumentParser(description="빠른 스윙 백테스트")
    parser.add_argument("symbol", nargs="?", help="단일 종목 (생략 시 NVDA, TSLA 둘 다)")
    parser.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    parser.add_argument("--trades", type=int, default=15)
    args = parser.parse_args()

    symbols = [args.symbol] if args.symbol else DEFAULT_UNIVERSE

    global symbol_data_global
    symbol_data_global = {}
    for sym in symbols:
        df = load_symbol(sym)
        if df is None:
            print(f"  [경고] {sym}: 데이터 없음")
            continue
        symbol_data_global[sym] = df
        print(f"  {sym}: {len(df)}일 ({df.index[0].date()} → {df.index[-1].date()})")

    if not symbol_data_global:
        print("데이터 없음. python -m src.load_candles SYMBOL --market US 먼저.")
        return 1

    # 종목별 독립 백테스트 (단일 종목 100% 투입)
    for sym in symbols:
        if sym not in symbol_data_global:
            continue
        single_data = {sym: symbol_data_global[sym]}
        state = run_backtest(single_data, initial_capital=args.capital)
        print_summary(sym, state, args.capital)
        print_recent_trades(sym, state, args.trades)

    return 0


if __name__ == "__main__":
    sys.exit(main())
