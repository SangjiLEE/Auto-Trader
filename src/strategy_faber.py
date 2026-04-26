"""
Faber Tactical Asset Allocation (TAA).

출처: Mebane Faber, "A Quantitative Approach to Tactical Asset Allocation"
     (Journal of Wealth Management, 2007)

가장 인용된 학술 백서 중 하나. 1973-2008 검증 결과:
  - 미국 주식 전체 BH: CAGR 9.7%, MDD -50.9%
  - Faber 10MA 적용: CAGR 10.5%, MDD -23.0%  ← MDD 절반!

룰 (놀라울 정도로 단순):
  매월 말 평가:
    - 자산 종가 > 10개월 SMA  →  보유
    - 자산 종가 ≤ 10개월 SMA  →  현금

장점:
  1. 파라미터 1개 (10) — 과적합 위험 낮음
  2. 자산 독립적 — 각 자산을 개별 평가
  3. 매월 1회 거래 — 비용 낮음
  4. 직관적 — "장기 추세 유지 시만 보유"

우리 universe (069500/133690/360750/148070) 적용 시 동일 weight 분배.
모든 자산이 cash 면 100% 현금.

run with:
  python -m src.strategy_faber
  python -m src.strategy_faber --ma 12  # 12MA 변형
"""
from __future__ import annotations

import pandas as pd

DEFAULT_MA_MONTHS = 10


def faber_signal(prices: pd.DataFrame, ma_months: int = DEFAULT_MA_MONTHS) -> pd.DataFrame:
    """
    매월 말 각 자산의 N개월 SMA 평가.

    반환: monthly index, columns = 자산, value = 1.0 (보유) or 0.0 (현금)
    """
    monthly = prices.resample("ME").last()

    # [B1 fix] 부분 현재월 bucket 제거 — incomplete 시 마지막 row drop
    if not monthly.empty and not prices.empty:
        last_data = prices.index[-1]
        last_bucket = monthly.index[-1]
        if last_data < last_bucket:
            monthly = monthly.iloc[:-1]

    # N개월 SMA
    ma = monthly.rolling(window=ma_months, min_periods=ma_months).mean()
    # 종가 > MA → 1.0, else 0.0
    above = (monthly > ma).astype(float)
    # MA 가 NaN 인 초기 N-1 개월은 신호 없음 (현금)
    above = above.where(ma.notna(), 0.0)
    return above


def run_faber_backtest(
    prices: pd.DataFrame,
    signal: pd.DataFrame,
    cost_model_fn=None,
    cost: float = 0.003,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Faber 신호 → 일별 weight → 전략 수익.

    각 활성 자산에 동일 weight (1/N) 분배. 모두 cash 면 0% 노출.

    cost_model_fn: callable(symbol) → CostModel. None 이면 cost float 사용.
    반환: (equity, returns, n_active_per_day)
    """
    daily_signal = signal.reindex(prices.index, method="ffill")
    # [B5 fix] 신호는 D 일 close → D+1 거래일에 반영. shift(1).
    daily_signal_lagged = daily_signal.shift(1).fillna(0.0)

    # 활성 자산 수
    n_active = daily_signal_lagged.sum(axis=1)
    # weights = 1/N for active assets, 0 for inactive
    weights = daily_signal_lagged.div(n_active.replace(0, 1), axis=0)
    weights = weights.where(n_active > 0, 0.0)

    # 자산별 일별 수익
    asset_returns = prices.pct_change().fillna(0.0)
    strategy_returns = (weights * asset_returns).sum(axis=1)

    # 비용: weight 변화 절댓값 × cost
    weight_changes = weights.diff().abs().fillna(weights.abs().fillna(0))
    if cost_model_fn is None:
        # 단일 비용 — 변화 총합 × half_cost (각 자산 half RT 가정)
        total_change = weight_changes.sum(axis=1)
        cost_drag = total_change * (cost / 2)
    else:
        # 자산별 정확 비용
        cost_drag = pd.Series(0.0, index=prices.index)
        for sym in prices.columns:
            sym_cm = cost_model_fn(str(sym))
            # weight 증가 = buy, 감소 = sell
            sym_changes = weights[sym].diff().fillna(weights[sym])
            buy_changes = sym_changes.clip(lower=0)
            sell_changes = (-sym_changes).clip(lower=0)
            cost_drag = cost_drag + buy_changes * sym_cm.buy_total + sell_changes * sym_cm.sell_total
    strategy_returns = strategy_returns - cost_drag

    equity = (1 + strategy_returns).cumprod()
    return equity, strategy_returns, n_active


def main() -> int:
    import argparse
    import sys
    from . import cost_model as cm
    from . import dual_momentum as dm
    from . import metrics as mt

    parser = argparse.ArgumentParser(description="Faber TAA 백테스트")
    parser.add_argument("--symbols", nargs="+",
                        default=["069500", "133690", "360750", "148070"])
    parser.add_argument("--ma", type=int, default=DEFAULT_MA_MONTHS,
                        help=f"SMA 기간 개월 (기본 {DEFAULT_MA_MONTHS})")
    args = parser.parse_args()

    prices = dm.load_multi_prices(args.symbols)
    if prices.empty:
        print("데이터 없음. python -m src.load_candles 먼저.")
        return 1

    print("=" * 70)
    print(f"Faber TAA: {args.symbols}")
    print(f"룩백 SMA: {args.ma}개월 | 기간 {prices.index[0].date()} → {prices.index[-1].date()}")
    print("=" * 70)

    signal = faber_signal(prices, ma_months=args.ma)
    eq, returns, n_active = run_faber_backtest(
        prices, signal, cost_model_fn=cm.get_cost_model,
    )

    metrics = mt.compute_metrics(eq)
    print("\n전략 결과 (확장 메트릭):")
    print(mt.format_summary(metrics, currency="배"))

    # 활성 자산 분포
    print(f"\n노출 분포 (일별 평균):")
    print(f"  100% 보유: {((n_active > 0) & (n_active == len(args.symbols))).mean()*100:.1f}%")
    print(f"  부분 보유: {((n_active > 0) & (n_active < len(args.symbols))).mean()*100:.1f}%")
    print(f"  100% 현금: {(n_active == 0).mean()*100:.1f}%")
    print(f"  평균 활성 자산: {n_active.mean():.2f}개")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
