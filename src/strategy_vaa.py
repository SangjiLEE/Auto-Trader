"""
Vigilant Asset Allocation (VAA).

출처: Wouter J. Keller, Jan Willem Keuning,
     "Breadth Momentum and the Canary Universe: Defensive Asset Allocation"
     (Working paper, 2017)
     https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3002624

기본 발상: Antonacci DM 의 진화형 + "카나리 (canary)" 안전 신호 추가
  - Offensive 자산 (예: 주식 ETF 4개) 의 13612U 모멘텀 평가
  - 카나리 신호: 모든 offensive 자산이 양수 score 면 Risk-On
  - 하나라도 음수면 Risk-Off → Defensive (채권/현금) 로 도피

13612U momentum (가중 모멘텀):
  score = 12 × r_1 + 4 × r_3 + 2 × r_6 + 1 × r_12
  where r_n = n개월 수익률
  → 단기 (1개월) 이 가장 큰 가중치 → 추세 변화 빠르게 반영

VAA 매월 룰:
  1. Offensive 자산 score 계산
  2. 모두 양수: best offensive 자산 100% 매수
  3. 하나라도 음수: best defensive 자산 100% (또는 CASH)

장점:
  - DM 보다 더 보수적 (한 자산 부진해도 전체 risk-off)
  - 13612U 가 12-month 단일보다 빠른 반응
  - 학술 검증: 1925-2016 평균 CAGR 17.8% / MDD -16% (US 데이터)

우리 universe:
  Offensive: 069500 (한국주식), 133690 (나스닥), 360750 (S&P)  ← 3개
  Defensive: 148070 (국고채), CASH

표준 VAA 는 offensive 4 + defensive 3 이지만 우리는 4-asset universe 라 변형.
"""
from __future__ import annotations

import pandas as pd

CASH_LABEL = "CASH"


def momentum_13612u(monthly_prices: pd.DataFrame) -> pd.DataFrame:
    """
    13612U 모멘텀 score: 12*r1 + 4*r3 + 2*r6 + 1*r12.

    monthly_prices: 월말 가격 DataFrame
    반환: 같은 shape DataFrame, score 값
    """
    r1 = monthly_prices.pct_change(1)
    r3 = monthly_prices.pct_change(3)
    r6 = monthly_prices.pct_change(6)
    r12 = monthly_prices.pct_change(12)
    return 12 * r1 + 4 * r3 + 2 * r6 + 1 * r12


def vaa_signal_breadth(
    prices: pd.DataFrame,
    offensive: list[str],
    defensive: list[str],
    breadth_threshold: float = 1.0,
) -> pd.Series:
    """
    VAA breadth 변형 — 카나리 임계 조정 가능.

    breadth_threshold=1.0: 표준 (모든 offensive 양수면 risk-on)
    breadth_threshold=0.75: 75% 이상 양수면 risk-on (덜 보수적)
    breadth_threshold=0.50: 절반 양수면 risk-on (적극적)

    낮을수록 risk-on 자주 → 강세장 더 따라잡지만 위기 회피 ↓.
    """
    monthly = prices.resample("ME").last()
    if not monthly.empty and not prices.empty:
        if prices.index[-1] < monthly.index[-1]:
            monthly = monthly.iloc[:-1]
    score = momentum_13612u(monthly)

    choices: list[str] = []
    for dt in monthly.index:
        off_syms = [s for s in offensive if s in score.columns]
        off_scores = score.loc[dt, off_syms]
        if off_scores.isna().any() or off_scores.empty:
            choices.append(CASH_LABEL)
            continue

        positive_ratio = (off_scores > 0).sum() / len(off_scores)
        if positive_ratio >= breadth_threshold:
            choices.append(str(off_scores.idxmax()))
        else:
            def_syms = [s for s in defensive if s in score.columns]
            def_scores = score.loc[dt, def_syms]
            if def_scores.empty or def_scores.isna().all():
                choices.append(CASH_LABEL)
            elif def_scores.max() > 0:
                choices.append(str(def_scores.idxmax()))
            else:
                choices.append(CASH_LABEL)

    return pd.Series(choices, index=monthly.index)


