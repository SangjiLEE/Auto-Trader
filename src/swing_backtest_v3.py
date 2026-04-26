"""
Enhanced Swing v3 백테스트 — 체제별 어댑티브 룰.

진입 시점에 체제 감지 → 해당 체제의 파라미터로 포지션 운영.
BEAR 체제는 신규 진입 차단.

실행:
  python -m src.swing_backtest_v3
  python -m src.swing_backtest_v3 NVDA
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

import pandas as pd

from . import db
from . import indicators
from . import market_regime as mr
from . import swing_strategy_v3 as v3

INITIAL_CAPITAL = 5_000_000
COMMISSION = 0.003

DEFAULT_UNIVERSE = ["NVDA", "TSLA", "AAPL", "069500"]


@dataclass
class CompletedTrade:
    symbol: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: int
    pnl: float
    pnl_pct: float
    days_held: int
    regime_at_entry: str
    n_partial_sells: int
    dca_done: bool
    final_reason: str


@dataclass
class BacktestStateV3:
    cash: float
    position: v3.PositionV3 | None = None
    completed_trades: list[CompletedTrade] = field(default_factory=list)
    equity_curve: dict[pd.Timestamp, float] = field(default_factory=dict)
    last_exit_price: float = 0.0
    last_exit_date: pd.Timestamp | None = None
    last_exit_regime: str = mr.REGIME_RANGE
    partial_sells: list[dict] = field(default_factory=list)
    blocked_by_bear: int = 0  # BEAR 차단으로 진입 안 한 카운트


def equity(state: BacktestStateV3, price: float) -> float:
    qty = state.position.qty if state.position else 0
    return state.cash + qty * price


def execute_buy(
    state: BacktestStateV3, qty: int, price: float, date: pd.Timestamp,
    regime: str | None = None,
) -> bool:
    cost = qty * price * (1 + COMMISSION / 2)
    if cost > state.cash:
        qty = int(state.cash / (price * (1 + COMMISSION / 2)))
        if qty <= 0:
            return False
        cost = qty * price * (1 + COMMISSION / 2)

    state.cash -= cost

    if state.position is None:
        state.position = v3.PositionV3(
            qty=qty, avg_price=price, entry_date=date,
            initial_qty=qty, peak_price=price,
            entry_regime=regime or mr.REGIME_RANGE,
        )
    else:
        new_qty = state.position.qty + qty
        new_avg = (
            state.position.qty * state.position.avg_price + qty * price
        ) / new_qty
        state.position.qty = new_qty
        state.position.avg_price = new_avg
        state.position.dca_done = True
    return True


def execute_partial_sell(
    state: BacktestStateV3, qty: int, price: float, date: pd.Timestamp, reason: str,
) -> None:
    if state.position is None or qty <= 0 or qty > state.position.qty:
        return
    state.cash += qty * price * (1 - COMMISSION / 2)
    pnl = (price - state.position.avg_price) * qty
    state.partial_sells.append({
        "date": date, "qty": qty, "price": price, "pnl": pnl, "reason": reason,
    })
    state.position.qty -= qty


def execute_full_sell(
    state: BacktestStateV3, price: float, date: pd.Timestamp, reason: str,
) -> None:
    if state.position is None:
        return
    pos = state.position
    state.cash += pos.qty * price * (1 - COMMISSION / 2)

    final_pnl = (price - pos.avg_price) * pos.qty
    partial_pnl = sum(s["pnl"] for s in state.partial_sells)
    total_pnl = partial_pnl + final_pnl

    full_cost = (pos.initial_qty * (2 if pos.dca_done else 1)) * pos.avg_price
    pnl_pct = total_pnl / full_cost if full_cost > 0 else 0

    total_qty_sold = sum(s["qty"] for s in state.partial_sells) + pos.qty
    total_proceeds = sum(s["qty"] * s["price"] for s in state.partial_sells) + pos.qty * price
    weighted_exit = total_proceeds / total_qty_sold if total_qty_sold > 0 else price

    state.completed_trades.append(CompletedTrade(
        symbol="", entry_date=pos.entry_date, exit_date=date,
        entry_price=pos.avg_price, exit_price=weighted_exit,
        qty=pos.initial_qty * (2 if pos.dca_done else 1),
        pnl=total_pnl, pnl_pct=pnl_pct,
        days_held=(date - pos.entry_date).days,
        regime_at_entry=pos.entry_regime,
        n_partial_sells=len(state.partial_sells),
        dca_done=pos.dca_done, final_reason=reason,
    ))

    state.last_exit_price = price
    state.last_exit_date = date
    state.last_exit_regime = pos.entry_regime
    state.position = None
    state.partial_sells = []


def can_reenter(
    state: BacktestStateV3, current_date: pd.Timestamp,
    current_price: float, current_regime: str,
) -> bool:
    """체제별 재진입 쿨다운."""
    params = v3.get_params(current_regime)
    if params.get("block_entry"):
        return False
    cooldown = params.get("reentry_cooldown_days", 3)

    if state.last_exit_date is None:
        return True
    days_since = (current_date - state.last_exit_date).days
    if days_since < cooldown:
        return False
    if state.last_exit_price <= 0:
        return True
    drop = (current_price - state.last_exit_price) / state.last_exit_price
    if drop <= -0.02:
        return True
    if days_since >= cooldown * 2.5:
        return True
    return False


def run_backtest(
    df: pd.DataFrame,
    initial_capital: float = INITIAL_CAPITAL,
    execution_lag: int = 1,
) -> BacktestStateV3:
    """
    [B5 fix] 백테스트-라이브 timing alignment.

    execution_lag=1 (기본): D 일 close 로 시그널 평가 → (D+1) open 으로 매매
       라이브 (어제 close 데이터로 오늘 09:20 시초 매매) 와 1:1 align
    execution_lag=0 (legacy): D close 시그널 + D close 매매
       (look-ahead 의심, BACKTESTS.md 결과 재현용으로만)

    Equity curve 는 항상 close 기준 (mark-to-market) 유지.
    """
    state = BacktestStateV3(cash=initial_capital)
    dates = df.index.tolist()
    n = len(dates)

    for i, date in enumerate(dates):
        row = df.iloc[i]
        close = float(row["close"]) if pd.notna(row["close"]) else None
        if close is None:
            state.equity_curve[date] = equity(state, state.position.avg_price if state.position else 0)
            continue

        # equity curve = D close 기준 mark-to-market
        state.equity_curve[date] = equity(state, close)

        # 매매 가격 / 시점 결정
        trade_idx = i + execution_lag
        if execution_lag > 0:
            if trade_idx >= n:
                continue  # 다음 거래일 데이터 없음 → 매매 불가
            trade_row = df.iloc[trade_idx]
            t_open = trade_row.get("open")
            t_close = trade_row.get("close")
            if pd.isna(t_open) and pd.isna(t_close):
                continue
            trade_price = float(t_open) if pd.notna(t_open) else float(t_close)
            trade_date = dates[trade_idx]
        else:
            trade_price = close
            trade_date = date

        # === D close 로 모든 시그널 평가 ===
        # 청산 + 부분익절 + 트레일링 + DCA
        if state.position is not None:
            actions = v3.check_exit_v3(row, state.position, date)
            if actions:
                for action in actions:
                    if action.type == "SELL_PARTIAL":
                        execute_partial_sell(state, action.qty, trade_price, trade_date, action.reason)
                    elif action.type == "SELL_ALL":
                        execute_full_sell(state, trade_price, trade_date, action.reason)
                        break

            if state.position is not None:
                dca = v3.check_dca(row, state.position)
                if dca is not None:
                    execute_buy(state, dca.qty, trade_price, trade_date)
        else:
            # 진입 검토 — D close 의 indicators 사용 (look-ahead 없음, D 종가는 D 종료 시점에 알 수 있음)
            regime_today = mr.detect_regime(row)
            params = v3.get_params(regime_today)

            if params.get("block_entry"):
                state.blocked_by_bear += 1
            else:
                signal = v3.check_entry(row, regime_today)
                if signal.valid and can_reenter(state, trade_date, trade_price, regime_today):
                    ratio = params["initial_buy_ratio"]
                    target_alloc = state.cash * ratio
                    qty = int(target_alloc / trade_price)
                    if qty > 0:
                        execute_buy(state, qty, trade_price, trade_date, regime=regime_today)

    # 백테스트 끝 잔여 포지션 강제 청산 (마지막 close)
    if state.position is not None and dates:
        last_date = dates[-1]
        last_price = float(df.iloc[-1]["close"])
        execute_full_sell(state, last_price, last_date, "백테스트 종료")

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


def compute_metrics(equity_curve: dict, initial: float) -> dict:
    eq = pd.Series(equity_curve)
    if eq.empty:
        return {}
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
    return {"final": final, "total_return": total_return, "cagr": cagr,
            "sharpe": sharpe, "mdd": mdd}


def regime_breakdown(state: BacktestStateV3) -> dict:
    by_regime: dict[str, list[CompletedTrade]] = {
        mr.REGIME_BULL: [], mr.REGIME_BEAR: [], mr.REGIME_RANGE: [],
    }
    for t in state.completed_trades:
        if t.regime_at_entry in by_regime:
            by_regime[t.regime_at_entry].append(t)

    result = {}
    for regime, trades in by_regime.items():
        if not trades:
            result[regime] = {"n_trades": 0, "win_rate": 0, "avg_pnl_pct": 0,
                              "total_pnl_pct": 0, "avg_win": 0, "avg_loss": 0,
                              "avg_days": 0}
            continue
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        result[regime] = {
            "n_trades": len(trades),
            "win_rate": len(wins) / len(trades),
            "avg_pnl_pct": sum(t.pnl_pct for t in trades) / len(trades),
            "total_pnl_pct": sum(t.pnl_pct for t in trades),
            "avg_win": sum(t.pnl_pct for t in wins) / len(wins) if wins else 0,
            "avg_loss": sum(t.pnl_pct for t in losses) / len(losses) if losses else 0,
            "avg_days": sum(t.days_held for t in trades) / len(trades),
        }
    return result


def print_summary(symbol: str, state: BacktestStateV3, df: pd.DataFrame, initial: float) -> None:
    metrics = compute_metrics(state.equity_curve, initial)
    if not metrics:
        print(f"{symbol}: 결과 없음")
        return

    bh_return = float(df["close"].iloc[-1] / df["close"].iloc[0] - 1)
    diff = metrics["total_return"] - bh_return

    dist = mr.regime_distribution(df)
    breakdown = regime_breakdown(state)

    print("\n" + "=" * 80)
    print(f"Enhanced Swing v3 (체제별 어댑티브) 백테스트: {symbol}")
    print("=" * 80)
    print(f"  기간          : {df.index[0].date()} → {df.index[-1].date()}")
    print(f"  초기/최종     : {initial:>12,.0f} → {metrics['final']:>12,.0f}")
    print(f"  누적 수익     : {metrics['total_return']*100:>+11.2f}%")
    print(f"  CAGR          : {metrics['cagr']*100:>+11.2f}%")
    print(f"  Sharpe        : {metrics['sharpe']:>12.2f}")
    print(f"  MDD           : {metrics['mdd']*100:>+11.2f}%")
    print(f"  Buy & Hold    : {bh_return*100:>+11.2f}%")
    print(f"  vs BH         : {diff*100:>+11.2f}%p [{'WIN' if diff > 0 else 'LOSE'}]")
    print(f"  거래 횟수     : {len(state.completed_trades)}")
    if state.blocked_by_bear:
        print(f"  BEAR 차단일수 : {state.blocked_by_bear}")
    print()
    print("  시장 체제 분포:")
    print(f"    BULL  : {dist[mr.REGIME_BULL]*100:>5.1f}%")
    print(f"    BEAR  : {dist[mr.REGIME_BEAR]*100:>5.1f}%")
    print(f"    RANGE : {dist[mr.REGIME_RANGE]*100:>5.1f}%")

    print("\n  체제별 거래 (체제별 어댑티브 룰 적용):")
    print(f"  {'체제':<8} {'거래':>5} {'승률':>7} {'평균수익':>10} {'평균승':>9} {'평균패':>9} {'평균보유':>8}")
    print("  " + "-" * 65)
    for regime in [mr.REGIME_BULL, mr.REGIME_RANGE, mr.REGIME_BEAR]:
        b = breakdown[regime]
        if b["n_trades"] == 0:
            print(f"  {regime:<8} {0:>5}    -        -         -         -        -")
            continue
        print(
            f"  {regime:<8} {b['n_trades']:>5} "
            f"{b['win_rate']*100:>6.1f}% "
            f"{b['avg_pnl_pct']*100:>+9.2f}% "
            f"{b['avg_win']*100:>+8.2f}% "
            f"{b['avg_loss']*100:>+8.2f}% "
            f"{b['avg_days']:>7.1f}d"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Enhanced Swing v3 백테스트")
    parser.add_argument("symbol", nargs="?")
    parser.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    args = parser.parse_args()

    symbols = [args.symbol] if args.symbol else DEFAULT_UNIVERSE
    print(f"백테스트: {symbols}\n")

    for sym in symbols:
        df = load_symbol(sym)
        if df is None:
            print(f"  [경고] {sym}: 데이터 없음")
            continue
        print(f"  {sym}: {len(df)}일")
        state = run_backtest(df, initial_capital=args.capital)
        print_summary(sym, state, df, args.capital)

    return 0


if __name__ == "__main__":
    sys.exit(main())
