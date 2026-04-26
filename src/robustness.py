"""
Dual Momentum 견고성 검증 (과적합 탐지).

3종 검증:
  1. 파라미터 민감도: 룩백 3~24개월 → 수익률 안정성
  2. IS/OOS 분할: 전반부 학습 vs 후반부 검증 → 시기 독립성
  3. 연도별 수익: 특정 해에만 의존하지 않는지

목적: "이 전략이 진짜 엣지인지, 우연히 잘 맞은 건지" 구분.

실행:
  python -m src.robustness
  python -m src.robustness --split 2021-06-01
  python -m src.robustness --symbols 069500 SPY QQQ 005930
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from . import dual_momentum as dm


def _single_run(prices: pd.DataFrame, lookback: int, cost: float) -> dict:
    """한 번의 백테스트 → 지표 dict."""
    signal = dm.dual_momentum_signal(prices, lookback)
    equity, returns, _ = dm.run_dual_momentum(prices, signal, cost)
    return dm.compute_metrics(equity, returns)


def _row(label: str, m: dict) -> str:
    return (
        f"{label:<14} "
        f"{m['cagr']*100:>+8.2f}% "
        f"{m['sharpe']:>7.2f} "
        f"{m['mdd']*100:>+8.2f}% "
        f"{m['total_return']*100:>+9.2f}%"
    )


def _header() -> str:
    return (
        f"{'':<14} "
        f"{'CAGR':>9} "
        f"{'Sharpe':>7} "
        f"{'MDD':>9} "
        f"{'누적':>10}"
    )


# ── 1. 파라미터 민감도 ─────────────────────────────────

def param_sensitivity(prices: pd.DataFrame, cost: float) -> None:
    print("\n" + "=" * 64)
    print("1. 파라미터 민감도 (룩백 개월 변화에 따른 결과)")
    print("=" * 64)

    lookbacks = [3, 6, 9, 12, 15, 18, 24]
    metrics_list = []

    print(_header())
    print("-" * 64)
    for lb in lookbacks:
        m = _single_run(prices, lb, cost)
        metrics_list.append((lb, m))
        print(_row(f"룩백 {lb:>2}개월", m))

    cagrs = [m["cagr"] for _, m in metrics_list]
    sharpes = [m["sharpe"] for _, m in metrics_list]
    cagr_range = max(cagrs) - min(cagrs)
    sharpe_range = max(sharpes) - min(sharpes)

    print(f"\nCAGR 변동 폭  : {cagr_range*100:.2f}%p")
    print(f"Sharpe 변동 폭: {sharpe_range:.2f}")

    if cagr_range < 0.05:
        verdict = "매우 안정적 (파라미터 둔감 → 엣지 가능성 높음)"
    elif cagr_range < 0.10:
        verdict = "안정적"
    else:
        verdict = "불안정 (파라미터 민감 → 과적합 의심)"
    print(f"판정: {verdict}")


# ── 2. IS / OOS 분할 ──────────────────────────────────

def oos_split(
    prices: pd.DataFrame, lookback: int, cost: float, split_date: str
) -> None:
    print("\n" + "=" * 64)
    print(f"2. In-Sample vs Out-of-Sample (분할: {split_date})")
    print("=" * 64)

    cutoff = pd.Timestamp(split_date)
    is_prices = prices[prices.index < cutoff]
    oos_prices = prices[prices.index >= cutoff]

    if len(is_prices) < 300 or len(oos_prices) < 300:
        print("[경고] 분할된 기간 중 하나가 너무 짧아 신뢰 어려움")

    is_m = _single_run(is_prices, lookback, cost)
    oos_m = _single_run(oos_prices, lookback, cost)
    all_m = _single_run(prices, lookback, cost)

    print(_header())
    print("-" * 64)
    print(_row(f"IS ({is_prices.index[0].year}-{is_prices.index[-1].year})", is_m))
    print(_row(f"OOS ({oos_prices.index[0].year}-{oos_prices.index[-1].year})", oos_m))
    print(_row("전체", all_m))

    degradation = is_m["cagr"] - oos_m["cagr"]
    print(f"\nOOS CAGR 저하: {degradation*100:+.2f}%p")

    if abs(degradation) < 0.03:
        verdict = "일관됨 (OOS에서도 유사 성과 → 엣지 신뢰 가능)"
    elif degradation > 0.10:
        verdict = "크게 저하 (과적합 강한 의심)"
    elif degradation > 0.05:
        verdict = "약간 저하 (경계 필요)"
    elif degradation < -0.05:
        verdict = "OOS가 더 좋음 (우연일 가능성, 또는 시장 환경 변화)"
    else:
        verdict = "정상 범위"
    print(f"판정: {verdict}")


# ── 3. 연도별 수익 ─────────────────────────────────────

def yearly_analysis(
    prices: pd.DataFrame, lookback: int, cost: float
) -> None:
    print("\n" + "=" * 64)
    print(f"3. 연도별 수익률 (룩백 {lookback}개월)")
    print("=" * 64)

    signal = dm.dual_momentum_signal(prices, lookback)
    equity, returns, daily_asset = dm.run_dual_momentum(prices, signal, cost)

    # 연도별 누적수익
    yearly_ret = returns.resample("YE").apply(lambda r: (1 + r).prod() - 1)

    # 연도별 각 자산 BH
    bh_yearly = {}
    for sym in prices.columns:
        bh_ret = prices[sym].pct_change().fillna(0)
        bh_yearly[sym] = bh_ret.resample("YE").apply(lambda r: (1 + r).prod() - 1)

    print(f"{'연도':<6} {'전략':>10}  ", end="")
    for sym in prices.columns:
        print(f"{sym:>10}", end=" ")
    print()
    print("-" * 64)

    for date, ret in yearly_ret.items():
        year = date.year
        line = f"{year:<6} {ret*100:>+9.2f}%  "
        for sym in prices.columns:
            bh_r = bh_yearly[sym].get(date, 0)
            line += f"{bh_r*100:>+9.2f}% "
        print(line)

    neg_years = int((yearly_ret < 0).sum())
    total = len(yearly_ret)
    best = float(yearly_ret.max())
    worst = float(yearly_ret.min())

    print(f"\n음수 연도   : {neg_years}/{total} ({neg_years/total*100:.0f}%)")
    print(f"최고 연도   : {best*100:+.2f}%")
    print(f"최악 연도   : {worst*100:+.2f}%")

    if neg_years / total <= 0.2:
        verdict = "손실 연도 적음 (일관된 엣지)"
    elif neg_years / total <= 0.4:
        verdict = "정상 범위"
    else:
        verdict = "손실 연도 많음 (일관성 부족)"
    print(f"판정: {verdict}")


# ── 메인 ──────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Dual Momentum 견고성 검증")
    parser.add_argument(
        "--symbols", nargs="+", default=["069500", "SPY", "QQQ"]
    )
    parser.add_argument("--lookback", type=int, default=12)
    parser.add_argument("--cost", type=float, default=0.003)
    parser.add_argument(
        "--split",
        default="2021-01-01",
        help="IS/OOS 분할 날짜 (기본 2021-01-01)",
    )
    args = parser.parse_args()

    prices = dm.load_multi_prices(args.symbols)
    if prices.empty:
        print("데이터 없음. python -m src.load_candles 먼저.")
        return 1

    print("=" * 64)
    print("Dual Momentum 견고성 검증")
    print("=" * 64)
    print(f"유니버스: {args.symbols}")
    print(f"기간:     {prices.index[0].date()} → {prices.index[-1].date()}")
    print(f"비용:     {args.cost*100:.2f}%")

    param_sensitivity(prices, args.cost)
    oos_split(prices, args.lookback, args.cost, args.split)
    yearly_analysis(prices, args.lookback, args.cost)

    print("\n" + "=" * 64)
    print("최종 판단 가이드")
    print("=" * 64)
    print(
        "  - 파라미터 민감도 '안정적' + OOS '일관됨' + 연도별 '손실 적음'"
    )
    print("    → 실거래 모의 배포 단계(C)로 진행 가능")
    print("  - 하나라도 '불안정/크게 저하/손실 많음' 이면 전략 재설계 필요")
    return 0


if __name__ == "__main__":
    sys.exit(main())
