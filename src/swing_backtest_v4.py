"""
Enhanced Swing v4 백테스트 — v3 + F&G 극단 분할매매.

룰:
  - F&G ≤ 7 (Extreme Fear): 매일 cash의 25% 분할 매수 (정상 룰 무시)
  - F&G ≥ 88 (Extreme Greed) + 포지션 보유: 매일 보유의 25% 분할 매도
  - 그 외: 정상 v3 로직 (체제 어댑티브)

분할: 25% × 4일 = 100% 처리 (이론상). 실제론 F&G 극단이 1-3일 짧게 끝나는 경우가 많음.

실행:
  python -m src.swing_backtest_v4 NVDA
  python -m src.swing_backtest_v4
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

import pandas as pd

from . import db
from . import fear_greed
from . import indicators
from . import market_regime as mr
from . import metrics as mt
from . import swing_strategy_v3 as v3

INITIAL_CAPITAL = 5_000_000
COMMISSION = 0.003
# 옵션 B 완화: 매도 임계값 92로 더 극단만, 매도 비율 10%로 약하게, BULL 모드 매도 무시
SPLIT_BUY_RATIO = 0.25         # 매수 25% (그대로)
SPLIT_SELL_RATIO = 0.10        # 매도 10% (25% → 완화)
FG_EXTREME_FEAR_THRESHOLD = 7
FG_EXTREME_GREED_THRESHOLD = 92  # 88 → 92 (더 극단만)
SKIP_FG_SELL_IN_BULL = True    # BULL 모드 시 F&G 매도 무시

DEFAULT_UNIVERSE = ["AAPL", "NVDA", "TSLA", "069500", "005930"]


@dataclass
class PositionV4:
    qty: int
    avg_price: float
    entry_date: pd.Timestamp
    initial_qty: int
    peak_price: float
    entry_regime: str
    pf_t1_done: bool = False
    pf_t2_done: bool = False
    trailing_active: bool = False
    dca_done: bool = False
    fg_buys: int = 0   # F&G 분할매수 횟수
    fg_sells: int = 0  # F&G 분할매도 횟수


@dataclass
class CompletedTradeV4:
    symbol: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    pnl: float
    pnl_pct: float
    days_held: int
    regime_at_entry: str
    final_reason: str
    fg_buys: int
    fg_sells: int


@dataclass
class BacktestStateV4:
    cash: float
    position: PositionV4 | None = None
    completed_trades: list[CompletedTradeV4] = field(default_factory=list)
    equity_curve: dict[pd.Timestamp, float] = field(default_factory=dict)
    last_exit_price: float = 0.0
    last_exit_date: pd.Timestamp | None = None
    fg_extreme_fear_days: int = 0
    fg_extreme_greed_days: int = 0
    fg_buy_actions: int = 0
    fg_sell_actions: int = 0
    # [B6 fix] 정확한 P&L 회계용 — 현재 사이클의 cash flow 추적
    position_cost: float = 0.0       # 누적 매수 금액 (commission 포함, 현금 유출)
    position_proceeds: float = 0.0   # 누적 매도 금액 (commission 차감, 현금 유입)


def equity(state: BacktestStateV4, price: float) -> float:
    qty = state.position.qty if state.position else 0
    return state.cash + qty * price


def buy(state: BacktestStateV4, qty: int, price: float, date: pd.Timestamp, regime: str, fg_buy: bool) -> None:
    cost = qty * price * (1 + COMMISSION / 2)
    if cost > state.cash:
        qty = int(state.cash / (price * (1 + COMMISSION / 2)))
        if qty <= 0:
            return
        cost = qty * price * (1 + COMMISSION / 2)
    state.cash -= cost
    state.position_cost += cost  # B6: 정확한 P&L 회계

    if state.position is None:
        state.position = PositionV4(
            qty=qty, avg_price=price, entry_date=date,
            initial_qty=qty, peak_price=price,
            entry_regime=regime,
            fg_buys=1 if fg_buy else 0,
        )
    else:
        new_qty = state.position.qty + qty
        new_avg = (
            state.position.qty * state.position.avg_price + qty * price
        ) / new_qty
        state.position.qty = new_qty
        state.position.avg_price = new_avg
        if fg_buy:
            state.position.fg_buys += 1
        else:
            state.position.dca_done = True

    if fg_buy:
        state.fg_buy_actions += 1


def sell_partial(state: BacktestStateV4, qty: int, price: float, date: pd.Timestamp, reason: str, fg_sell: bool) -> None:
    if state.position is None or qty <= 0 or qty > state.position.qty:
        return
    proceeds = qty * price * (1 - COMMISSION / 2)
    state.cash += proceeds
    state.position_proceeds += proceeds  # B6: 정확한 P&L 회계
    state.position.qty -= qty
    if fg_sell:
        state.position.fg_sells += 1
        state.fg_sell_actions += 1
    if state.position.qty <= 0:
        # 완전 청산 처리 (cash 는 이미 +proceeds 됨)
        sell_full_finalize(state, price, date, reason, already_settled=True)


def sell_full_finalize(
    state: BacktestStateV4,
    price: float,
    date: pd.Timestamp,
    reason: str,
    already_settled: bool = False,
) -> None:
    """qty 0 도달 시 호출되는 마무리. completed_trades 기록.

    [B6 fix] 정확한 P&L:
      pnl     = position_proceeds - position_cost  (실측 cash flow)
      pnl_pct = pnl / position_cost
    이전엔 (price - avg_price) * initial_qty 로 근사 → 부분익절/DCA/F&G
    분할매수 후 매우 부정확. 이제 실측.
    """
    pos = state.position
    if pos is None:
        return

    # B6 fix: 실측 P&L (cash flow 기준)
    pnl = state.position_proceeds - state.position_cost
    pnl_pct = pnl / state.position_cost if state.position_cost > 0 else 0

    state.completed_trades.append(CompletedTradeV4(
        symbol="", entry_date=pos.entry_date, exit_date=date,
        entry_price=pos.avg_price, exit_price=price,
        pnl=pnl, pnl_pct=pnl_pct,
        days_held=(date - pos.entry_date).days,
        regime_at_entry=pos.entry_regime,
        final_reason=reason,
        fg_buys=pos.fg_buys,
        fg_sells=pos.fg_sells,
    ))
    state.last_exit_price = price
    state.last_exit_date = date
    state.position = None
    # 새 사이클 위해 회계 리셋
    state.position_cost = 0.0
    state.position_proceeds = 0.0


def sell_full(state: BacktestStateV4, price: float, date: pd.Timestamp, reason: str) -> None:
    if state.position is None:
        return
    qty = state.position.qty
    proceeds = qty * price * (1 - COMMISSION / 2)
    state.cash += proceeds
    state.position_proceeds += proceeds  # B6: 정확한 P&L 회계
    state.position.qty = 0
    sell_full_finalize(state, price, date, reason, already_settled=True)


def _prev_trading_day_fg(
    fg_history: dict[str, int],
    today: pd.Timestamp,
    max_lookback: int = 7,
    default: int = 50,
) -> int:
    """[B6 fix] D 일 매매 시 D-1 이전의 마지막으로 발표된 F&G 사용.

    F&G 는 alternative.me 에서 그 날 23:59 UTC 에 발표.
    D 일 close 시점 (KRX 06:30 UTC) 에는 D 일 F&G 아직 미발표.
    → D-1 (또는 그 이전 가용) 값 사용. 휴일 등으로 빠진 날 백오프.
    """
    for offset in range(1, max_lookback + 1):
        date_str = (today - pd.Timedelta(days=offset)).strftime("%Y-%m-%d")
        if date_str in fg_history:
            return fg_history[date_str]
    return default


def run_backtest(
    df: pd.DataFrame,
    fg_history: dict[str, int],
    initial_capital: float = INITIAL_CAPITAL,
    execution_lag: int = 1,
) -> BacktestStateV4:
    """
    [B5 + B6 fix] v4 백테스트.

    - F&G: D-1 이전 발표값 사용 (look-ahead 제거)
    - 매매: D close 시그널 → (D+execution_lag) open 매매 (라이브 align)
    - P&L: 실측 cash flow 기반 (sell_full_finalize)
    """
    state = BacktestStateV4(cash=initial_capital)
    dates = df.index.tolist()
    n = len(dates)

    for i, date in enumerate(dates):
        row = df.iloc[i]
        close = float(row["close"]) if pd.notna(row["close"]) else None
        if close is None:
            state.equity_curve[date] = equity(state, state.position.avg_price if state.position else 0)
            continue

        # equity = D close 기준 mark-to-market
        state.equity_curve[date] = equity(state, close)

        # B6 fix: D 일 매매 시 D-1 이전 F&G 사용 (look-ahead 제거)
        fg_value = _prev_trading_day_fg(fg_history, pd.Timestamp(date))

        # B5 fix: 매매 가격 = (D + execution_lag) open
        trade_idx = i + execution_lag
        if execution_lag > 0:
            if trade_idx >= n:
                continue
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

        # 1. F&G 극단 처리
        if fg_value <= FG_EXTREME_FEAR_THRESHOLD:
            state.fg_extreme_fear_days += 1
            cash_to_use = state.cash * SPLIT_BUY_RATIO
            qty = int(cash_to_use / trade_price) if trade_price > 0 else 0
            if qty > 0:
                regime = mr.detect_regime(row)
                buy(state, qty, trade_price, trade_date, regime, fg_buy=True)
            continue

        if fg_value >= FG_EXTREME_GREED_THRESHOLD:
            state.fg_extreme_greed_days += 1
            if state.position is not None:
                skip_fg_sell = (
                    SKIP_FG_SELL_IN_BULL
                    and state.position.entry_regime == mr.REGIME_BULL
                )
                if not skip_fg_sell:
                    qty_to_sell = max(int(state.position.qty * SPLIT_SELL_RATIO), 1) if state.position.qty > 0 else 0
                    if qty_to_sell > 0:
                        sell_partial(
                            state, qty_to_sell, trade_price, trade_date,
                            f"F&G {fg_value} 극탐 분할매도", fg_sell=True,
                        )
            continue

        # 2. 정상 v3 로직 (D close 시그널 평가, trade_price 매매)
        if state.position is not None:
            tmp_pos = v3.PositionV3(
                qty=state.position.qty, avg_price=state.position.avg_price,
                entry_date=state.position.entry_date,
                initial_qty=state.position.initial_qty,
                peak_price=state.position.peak_price,
                entry_regime=state.position.entry_regime,
                pf_t1_done=state.position.pf_t1_done,
                pf_t2_done=state.position.pf_t2_done,
                trailing_active=state.position.trailing_active,
                dca_done=state.position.dca_done,
            )
            actions = v3.check_exit_v3(row, tmp_pos, date)
            # 상태 동기화 (peak/플래그)
            state.position.peak_price = tmp_pos.peak_price
            state.position.pf_t1_done = tmp_pos.pf_t1_done
            state.position.pf_t2_done = tmp_pos.pf_t2_done
            state.position.trailing_active = tmp_pos.trailing_active

            if actions:
                for action in actions:
                    if action.type == "SELL_PARTIAL":
                        sell_partial(state, action.qty, trade_price, trade_date, action.reason, fg_sell=False)
                    elif action.type == "SELL_ALL":
                        sell_full(state, trade_price, trade_date, action.reason)
                        break

            if state.position is not None:
                dca = v3.check_dca(row, tmp_pos)
                if dca is not None:
                    buy(state, dca.qty, trade_price, trade_date, state.position.entry_regime, fg_buy=False)
        else:
            # v3 진입 체크 — D close 의 indicators 사용
            regime_today = mr.detect_regime(row)
            params = v3.get_params(regime_today)
            if not params.get("block_entry"):
                sig = v3.check_entry(row, regime_today)
                if sig.valid:
                    # 재진입 쿨다운
                    ok_to_reenter = True
                    if state.last_exit_date is not None:
                        cooldown = params.get("reentry_cooldown_days", 3)
                        days_since = (trade_date - state.last_exit_date).days
                        if days_since < cooldown:
                            ok_to_reenter = False
                    if ok_to_reenter:
                        ratio = params["initial_buy_ratio"]
                        target_alloc = state.cash * ratio
                        qty = int(target_alloc / trade_price)
                        if qty > 0:
                            buy(state, qty, trade_price, trade_date, regime_today, fg_buy=False)

    # 백테스트 끝 잔여 포지션 강제 청산 (마지막 close)
    if state.position is not None and dates:
        last_date = dates[-1]
        last_price = float(df.iloc[-1]["close"])
        sell_full(state, last_price, last_date, "백테스트 종료")

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


def print_summary(symbol: str, state: BacktestStateV4, df: pd.DataFrame, initial: float) -> None:
    if not state.equity_curve:
        print(f"{symbol}: 결과 없음")
        return

    # [Phase 1] 새 metrics
    m = mt.compute_metrics(state.equity_curve, initial_capital=initial)

    bh_return = float(df["close"].iloc[-1] / df["close"].iloc[0] - 1)
    diff = m.total_return - bh_return

    trades = state.completed_trades
    wins = [t for t in trades if t.pnl > 0]
    win_rate = len(wins) / len(trades) if trades else 0

    print("\n" + "=" * 78)
    print(f"v4 (v3 + F&G 극단) 백테스트: {symbol}")
    print("=" * 78)
    print(f"  기간          : {df.index[0].date()} → {df.index[-1].date()} ({len(df)}일)")
    print(mt.format_summary(m))
    print(f"  Buy & Hold    : {bh_return*100:>+11.2f}%")
    print(f"  vs BH         : {diff*100:>+11.2f}%p [{'WIN' if diff > 0 else 'LOSE'}]")
    print(f"  거래 횟수     : {len(trades)} (승률 {win_rate*100:.1f}%)")
    print(f"  F&G 극공포일  : {state.fg_extreme_fear_days}일")
    print(f"  F&G 극탐욕일  : {state.fg_extreme_greed_days}일")
    print(f"  F&G 분할매수  : {state.fg_buy_actions}회")
    print(f"  F&G 분할매도  : {state.fg_sell_actions}회")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("symbol", nargs="?")
    parser.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    args = parser.parse_args()

    print("F&G 히스토리 다운로드 중...")
    fg_history = fear_greed.fetch_history(limit=0)
    if not fg_history:
        print("[실패] F&G 히스토리 가져올 수 없음")
        return 1
    print(f"  {len(fg_history)}일 확보\n")

    symbols = [args.symbol] if args.symbol else DEFAULT_UNIVERSE
    print(f"백테스트: {symbols}")

    for sym in symbols:
        df = load_symbol(sym)
        if df is None:
            print(f"  [경고] {sym}: 데이터 없음")
            continue
        state = run_backtest(df, fg_history, args.capital)
        print_summary(sym, state, df, args.capital)

    return 0


if __name__ == "__main__":
    sys.exit(main())
