"""
Dual Momentum 백테스트 (Gary Antonacci, 2014).

로직:
  매월 말:
    1. 유니버스 각 자산의 지난 N개월 누적 수익률 계산
    2. 가장 높은 수익률의 자산 선택
    3. 그 수익률이 0 초과면 다음 달 해당 자산 보유, else 현금

유니버스 기본: KODEX 200(한국), SPY(미국), QQQ(미국 기술주).
Antonacci 원본은 SPY vs VEU vs BIL. 우리는 한국 투자자 관점.

왜 MA 단순 크로스보다 강한가:
  - 상대 강도(relative momentum): 여러 자산 중 이긴 걸 고른다
  - 절대 강도(absolute momentum): 모두 지면 현금으로 빠진다
  - 월봉 리밸런싱: 비용 낮고 과적합 적음

실행:
  python -m src.dual_momentum
  python -m src.dual_momentum --lookback 6
  python -m src.dual_momentum --symbols 069500 SPY
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from . import db

DEFAULT_SYMBOLS = ["069500", "SPY", "QQQ"]
CASH_LABEL = "CASH"


def load_multi_prices(symbols: list[str]) -> pd.DataFrame:
    """여러 종목 종가를 한 DataFrame으로. 공통 기간만 남김."""
    frames: dict[str, pd.Series] = {}
    with db.connection() as conn:
        for sym in symbols:
            df = pd.read_sql_query(
                "SELECT date, close FROM daily_candles WHERE symbol = ? ORDER BY date",
                conn,
                params=(sym,),
                parse_dates=["date"],
                index_col="date",
            )
            if df.empty:
                print(f"  [경고] {sym}: 데이터 없음, 유니버스에서 제외")
                continue
            frames[sym] = df["close"]

    if not frames:
        return pd.DataFrame()

    combined = pd.DataFrame(frames).dropna()
    return combined


def dual_momentum_signal(
    prices: pd.DataFrame,
    lookback_months: int,
    complete_only: bool = True,
) -> pd.Series:
    """매월 말 선택 자산 (또는 CASH) 반환.

    반환: index = 월말 날짜, value = 자산 이름 또는 'CASH'

    [B1 fix] complete_only=True (기본):
      `resample("ME").last()` 는 부분 진행 중인 현재 월에 대해서도
      bucket(label = calendar month-end) 을 만들기 때문에, 매월 1~3일에
      라이브 실행하면 "지난달 신호" 가 아니라 "현재 부분월 신호" 를 사용
      하게 됨. 마지막 실제 거래일이 마지막 bucket label 보다 이전이면
      해당 bucket 은 미완료 → 제외.

      백테스트 시퀀스 분석 등에서 모든 bucket 이 필요하다면
      complete_only=False 로 호출.
    """
    monthly = prices.resample("ME").last()

    # B1: 미완료 현재월 bucket 제거 (라이브 신호 정확성 보장)
    if complete_only and not monthly.empty and not prices.empty:
        last_data_date = prices.index[-1]
        last_bucket_label = monthly.index[-1]
        if last_data_date < last_bucket_label:
            monthly = monthly.iloc[:-1]

    # N개월 수익률
    monthly_return = monthly.pct_change(lookback_months)

    choices: list[str] = []
    dates: list[pd.Timestamp] = []
    for dt, row in monthly_return.iterrows():
        dates.append(dt)
        if row.isna().all():
            choices.append(CASH_LABEL)
            continue
        best_asset = row.idxmax()
        best_return = row[best_asset]
        if pd.notna(best_return) and best_return > 0:
            choices.append(str(best_asset))
        else:
            choices.append(CASH_LABEL)

    return pd.Series(choices, index=pd.DatetimeIndex(dates))


def run_dual_momentum(
    prices: pd.DataFrame,
    monthly_signal: pd.Series,
    cost: float = 0.003,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """월봉 시그널 → 일봉 포지션 → 전략 수익.

    반환: (equity, returns, daily_asset_signal)
    """
    # 일봉 인덱스로 ffill (월말 결정 → 다음 달까지 유지)
    daily_asset = monthly_signal.reindex(prices.index, method="ffill")

    # 하루 늦춰서 실행 (look-ahead 방지, t 시그널 → t+1 진입)
    daily_asset_lagged = daily_asset.shift(1).ffill().fillna(CASH_LABEL)

    # 각 자산의 일별 수익률
    asset_returns = prices.pct_change().fillna(0)

    # 그날 보유 자산의 수익률 선택
    strategy_returns = pd.Series(0.0, index=prices.index)
    for dt, asset in daily_asset_lagged.items():
        if asset != CASH_LABEL and asset in asset_returns.columns:
            strategy_returns.loc[dt] = asset_returns.at[dt, asset]

    # 자산 변경 시 거래 비용 (양쪽 청산·진입)
    changes = (daily_asset_lagged != daily_asset_lagged.shift(1)).fillna(False)
    strategy_returns = strategy_returns - changes.astype(float) * cost

    equity = (1 + strategy_returns).cumprod()
    return equity, strategy_returns, daily_asset_lagged


def compute_metrics(
    equity: pd.Series, returns: pd.Series
) -> dict:
    total_return = float(equity.iloc[-1] - 1) if len(equity) else 0.0

    days = len(equity)
    years = days / 252 if days > 0 else 1
    cagr = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

    sharpe = 0.0
    if returns.std() > 0:
        sharpe = float((returns.mean() * 252) / (returns.std() * (252 ** 0.5)))

    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max
    mdd = float(drawdown.min()) if len(drawdown) else 0

    return {
        "total_return": total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "mdd": mdd,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Dual Momentum 백테스트")
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=DEFAULT_SYMBOLS,
        help="유니버스 (기본: 069500 SPY QQQ)",
    )
    parser.add_argument(
        "--lookback",
        type=int,
        default=12,
        help="수익률 룩백 기간 (개월, 기본 12)",
    )
    parser.add_argument(
        "--cost",
        type=float,
        default=0.003,
        help="왕복 거래 비용 (기본 0.003)",
    )
    args = parser.parse_args()

    prices = load_multi_prices(args.symbols)
    if prices.empty:
        print("데이터 없음. python -m src.load_candles 먼저.")
        return 1

    print("=" * 64)
    print(f"Dual Momentum: {' + '.join(args.symbols)} + CASH")
    print(f"룩백 {args.lookback}개월 | 비용 {args.cost*100:.2f}%")
    print(f"공통 기간: {prices.index[0].date()} → {prices.index[-1].date()}")
    print("=" * 64)

    signal = dual_momentum_signal(prices, args.lookback)
    equity, returns, daily_asset = run_dual_momentum(prices, signal, args.cost)
    m = compute_metrics(equity, returns)

    # 전략 지표
    print("\n전략 결과:")
    print(f"  누적 수익률  : {m['total_return'] * 100:+.2f}%")
    print(f"  CAGR         : {m['cagr'] * 100:+.2f}%")
    print(f"  샤프         : {m['sharpe']:.2f}")
    print(f"  최대 낙폭    : {m['mdd'] * 100:+.2f}%")

    # 선택 분포 (월봉 기준)
    print("\n월별 선택 분포:")
    counts = signal.value_counts().sort_values(ascending=False)
    total_months = len(signal)
    for asset, n in counts.items():
        pct = n / total_months * 100
        bar = "█" * int(pct / 2)
        print(f"  {asset:<8}: {n:>4} ({pct:>5.1f}%) {bar}")

    # 개별 자산 Buy & Hold 비교
    print("\n참고: 각 자산 Buy & Hold (동일 기간):")
    days = len(prices)
    years = days / 252
    for sym in args.symbols:
        if sym not in prices.columns:
            continue
        bh_total = float(prices[sym].iloc[-1] / prices[sym].iloc[0] - 1)
        bh_cagr = (1 + bh_total) ** (1 / years) - 1
        bh_returns = prices[sym].pct_change().fillna(0)
        bh_sharpe = 0.0
        if bh_returns.std() > 0:
            bh_sharpe = float(
                (bh_returns.mean() * 252) / (bh_returns.std() * (252 ** 0.5))
            )
        running_max = prices[sym].cummax()
        bh_mdd = float(((prices[sym] - running_max) / running_max).min())
        print(
            f"  {sym:<8}: 누적 {bh_total*100:>+8.2f}% | "
            f"CAGR {bh_cagr*100:>+6.2f}% | Sharpe {bh_sharpe:.2f} | MDD {bh_mdd*100:>+.2f}%"
        )

    # 전략 vs 단일 최고 BH
    best_sym = max(
        args.symbols,
        key=lambda s: prices[s].iloc[-1] / prices[s].iloc[0] if s in prices.columns else 0,
    )
    best_bh = float(prices[best_sym].iloc[-1] / prices[best_sym].iloc[0] - 1)
    diff = m["total_return"] - best_bh
    verdict = "WIN" if diff > 0 else "LOSE"
    print(f"\n전략 vs {best_sym} (최고 BH): {diff*100:+.2f}%p [{verdict}]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
