"""
Walk-Forward 검증 — 시기별 OOS 분포 + Rolling Window 분석.

[Codex 리뷰 후속] 단일 IS/OOS 분할 (`robustness.py` 의 split=2021-01-01)
의 약점:
  - "그 시점에 split 하니 우호적" 같은 cherry-pick 가능성
  - OOS 가 한 번이라 통계력 부족

해결:
  1. **Yearly fold** — 매년 별 CAGR/Sharpe/MDD 분포. 어떤 해 강하고 어떤 해 약한지.
  2. **Rolling 3-year window** — 1095일 window 를 250일씩 slide.
     모든 window 의 metrics 분포 → 강건성 정량화.
  3. **Strategy 비교** — DM/Faber/VAA 같은 fold 로 비교.

이건 정통 anchored walk-forward (López de Prado, "Advances in Financial ML"
Ch.7) 의 변형이지만, 우리 전략들이 파라미터 고정이라 fold 별 재최적화 X.
대신 "시기 다양성에 대한 robustness" 측정.

실행:
  python -m src.walk_forward
  python -m src.walk_forward --strategies vaa dm faber
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import cost_model as cm
from . import dual_momentum as dm
from . import metrics as mt
from . import strategy_faber as faber
from . import strategy_vaa as vaa


# ─── Strategy runners ──────────────────────────────────

def _run_dm_eq(prices: pd.DataFrame) -> pd.Series:
    sig = dm.dual_momentum_signal(prices, 12)
    eq, _, _ = dm.run_dual_momentum(prices, sig, cost_model_fn=cm.get_cost_model)
    return eq


def _run_faber_eq(prices: pd.DataFrame) -> pd.Series:
    sig = faber.faber_signal(prices, ma_months=10)
    eq, _, _ = faber.run_faber_backtest(prices, sig, cost_model_fn=cm.get_cost_model)
    return eq


def _run_vaa_eq(prices: pd.DataFrame, offensive: list[str], defensive: list[str]) -> pd.Series:
    sig = vaa.vaa_signal(prices, offensive, defensive)
    eq, _, _ = vaa.run_vaa_backtest(prices, sig, cost_model_fn=cm.get_cost_model)
    return eq


# ─── Yearly fold ───────────────────────────────────────

@dataclass
class YearlyFold:
    year: int
    days: int
    total_return: float
    cagr: float
    sharpe: float
    mdd: float


def yearly_breakdown(equity: pd.Series) -> list[YearlyFold]:
    """매년 별 fold metrics."""
    eq = equity.dropna()
    if eq.empty:
        return []

    folds: list[YearlyFold] = []
    for year, group in eq.groupby(eq.index.year):
        if len(group) < 30:
            continue
        # 그 해 시작점 기준 normalize
        start = float(group.iloc[0])
        if start <= 0:
            continue
        normalized = group / start
        m = mt.compute_metrics(normalized, initial_capital=1.0)
        folds.append(YearlyFold(
            year=int(year), days=len(group),
            total_return=m.total_return, cagr=m.cagr,
            sharpe=m.sharpe, mdd=m.mdd,
        ))
    return folds


# ─── Rolling 3-year window ─────────────────────────────

@dataclass
class RollingStats:
    n_windows: int
    mean_cagr: float
    median_cagr: float
    std_cagr: float
    p5_cagr: float       # 5th percentile (worst case)
    mean_sharpe: float
    median_sharpe: float
    p5_sharpe: float
    mean_mdd: float
    worst_mdd: float
    pct_positive_cagr: float


def rolling_window_stats(
    equity: pd.Series,
    window_days: int = 750,        # 약 3년 (252 × 3)
    step_days: int = 60,           # 60일씩 slide
) -> RollingStats:
    """
    Rolling N-day window 의 metrics 분포.

    각 window:
      m = compute_metrics(equity[t:t+window])
    모든 m 을 모아 mean / median / 5th percentile 등 산출.
    """
    eq = equity.dropna()
    if len(eq) < window_days:
        return RollingStats(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    cagrs, sharpes, mdds = [], [], []
    for start in range(0, len(eq) - window_days, step_days):
        window = eq.iloc[start:start + window_days]
        if len(window) < window_days * 0.9:  # 데이터 결손 방어
            continue
        start_value = float(window.iloc[0])
        if start_value <= 0:
            continue
        normalized = window / start_value
        m = mt.compute_metrics(normalized, initial_capital=1.0)
        cagrs.append(m.cagr)
        sharpes.append(m.sharpe)
        mdds.append(m.mdd)

    if not cagrs:
        return RollingStats(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    cagrs_arr = np.array(cagrs)
    sharpes_arr = np.array(sharpes)
    mdds_arr = np.array(mdds)

    return RollingStats(
        n_windows=len(cagrs),
        mean_cagr=float(cagrs_arr.mean()),
        median_cagr=float(np.median(cagrs_arr)),
        std_cagr=float(cagrs_arr.std()),
        p5_cagr=float(np.percentile(cagrs_arr, 5)),
        mean_sharpe=float(sharpes_arr.mean()),
        median_sharpe=float(np.median(sharpes_arr)),
        p5_sharpe=float(np.percentile(sharpes_arr, 5)),
        mean_mdd=float(mdds_arr.mean()),
        worst_mdd=float(mdds_arr.min()),
        pct_positive_cagr=float((cagrs_arr > 0).mean()),
    )


# ─── Print helpers ─────────────────────────────────────

def print_yearly(name: str, folds: list[YearlyFold]) -> None:
    if not folds:
        print(f"\n  {name}: fold 없음")
        return
    print(f"\n## {name}")
    print(f"  {'연도':<6} {'일수':>5} {'수익률':>9} {'Sharpe':>7} {'MDD':>9}")
    print("  " + "-" * 45)
    for f in folds:
        print(
            f"  {f.year:<6} {f.days:>5} "
            f"{f.total_return*100:>+8.2f}% "
            f"{f.sharpe:>7.2f} "
            f"{f.mdd*100:>+8.2f}%"
        )
    n_pos = sum(1 for f in folds if f.total_return > 0)
    print(
        f"  → {len(folds)}개 fold 중 양수 {n_pos}개 ({n_pos/len(folds)*100:.0f}%)"
    )


def print_rolling(name: str, s: RollingStats) -> None:
    if s.n_windows == 0:
        print(f"\n  {name}: window 없음")
        return
    print(f"\n## {name} — Rolling 3-year (n={s.n_windows} windows)")
    print(f"  CAGR     : mean {s.mean_cagr*100:+.2f}%  median {s.median_cagr*100:+.2f}%  "
          f"std {s.std_cagr*100:.2f}%  p5 {s.p5_cagr*100:+.2f}%")
    print(f"  Sharpe   : mean {s.mean_sharpe:.2f}     median {s.median_sharpe:.2f}     "
          f"                      p5 {s.p5_sharpe:.2f}")
    print(f"  MDD      : mean {s.mean_mdd*100:+.2f}%  worst {s.worst_mdd*100:+.2f}%")
    print(f"  +CAGR 비율: {s.pct_positive_cagr*100:.1f}% (양수 수익 window)")


# ─── Main ──────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Walk-Forward + Rolling 검증")
    parser.add_argument("--strategies", nargs="+",
                        default=["vaa", "dm", "faber"],
                        choices=["vaa", "dm", "faber", "bh"])
    parser.add_argument("--symbols", nargs="+",
                        default=["069500", "133690", "148070"],
                        help="3-asset (10년) 또는 4-asset (5.7년)")
    parser.add_argument("--window-years", type=float, default=3.0,
                        help="rolling window 길이 (년)")
    args = parser.parse_args()

    prices = dm.load_multi_prices(args.symbols)
    if prices.empty:
        print("데이터 없음.")
        return 1

    print("=" * 90)
    print(f"Walk-Forward 검증")
    print("=" * 90)
    print(f"  Universe : {args.symbols}")
    print(f"  기간     : {prices.index[0].date()} → {prices.index[-1].date()} ({len(prices)}일)")
    print(f"  Rolling  : {args.window_years}년 window, 60일 slide")

    # 전략별 equity 생성
    strategies = {}
    for s in args.strategies:
        if s == "dm":
            strategies["DM (12-month)"] = _run_dm_eq(prices)
        elif s == "faber":
            strategies["Faber TAA (10MA)"] = _run_faber_eq(prices)
        elif s == "vaa":
            # offensive = 주식, defensive = 채권
            offensive = [s for s in args.symbols if s != "148070"]
            defensive = ["148070"] if "148070" in args.symbols else []
            strategies["VAA (13612U)"] = _run_vaa_eq(prices, offensive, defensive)
        elif s == "bh":
            strategies["BH equal-weight"] = (prices.div(prices.iloc[0])).mean(axis=1)

    window_days = int(args.window_years * 252)

    # ─── 결과 출력 ───
    print("\n" + "=" * 90)
    print("YEARLY FOLDS — 시기별 분포")
    print("=" * 90)
    for name, eq in strategies.items():
        folds = yearly_breakdown(eq)
        print_yearly(name, folds)

    print("\n" + "=" * 90)
    print(f"ROLLING {args.window_years}-YEAR WINDOWS — 강건성 분포")
    print("=" * 90)
    for name, eq in strategies.items():
        stats = rolling_window_stats(eq, window_days=window_days)
        print_rolling(name, stats)

    # ─── 비교 표 ───
    print("\n" + "=" * 90)
    print("STRATEGY COMPARISON — Rolling 3-year stats")
    print("=" * 90)
    print(f"  {'전략':<25} {'mean CAGR':>10} {'mean Sharpe':>13} {'p5 Sharpe':>11} "
          f"{'worst MDD':>11} {'+CAGR%':>8}")
    print("  " + "-" * 85)

    rows = []
    for name, eq in strategies.items():
        stats = rolling_window_stats(eq, window_days=window_days)
        rows.append((name, stats))
        print(
            f"  {name:<25} "
            f"{stats.mean_cagr*100:>+9.2f}% "
            f"{stats.mean_sharpe:>13.2f} "
            f"{stats.p5_sharpe:>11.2f} "
            f"{stats.worst_mdd*100:>+10.2f}% "
            f"{stats.pct_positive_cagr*100:>7.1f}%"
        )

    # 가장 강건한 전략
    if rows:
        best = max(rows, key=lambda r: r[1].mean_sharpe)
        print(f"\n  → Rolling Sharpe 평균 1위: {best[0]} ({best[1].mean_sharpe:.2f})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