def vaa_signal(
    prices: pd.DataFrame,
    offensive: list[str],
    defensive: list[str],
) -> pd.Series:
    """
    매월 말 VAA 신호: best offensive 또는 best defensive (또는 CASH).

    반환: monthly index, value = 자산명 또는 'CASH'
    """
    monthly = prices.resample("ME").last()

    # [B1 fix] 부분 현재월 bucket 제거
    if not monthly.empty and not prices.empty:
        if prices.index[-1] < monthly.index[-1]:
            monthly = monthly.iloc[:-1]

    score = momentum_13612u(monthly)

    choices: list[str] = []
    for dt in monthly.index:
        # offensive score 가용한지
        off_scores = score.loc[dt, [s for s in offensive if s in score.columns]]
        if off_scores.isna().any() or off_scores.empty:
            choices.append(CASH_LABEL)
            continue

        # 카나리 신호: 모두 양수면 Risk-On
        if (off_scores > 0).all():
            choices.append(str(off_scores.idxmax()))
        else:
            # Risk-Off: best defensive
            def_scores = score.loc[dt, [s for s in defensive if s in score.columns]]
            if def_scores.empty or def_scores.isna().all():
                choices.append(CASH_LABEL)
            elif def_scores.max() > 0:
                choices.append(str(def_scores.idxmax()))
            else:
                # 모든 defensive 도 음수 → CASH
                choices.append(CASH_LABEL)

    return pd.Series(choices, index=monthly.index)


def run_vaa_backtest(
    prices: pd.DataFrame,
    monthly_signal: pd.Series,
    cost_model_fn=None,
    cost: float = 0.003,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    VAA 신호 (single-asset switching) → 백테스트.

    DM 과 동일 구조 (winner-take-all). 자산 전환 시 from sell + to buy 비용.
    반환: (equity, returns, daily_asset_lagged)
    """
    daily_asset = monthly_signal.reindex(prices.index, method="ffill")
    # [B5 fix] D 일 신호 → D+1 거래일 반영
    daily_asset_lagged = daily_asset.shift(1).ffill().fillna(CASH_LABEL)

    asset_returns = prices.pct_change().fillna(0)

    strategy_returns = pd.Series(0.0, index=prices.index)
    for dt, asset in daily_asset_lagged.items():
        if asset != CASH_LABEL and asset in asset_returns.columns:
            strategy_returns.loc[dt] = asset_returns.at[dt, asset]

    # 자산 변경 비용
    changes = (daily_asset_lagged != daily_asset_lagged.shift(1)).fillna(False)
    if cost_model_fn is None:
        strategy_returns = strategy_returns - changes.astype(float) * cost
    else:
        cost_per_change = pd.Series(0.0, index=prices.index)
        prev_assets = daily_asset_lagged.shift(1)
        for dt in prices.index:
            if not changes.at[dt]:
                continue
            prev = prev_assets.at[dt]
            curr = daily_asset_lagged.at[dt]
            c = 0.0
            if prev != CASH_LABEL and prev in prices.columns:
                c += cost_model_fn(str(prev)).sell_total
            if curr != CASH_LABEL and curr in prices.columns:
                c += cost_model_fn(str(curr)).buy_total
            cost_per_change.at[dt] = c
        strategy_returns = strategy_returns - cost_per_change

    equity = (1 + strategy_returns).cumprod()
    return equity, strategy_returns, daily_asset_lagged


def main() -> int:
    import argparse
    import sys
    from . import cost_model as cm
    from . import dual_momentum as dm
    from . import metrics as mt

    parser = argparse.ArgumentParser(description="VAA 백테스트")
    parser.add_argument("--offensive", nargs="+",
                        default=["069500", "133690", "360750"],
                        help="Offensive (주식) 유니버스")
    parser.add_argument("--defensive", nargs="+",
                        default=["148070"],
                        help="Defensive (채권) 유니버스")
    args = parser.parse_args()

    all_symbols = list(set(args.offensive + args.defensive))
    prices = dm.load_multi_prices(all_symbols)
    if prices.empty:
        print("데이터 없음.")
        return 1

    print("=" * 70)
    print(f"VAA: offensive={args.offensive} | defensive={args.defensive}")
    print(f"기간: {prices.index[0].date()} → {prices.index[-1].date()}")
    print("=" * 70)

    signal = vaa_signal(prices, args.offensive, args.defensive)
    eq, returns, daily_asset = run_vaa_backtest(
        prices, signal, cost_model_fn=cm.get_cost_model,
    )

    metrics = mt.compute_metrics(eq)
    print("\n전략 결과 (확장 메트릭):")
    print(mt.format_summary(metrics, currency="배"))

    # 자산별 보유 비율
    counts = signal.value_counts().sort_values(ascending=False)
    total = len(signal)
    print(f"\n월별 선택 분포:")
    for asset, n in counts.items():
        pct = n / total * 100
        bar = "█" * int(pct / 2)
        print(f"  {asset:<8}: {n:>4}개월 ({pct:>5.1f}%) {bar}")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
