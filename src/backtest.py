"""
단순 백테스트 엔진 (pandas 기반).

전략:
  - 일봉 MA 크로스 (기본): 매일 체크. 위면 보유, 아래면 현금.
  - 월봉 MA 크로스 (--monthly): 월말에만 체크. Faber Timing Model.
    월말 종가 > N개월 MA → 다음 달 보유 / else 현금.

지표:
  누적수익, CAGR, Sharpe, MDD, 승률, 거래횟수, Buy & Hold 비교.

실행:
  python -m src.backtest                          # 전 종목, 일봉 200일 MA
  python -m src.backtest 005930 --monthly --ma 10 # 삼성, 월봉 10개월 MA
  python -m src.backtest SPY --cost 0             # 수수료 제로
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import pandas as pd

from . import db


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    start_date: str
    end_date: str
    total_return: float
    cagr: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    n_trades: int
    buy_hold_return: float


def load_prices(symbol: str) -> pd.DataFrame:
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
    return df


# ── 시그널 ──────────────────────────────────────────────

def ma_cross_signal_daily(prices: pd.DataFrame, window: int) -> pd.Series:
    """일봉 MA 크로스. 매일 시그널 갱신."""
    ma = prices["close"].rolling(window=window).mean()
    return (prices["close"] > ma).astype(int)


def ma_cross_signal_monthly(prices: pd.DataFrame, months: int) -> pd.Series:
    """
    월봉 MA 크로스 (Faber Timing Model).

    1. 월별 마지막 거래일 종가 추출
    2. N개월 MA 계산
    3. 월말 종가 > MA → 1, else 0
    4. 일봉 인덱스로 forward-fill (월 내 변경 없음)

    일봉 체크 대비 whipsaw 대폭 감소 → 거래 횟수 1/3~1/5 수준.
    """
    monthly_close = prices["close"].resample("ME").last()
    monthly_ma = monthly_close.rolling(window=months).mean()
    monthly_signal = (monthly_close > monthly_ma).astype(int)

    # 일봉 인덱스에 맞춰 확장 (ffill로 월 내내 같은 값 유지)
    daily_signal = monthly_signal.reindex(prices.index, method="ffill")
    return daily_signal.fillna(0).astype(int)


# ── 백테스트 루프 ────────────────────────────────────────

def run_backtest(
    prices: pd.DataFrame,
    signal: pd.Series,
    transaction_cost: float = 0.003,
) -> tuple[pd.Series, pd.Series]:
    """시그널 → 포지션 → 수익 → 자본 곡선.

    position = signal.shift(1): 오늘 시그널로 내일 진입 (look-ahead 방지).
    """
    position = signal.shift(1).fillna(0)
    daily_returns = prices["close"].pct_change().fillna(0)

    strategy_returns = position * daily_returns

    trades = position.diff().abs().fillna(0)
    costs = trades * transaction_cost
    strategy_returns = strategy_returns - costs

    equity = (1 + strategy_returns).cumprod()
    return equity, strategy_returns


# ── 지표 ───────────────────────────────────────────────

def compute_metrics(
    symbol: str,
    strategy: str,
    prices: pd.DataFrame,
    equity: pd.Series,
    returns: pd.Series,
    signal: pd.Series,
) -> BacktestResult:
    warmup_mask = signal.shift(1).notna()
    equity_v = equity[warmup_mask]
    returns_v = returns[warmup_mask]

    total_return = float(equity_v.iloc[-1] - 1) if len(equity_v) else 0.0

    days = len(equity_v)
    years = days / 252 if days > 0 else 1
    cagr = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

    sharpe = 0.0
    if returns_v.std() > 0:
        sharpe = float((returns_v.mean() * 252) / (returns_v.std() * (252 ** 0.5)))

    running_max = equity_v.cummax()
    drawdown = (equity_v - running_max) / running_max
    mdd = float(drawdown.min()) if len(drawdown) else 0

    position = signal.shift(1).fillna(0)
    entries = int((position.diff() == 1).sum())

    win_rate = _compute_win_rate(position, returns)
    bh_return = float(prices["close"].iloc[-1] / prices["close"].iloc[0] - 1)

    return BacktestResult(
        symbol=symbol,
        strategy=strategy,
        start_date=prices.index[0].strftime("%Y-%m-%d"),
        end_date=prices.index[-1].strftime("%Y-%m-%d"),
        total_return=total_return,
        cagr=cagr,
        sharpe=sharpe,
        max_drawdown=mdd,
        win_rate=win_rate,
        n_trades=entries,
        buy_hold_return=bh_return,
    )


def _compute_win_rate(position: pd.Series, returns: pd.Series) -> float:
    changes = position.diff().fillna(0)
    trade_returns = []
    entry_date = None
    for dt, chg in changes.items():
        if chg == 1:
            entry_date = dt
        elif chg == -1 and entry_date is not None:
            window = returns.loc[entry_date:dt]
            trade_returns.append(float((1 + window).prod() - 1))
            entry_date = None
    if not trade_returns:
        return 0.0
    wins = sum(1 for r in trade_returns if r > 0)
    return wins / len(trade_returns)


# ── 출력 ───────────────────────────────────────────────

def print_result(r: BacktestResult) -> None:
    pct = lambda x: f"{x * 100:+.2f}%"
    vs_bh = "WIN" if r.total_return > r.buy_hold_return else "LOSE"

    print(f"  전략         : {r.strategy}")
    print(f"  기간         : {r.start_date} → {r.end_date}")
    print(f"  누적 수익률  : {pct(r.total_return):>10}")
    print(f"  연복리 CAGR  : {pct(r.cagr):>10}")
    print(f"  샤프 (연환산): {r.sharpe:>10.2f}")
    print(f"  최대 낙폭 MDD: {pct(r.max_drawdown):>10}")
    print(f"  승률         : {r.win_rate * 100:>9.1f}% ({r.n_trades}회 거래)")
    print(f"  Buy & Hold   : {pct(r.buy_hold_return):>10}  <-- 단순 보유")
    print(f"  전략 vs BH   : {pct(r.total_return - r.buy_hold_return):>10}  [{vs_bh}]")


# ── 실행 ───────────────────────────────────────────────

def backtest_symbol(
    symbol: str, window: int, cost: float, monthly: bool
) -> BacktestResult | None:
    prices = load_prices(symbol)
    if prices.empty:
        print(f"  {symbol}: 데이터 없음")
        return None

    if monthly:
        # 월봉 필요 최소 데이터: months * ~21 거래일
        if len(prices) < window * 21 + 20:
            print(f"  {symbol}: 데이터 부족 (월봉 {window}개월에 필요)")
            return None
        strategy = f"월봉 {window}개월 MA (비용 {cost*100:.2f}%)"
        signal = ma_cross_signal_monthly(prices, window)
    else:
        if len(prices) < window + 10:
            print(f"  {symbol}: 데이터 부족 ({len(prices)}개)")
            return None
        strategy = f"일봉 {window}일 MA (비용 {cost*100:.2f}%)"
        signal = ma_cross_signal_daily(prices, window)

    equity, returns = run_backtest(prices, signal, transaction_cost=cost)
    return compute_metrics(symbol, strategy, prices, equity, returns, signal)


def main() -> int:
    parser = argparse.ArgumentParser(description="백테스트")
    parser.add_argument("symbol", nargs="?", help="종목코드 (생략 시 전체)")
    parser.add_argument(
        "--ma",
        type=int,
        default=None,
        help="MA 기간. 일봉이면 일수, 월봉이면 개월수 (기본: 일봉 200, 월봉 10)",
    )
    parser.add_argument(
        "--monthly",
        action="store_true",
        help="월봉 리밸런싱 (Faber Timing Model)",
    )
    parser.add_argument(
        "--cost",
        type=float,
        default=0.003,
        help="왕복 거래 비용 (기본 0.003 = 0.3%%)",
    )
    args = parser.parse_args()

    # 기본값: 일봉 200일, 월봉 10개월
    if args.ma is None:
        args.ma = 10 if args.monthly else 200

    if args.symbol:
        targets = [args.symbol]
    else:
        with db.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM daily_candles ORDER BY market, symbol"
            ).fetchall()
        targets = [r["symbol"] for r in rows]

    if not targets:
        print("데이터 없음. python -m src.load_candles 먼저.")
        return 1

    results = []
    for symbol in targets:
        print("=" * 64)
        print(f"백테스트: {symbol}")
        print("=" * 64)
        r = backtest_symbol(symbol, args.ma, args.cost, args.monthly)
        if r:
            print_result(r)
            results.append(r)
        print()

    if len(results) > 1:
        print("=" * 64)
        print("요약")
        print("=" * 64)
        print(
            f"{'종목':<8} {'CAGR':>8} {'샤프':>7} {'MDD':>8} "
            f"{'거래':>5} {'BH':>9} {'vs BH':>9}"
        )
        print("-" * 64)
        for r in results:
            print(
                f"{r.symbol:<8} "
                f"{r.cagr * 100:>+7.2f}% "
                f"{r.sharpe:>7.2f} "
                f"{r.max_drawdown * 100:>+7.2f}% "
                f"{r.n_trades:>5} "
                f"{r.buy_hold_return * 100:>+8.2f}% "
                f"{(r.total_return - r.buy_hold_return) * 100:>+8.2f}%"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
