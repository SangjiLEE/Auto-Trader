"""
Enhanced Swing v2 백테스트 + 시장 체제별 분석.

기능:
  - 부분 진입 / 부분 청산 / DCA / 트레일링 / 재진입
  - 각 거래일에 시장 체제 (BULL / BEAR / RANGE) 라벨링
  - 거래는 진입일 시점의 체제로 분류
  - 체제별 수익률 / 승률 / 거래수 분리 출력

실행:
  python -m src.swing_backtest_v2 NVDA
  python -m src.swing_backtest_v2          # 기본 4종목 (NVDA, TSLA, AAPL, 069500)
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

import pandas as pd

from . import db
from . import indicators
from . import market_regime as mr
from . import swing_strategy_v2 as v2

INITIAL_CAPITAL = 5_000_000
COMMISSION = 0.003
INITIAL_BUY_RATIO = 0.50    # 1차 매수: 슬롯의 50%
DCA_BUY_RATIO = 0.50        # DCA 매수: 슬롯의 50% 추가 (총 100% 까지)

DEFAULT_UNIVERSE = ["NVDA", "TSLA", "AAPL", "069500"]


@dataclass
class CompletedTrade:
    symbol: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float    # 가중평균 청산가
    qty: int
    pnl: float
    pnl_pct: float
    days_held: int
    regime_at_entry: str
    n_partial_sells: int  # 부분매도 횟수
    dca_done: bool
    final_reason: str


@dataclass
class BacktestStateV2:
    cash: float
    position: v2.PositionV2 | None = None  # 단일 포지션 백테스트
    completed_trades: list[CompletedTrade] = field(default_factory=list)
    equity_curve: dict[pd.Timestamp, float] = field(default_factory=dict)
    last_exit_price: float = 0.0
    last_exit_date: pd.Timestamp | None = None
    # 진행 중 포지션의 부분매도 결과 누적
    partial_sells: list[dict] = field(default_factory=list)  # for current position only


def equity(state: BacktestStateV2, price_today: float) -> float:
    qty = state.position.qty if state.position else 0
    return state.cash + qty * price_today


def execute_buy(
    state: BacktestStateV2,
    qty: int,
    price: float,
    date: pd.Timestamp,
) -> bool:
    """초기 매수 또는 DCA. cash 부족 시 사이즈 자동 축소."""
    cost = qty * price * (1 + COMMISSION / 2)
    if cost > state.cash:
        qty = int(state.cash / (price * (1 + COMMISSION / 2)))
        if qty <= 0:
            return False
        cost = qty * price * (1 + COMMISSION / 2)

    state.cash -= cost

    if state.position is None:
        state.position = v2.PositionV2(
            qty=qty,
            avg_price=price,
            entry_date=date,
            initial_qty=qty,
            peak_price=price,
        )
    else:
        # DCA: 평단 갱신
        new_qty = state.position.qty + qty
        new_avg = (
            state.position.qty * state.position.avg_price + qty * price
        ) / new_qty
        state.position.qty = new_qty
        state.position.avg_price = new_avg
        state.position.dca_done = True
    return True


def execute_partial_sell(
    state: BacktestStateV2,
    qty: int,
    price: float,
    date: pd.Timestamp,
    reason: str,
) -> None:
    if state.position is None or qty <= 0 or qty > state.position.qty:
        return
    proceeds = qty * price * (1 - COMMISSION / 2)
    state.cash += proceeds

    pnl = (price - state.position.avg_price) * qty
    state.partial_sells.append({
        "date": date,
        "qty": qty,
        "price": price,
        "pnl": pnl,
        "reason": reason,
    })
    state.position.qty -= qty


def execute_full_sell(
    state: BacktestStateV2,
    price: float,
    date: pd.Timestamp,
    reason: str,
    regime_at_entry: str,
) -> None:
    if state.position is None:
        return
    pos = state.position
    proceeds = pos.qty * price * (1 - COMMISSION / 2)
    state.cash += proceeds

    # 종합 손익 계산: 부분 매도 + 최종 매도 - 매입원금
    final_pnl = (price - pos.avg_price) * pos.qty
    partial_pnl = sum(s["pnl"] for s in state.partial_sells)
    total_pnl = partial_pnl + final_pnl

    initial_cost = (
        pos.initial_qty * pos.avg_price  # 단순화 (DCA가 평단 바꿨으므로 근사)
    )
    if pos.dca_done:
        # DCA 후 총 투입자본은 (initial_qty + DCA qty) * avg_price
        # 단순화: avg_price 기준 전체 cost
        full_cost = (pos.initial_qty * 2) * pos.avg_price
    else:
        full_cost = pos.initial_qty * pos.avg_price

    pnl_pct = total_pnl / full_cost if full_cost > 0 else 0

    # 가중평균 청산가
    total_qty_sold = sum(s["qty"] for s in state.partial_sells) + pos.qty
    total_proceeds = sum(s["qty"] * s["price"] for s in state.partial_sells) + pos.qty * price
    weighted_exit = total_proceeds / total_qty_sold if total_qty_sold > 0 else price

    state.completed_trades.append(CompletedTrade(
        symbol="",  # 호출자가 채움
        entry_date=pos.entry_date,
        exit_date=date,
        entry_price=pos.avg_price,
        exit_price=weighted_exit,
        qty=pos.initial_qty * (2 if pos.dca_done else 1),
        pnl=total_pnl,
        pnl_pct=pnl_pct,
        days_held=(date - pos.entry_date).days,
        regime_at_entry=regime_at_entry,
        n_partial_sells=len(state.partial_sells),
        dca_done=pos.dca_done,
        final_reason=reason,
    ))

    # 상태 리셋
    state.last_exit_price = price
    state.last_exit_date = date
    state.position = None
    state.partial_sells = []


def can_reenter(state: BacktestStateV2, current_date: pd.Timestamp, current_price: float) -> bool:
    """재진입 조건:
    - 청산 후 N일 (쿨다운) 경과
    - AND ( 직전 청산가 -2% 이상 하락  OR  쿨다운 × 2.5 이상 시간 경과 )

    트렌드 시장에서 가격이 안 떨어져도 일정 시간 지나면 재진입 가능.
    """
    if state.last_exit_date is None:
        return True
    days_since = (current_date - state.last_exit_date).days
    if days_since < v2.REENTRY_COOLDOWN_DAYS:
        return False
    if state.last_exit_price <= 0:
        return True
    drop = (current_price - state.last_exit_price) / state.last_exit_price
    if drop <= v2.REENTRY_PRICE_DROP:
        return True
    # 충분히 시간 지났으면 가격 무관 재진입 허용 (트렌드 시장 대응)
    if days_since >= v2.REENTRY_COOLDOWN_DAYS * 2.5:
        return True
    return False


def run_backtest(
    df: pd.DataFrame,
    initial_capital: float = INITIAL_CAPITAL,
) -> BacktestStateV2:
    """단일 종목 v2 백테스트."""
    state = BacktestStateV2(cash=initial_capital)

    # 진입 시점에 잡힌 체제 추적 (전역으로 1개)
    current_entry_regime: str = mr.REGIME_RANGE

    dates = df.index.tolist()
    for i, date in enumerate(dates):
        row = df.iloc[i]
        price = float(row["close"]) if pd.notna(row["close"]) else None
        if price is None:
            state.equity_curve[date] = equity(state, state.position.avg_price if state.position else 0)
            continue

        # 청산 / DCA / 진입 로직
        if state.position is not None:
            # 1. 청산 체크
            actions = v2.check_exit_v2(row, state.position, date)
            if actions:
                for action in actions:
                    if action.type == "SELL_PARTIAL":
                        execute_partial_sell(state, action.qty, price, date, action.reason)
                    elif action.type == "SELL_ALL":
                        execute_full_sell(state, price, date, action.reason, current_entry_regime)
                        # 포지션 완전 청산됨; 다음 사이클에서 재진입 검토
                        break
            # 2. 청산 후에도 포지션 남아있으면 DCA 검토
            if state.position is not None:
                dca = v2.check_dca(row, state.position)
                if dca is not None:
                    execute_buy(state, dca.qty, price, date)
        else:
            # 포지션 없음 → 진입 검토
            # look-ahead 방지: 어제 종가 시그널
            if i >= 1:
                prev_row = df.iloc[i - 1]
                signal = v2.check_entry(prev_row)
                if signal.valid and can_reenter(state, date, price):
                    # 슬롯 50% 만큼 수량 계산
                    cash_available = state.cash
                    target_alloc = cash_available * INITIAL_BUY_RATIO
                    qty = int(target_alloc / price)
                    if qty > 0:
                        execute_buy(state, qty, price, date)
                        # 진입 시점 체제 기록
                        current_entry_regime = mr.detect_regime(row)

        # 자본 곡선
        state.equity_curve[date] = equity(state, price)

    # 마지막 강제 청산
    if state.position is not None and dates:
        last_date = dates[-1]
        last_price = float(df.iloc[-1]["close"])
        execute_full_sell(state, last_price, last_date, "백테스트 종료", current_entry_regime)

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
    return {
        "final": final,
        "total_return": total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "mdd": mdd,
    }


def regime_breakdown(state: BacktestStateV2) -> dict:
    """체제별 거래 통계."""
    by_regime: dict[str, list[CompletedTrade]] = {
        mr.REGIME_BULL: [],
        mr.REGIME_BEAR: [],
        mr.REGIME_RANGE: [],
    }
    for t in state.completed_trades:
        if t.regime_at_entry in by_regime:
            by_regime[t.regime_at_entry].append(t)

    result = {}
    for regime, trades in by_regime.items():
        if not trades:
            result[regime] = {
                "n_trades": 0,
                "win_rate": 0,
                "avg_pnl_pct": 0,
                "total_pnl_pct": 0,
                "avg_win": 0,
                "avg_loss": 0,
                "avg_days": 0,
            }
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


def print_summary(symbol: str, state: BacktestStateV2, df: pd.DataFrame, initial: float) -> None:
    metrics = compute_metrics(state.equity_curve, initial)
    if not metrics:
        print(f"{symbol}: 결과 없음")
        return

    bh_return = float(df["close"].iloc[-1] / df["close"].iloc[0] - 1)
    diff = metrics["total_return"] - bh_return

    # 체제 분포
    dist = mr.regime_distribution(df)
    breakdown = regime_breakdown(state)

    print("\n" + "=" * 80)
    print(f"Enhanced Swing v2 백테스트: {symbol}")
    print("=" * 80)
    print(f"  기간          : {df.index[0].date()} → {df.index[-1].date()} ({len(df)}일)")
    print(f"  초기 자본     : {initial:>12,.0f}")
    print(f"  최종 자본     : {metrics['final']:>12,.0f}")
    print(f"  누적 수익     : {metrics['total_return']*100:>+11.2f}%")
    print(f"  CAGR          : {metrics['cagr']*100:>+11.2f}%")
    print(f"  Sharpe        : {metrics['sharpe']:>12.2f}")
    print(f"  MDD           : {metrics['mdd']*100:>+11.2f}%")
    print(f"  Buy & Hold    : {bh_return*100:>+11.2f}%")
    print(f"  vs BH         : {diff*100:>+11.2f}%p [{'WIN' if diff > 0 else 'LOSE'}]")
    print()
    print(f"  거래 횟수     : {len(state.completed_trades)}")
    if state.completed_trades:
        wins = [t for t in state.completed_trades if t.pnl > 0]
        win_rate = len(wins) / len(state.completed_trades)
        avg_days = sum(t.days_held for t in state.completed_trades) / len(state.completed_trades)
        partial_count = sum(1 for t in state.completed_trades if t.n_partial_sells > 0)
        dca_count = sum(1 for t in state.completed_trades if t.dca_done)
        print(f"  승률          : {win_rate*100:.1f}%")
        print(f"  평균 보유일수 : {avg_days:.1f}일")
        print(f"  부분익절 거래 : {partial_count}/{len(state.completed_trades)}")
        print(f"  DCA 적용 거래 : {dca_count}/{len(state.completed_trades)}")

    # 체제 분포
    print()
    print("  시장 체제 분포 (전 기간):")
    print(f"    BULL  : {dist[mr.REGIME_BULL]*100:>5.1f}%")
    print(f"    BEAR  : {dist[mr.REGIME_BEAR]*100:>5.1f}%")
    print(f"    RANGE : {dist[mr.REGIME_RANGE]*100:>5.1f}%")

    # 체제별 거래 결과
    print("\n  체제별 거래 결과:")
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
    parser = argparse.ArgumentParser(description="Enhanced Swing v2 백테스트")
    parser.add_argument("symbol", nargs="?")
    parser.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    args = parser.parse_args()

    symbols = [args.symbol] if args.symbol else DEFAULT_UNIVERSE
    print(f"백테스트 대상: {symbols}\n")

    for sym in symbols:
        df = load_symbol(sym)
        if df is None:
            print(f"  [경고] {sym}: 데이터 없음")
            continue
        print(f"  {sym}: {len(df)}일 ({df.index[0].date()} → {df.index[-1].date()})")
        state = run_backtest(df, initial_capital=args.capital)
        print_summary(sym, state, df, args.capital)

    return 0


if __name__ == "__main__":
    sys.exit(main())
