"""
스윙 트레이딩 백테스트.

지표 계산 → 일자별 루프 → 진입/청산 → 포지션 관리 → 리스크 체크 → 성과.

리스크 관리:
  - 1종목 계좌의 10% (설정값)
  - 동시 최대 3종목
  - 일일 -3% 손실 → 당일 신규 진입 차단 (청산은 허용)
  - 월간 -10% 손실 → 해당 월 신규 진입 차단
  - 연속 3거래 손실 → N일 쿨다운

실행:
  python -m src.swing_backtest                    # 기본 유니버스
  python -m src.swing_backtest 069500             # 단일 종목
  python -m src.swing_backtest --capital 10000000
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

import pandas as pd

from . import db
from . import indicators
from . import swing_strategy as strat

# ── 설정 ───────────────────────────────────────

INITIAL_CAPITAL = 10_000_000   # 천만원 가정 (백테스트용)
POSITION_PCT = 0.10            # 1종목 계좌 10%
MAX_POSITIONS = 3              # 동시 최대 3종목
DAILY_LOSS_LIMIT = -0.03       # 일일 -3% (신규 진입 차단)
MONTHLY_LOSS_LIMIT = -0.10     # 월간 -10% (해당 월 차단)
LOSING_STREAK_LIMIT = 3        # 연속 3손실
COOLDOWN_DAYS = 7              # 쿨다운 기간
COMMISSION = 0.003             # 왕복 0.3% (수수료+세금+슬리피지)

DEFAULT_UNIVERSE = ["069500", "133690", "005930"]


# ── 상태 ───────────────────────────────────────

@dataclass
class BacktestState:
    cash: float
    positions: dict[str, strat.Position] = field(default_factory=dict)
    completed_trades: list[dict] = field(default_factory=list)
    equity_curve: dict[pd.Timestamp, float] = field(default_factory=dict)
    cooldown_until: pd.Timestamp | None = None
    last_n_results: list[bool] = field(default_factory=list)  # True=수익, False=손실
    last_exit_per_symbol: dict[str, pd.Timestamp] = field(default_factory=dict)


def compute_equity(
    state: BacktestState, prices_today: dict[str, float]
) -> float:
    total = state.cash
    for sym, pos in state.positions.items():
        price = prices_today.get(sym, pos.entry_price)
        total += pos.qty * price
    return total


def in_cooldown(state: BacktestState, date: pd.Timestamp) -> bool:
    if state.cooldown_until is None:
        return False
    return date < state.cooldown_until


def check_daily_loss_block(
    state: BacktestState,
    today_equity: float,
    yesterday_equity: float,
) -> bool:
    """일일 손실 한도 초과 여부."""
    if yesterday_equity <= 0:
        return False
    pnl = (today_equity - yesterday_equity) / yesterday_equity
    return pnl <= DAILY_LOSS_LIMIT


def check_monthly_loss_block(
    state: BacktestState,
    today_equity: float,
    month_start_equity: float,
) -> bool:
    """월간 손실 한도 초과 여부."""
    if month_start_equity <= 0:
        return False
    pnl = (today_equity - month_start_equity) / month_start_equity
    return pnl <= MONTHLY_LOSS_LIMIT


# ── 거래 ───────────────────────────────────────

def enter_position(
    state: BacktestState,
    symbol: str,
    date: pd.Timestamp,
    price: float,
    equity: float,
) -> None:
    """진입. 계좌의 10% 사이즈로."""
    alloc = equity * POSITION_PCT
    qty = int(alloc / price)
    if qty <= 0:
        return

    cost = qty * price * (1 + COMMISSION / 2)  # 매수 수수료
    if cost > state.cash:
        qty = int(state.cash / (price * (1 + COMMISSION / 2)))
        if qty <= 0:
            return
        cost = qty * price * (1 + COMMISSION / 2)

    state.cash -= cost
    state.positions[symbol] = strat.Position(
        entry_date=date, entry_price=price, qty=qty
    )


def exit_position(
    state: BacktestState,
    symbol: str,
    date: pd.Timestamp,
    price: float,
    reason: str,
) -> None:
    """청산."""
    position = state.positions.pop(symbol)
    proceeds = position.qty * price * (1 - COMMISSION / 2)
    state.cash += proceeds

    pnl = proceeds - (position.qty * position.entry_price * (1 + COMMISSION / 2))
    pnl_pct = pnl / (position.qty * position.entry_price)
    is_win = pnl > 0

    state.completed_trades.append(
        {
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
        }
    )

    # 재진입 쿨다운용 마지막 청산일 기록
    state.last_exit_per_symbol[symbol] = date

    # 연속 손실 트래킹
    state.last_n_results.append(is_win)
    state.last_n_results = state.last_n_results[-LOSING_STREAK_LIMIT:]
    if (
        len(state.last_n_results) == LOSING_STREAK_LIMIT
        and not any(state.last_n_results)
    ):
        state.cooldown_until = date + pd.Timedelta(days=COOLDOWN_DAYS)
        state.last_n_results = []  # 리셋


# ── 메인 루프 ──────────────────────────────────

def run_backtest(
    symbol_data: dict[str, pd.DataFrame],
    initial_capital: float = INITIAL_CAPITAL,
) -> BacktestState:
    """symbol_data: {symbol: indicators 붙은 DataFrame}."""

    # 공통 인덱스 (모든 종목의 거래일 교집합)
    common_dates = None
    for df in symbol_data.values():
        if common_dates is None:
            common_dates = set(df.index)
        else:
            common_dates = common_dates & set(df.index)
    if not common_dates:
        return BacktestState(cash=initial_capital)

    dates = sorted(common_dates)
    state = BacktestState(cash=initial_capital)

    # 월간 시작 equity 추적
    prev_date: pd.Timestamp | None = None
    prev_equity = initial_capital
    month_start_equity = initial_capital
    current_month = dates[0].to_period("M")

    for i, date in enumerate(dates):
        # 오늘 종가 맵
        prices_today = {
            sym: float(df.loc[date, "close"])
            for sym, df in symbol_data.items()
            if date in df.index and pd.notna(df.loc[date, "close"])
        }

        # 월 시작 감지
        this_month = date.to_period("M")
        if this_month != current_month:
            current_month = this_month
            month_start_equity = prev_equity

        # 1. 청산 먼저 (위험 한도와 무관하게 항상 허용)
        for sym in list(state.positions.keys()):
            if sym not in symbol_data:
                continue
            df = symbol_data[sym]
            if date not in df.index:
                continue
            row = df.loc[date]
            position = state.positions[sym]
            exit_signal = strat.check_exit(row, position, date)
            if exit_signal.should_exit:
                exit_position(state, sym, date, float(row["close"]), exit_signal.reason)

        # 2. 진입 판단 (리스크 한도 체크)
        today_equity = compute_equity(state, prices_today)

        block_new = False
        if in_cooldown(state, date):
            block_new = True
        elif check_daily_loss_block(state, today_equity, prev_equity):
            block_new = True
        elif check_monthly_loss_block(state, today_equity, month_start_equity):
            block_new = True

        # 진입 (쿨다운/한도 아니면)
        if not block_new and len(state.positions) < MAX_POSITIONS:
            for sym in symbol_data:
                if sym in state.positions:
                    continue
                if len(state.positions) >= MAX_POSITIONS:
                    break
                # 재진입 쿨다운 체크 (같은 종목 최근 청산 후 N일 미만이면 스킵)
                last_exit = state.last_exit_per_symbol.get(sym)
                if last_exit is not None:
                    days_since_exit = (date - last_exit).days
                    if days_since_exit < strat.REENTRY_COOLDOWN_DAYS:
                        continue
                df = symbol_data[sym]
                # 어제 종가 기준 시그널 (look-ahead 방지)
                prev_idx = df.index.get_loc(date) - 1 if date in df.index else -1
                if prev_idx < 0:
                    continue
                prev_row = df.iloc[prev_idx]
                entry_signal = strat.check_entry(prev_row)
                if entry_signal.valid:
                    price = float(df.loc[date, "close"])
                    enter_position(state, sym, date, price, today_equity)

        # 자본 곡선 기록
        end_of_day_equity = compute_equity(state, prices_today)
        state.equity_curve[date] = end_of_day_equity
        prev_equity = end_of_day_equity
        prev_date = date

    # 마지막 날 모든 포지션 강제 청산
    if state.positions and dates:
        last_date = dates[-1]
        for sym in list(state.positions.keys()):
            if sym in symbol_data and last_date in symbol_data[sym].index:
                price = float(symbol_data[sym].loc[last_date, "close"])
                exit_position(state, sym, last_date, price, "백테스트 종료")

    return state


# ── 데이터 로드 ────────────────────────────────

def load_symbol_with_indicators(symbol: str) -> pd.DataFrame | None:
    with db.connection() as conn:
        df = pd.read_sql_query(
            """
            SELECT date, open, high, low, close, volume
            FROM daily_candles
            WHERE symbol = ?
            ORDER BY date ASC
            """,
            conn,
            params=(symbol,),
            parse_dates=["date"],
            index_col="date",
        )
    if df.empty:
        return None
    return indicators.attach_all(df)


# ── 결과 출력 ──────────────────────────────────

def print_summary(state: BacktestState, initial: float) -> None:
    equity_series = pd.Series(state.equity_curve)
    if equity_series.empty:
        print("백테스트 결과 없음")
        return

    final = float(equity_series.iloc[-1])
    total_return = (final - initial) / initial

    days = len(equity_series)
    years = days / 252 if days > 0 else 1
    cagr = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

    returns = equity_series.pct_change().fillna(0)
    sharpe = 0.0
    if returns.std() > 0:
        sharpe = float((returns.mean() * 252) / (returns.std() * (252 ** 0.5)))

    running_max = equity_series.cummax()
    mdd = float(((equity_series - running_max) / running_max).min())

    trades = state.completed_trades
    n_trades = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / n_trades if n_trades else 0
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    avg_hold = sum(t["days_held"] for t in trades) / n_trades if n_trades else 0

    exit_reasons: dict[str, int] = {}
    for t in trades:
        reason_key = t["reason"].split()[0] if t["reason"] else "?"
        exit_reasons[reason_key] = exit_reasons.get(reason_key, 0) + 1

    print("\n" + "=" * 64)
    print("스윙 백테스트 결과")
    print("=" * 64)
    print(f"  초기 자본    : {initial:>15,.0f} 원")
    print(f"  최종 자본    : {final:>15,.0f} 원")
    print(f"  누적 수익    : {total_return*100:>+14.2f}%")
    print(f"  CAGR         : {cagr*100:>+14.2f}%")
    print(f"  Sharpe       : {sharpe:>15.2f}")
    print(f"  최대 낙폭    : {mdd*100:>+14.2f}%")
    print()
    print(f"  거래 횟수    : {n_trades}")
    print(f"  승률         : {win_rate*100:.1f}% ({len(wins)} 승 {len(losses)} 패)")
    print(f"  평균 수익거래: {avg_win*100:+.2f}%")
    print(f"  평균 손실거래: {avg_loss*100:+.2f}%")
    print(f"  평균 보유일수: {avg_hold:.1f}일")
    print()
    if exit_reasons:
        print("  청산 사유 분포:")
        for reason, count in sorted(
            exit_reasons.items(), key=lambda x: -x[1]
        ):
            print(f"    {reason:<12}: {count}")


def print_recent_trades(state: BacktestState, n: int = 10) -> None:
    trades = state.completed_trades
    if not trades:
        return
    print(f"\n최근 {min(n, len(trades))} 거래:")
    print(
        f"  {'종목':<8} {'진입':<12} {'청산':<12} {'수익':>8} "
        f"{'일':>4} {'사유':<20}"
    )
    for t in trades[-n:]:
        print(
            f"  {t['symbol']:<8} "
            f"{t['entry_date'].strftime('%Y-%m-%d'):<12} "
            f"{t['exit_date'].strftime('%Y-%m-%d'):<12} "
            f"{t['pnl_pct']*100:>+7.2f}% "
            f"{t['days_held']:>3}d "
            f"{t['reason']:<20}"
        )


# ── 메인 ───────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="스윙 백테스트")
    parser.add_argument("symbol", nargs="?", help="단일 종목 (생략 시 기본 유니버스)")
    parser.add_argument(
        "--capital", type=float, default=INITIAL_CAPITAL, help="초기 자본"
    )
    parser.add_argument("--trades", type=int, default=15, help="출력할 최근 거래 수")
    args = parser.parse_args()

    symbols = [args.symbol] if args.symbol else DEFAULT_UNIVERSE
    print(f"유니버스: {symbols}")
    print(f"초기 자본: {args.capital:,.0f}원")

    symbol_data: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = load_symbol_with_indicators(sym)
        if df is None:
            print(f"  [경고] {sym}: 데이터 없음")
            continue
        symbol_data[sym] = df
        print(f"  {sym}: {len(df)}일")

    if not symbol_data:
        print("데이터 없음. python -m src.load_candles 먼저.")
        return 1

    print("\n백테스트 실행 중...")
    state = run_backtest(symbol_data, initial_capital=args.capital)

    print_summary(state, args.capital)
    print_recent_trades(state, n=args.trades)
    return 0


if __name__ == "__main__":
    sys.exit(main())
