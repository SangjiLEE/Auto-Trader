"""
Path C — Tactical Asset Allocation 비교 백테스트.

[Day 9 후속] DM vs Faber vs VAA 동일 universe / 동일 기간 / 동일 비용 비교.

목적:
  Day 9 재측정에서 발견:
    - v3 (체제 어댑티브 swing) 은 BH 한참 못 이김
    - v4 의 우수성 주장은 데이터 leakage 환상
    - DM 4-asset 만 진짜 살아남음 (Sharpe 1.10)
  → 학술 검증된 다른 TAA 와 비교해 더 나은 path 찾기

비교 전략:
  1. Buy & Hold (069500 단일, 한국 패시브)
  2. Buy & Hold (4-asset 동일 weight, 단순 분산)
  3. Dual Momentum (Antonacci 12-month) ← 현재 운영
  4. Faber TAA (10MA monthly) ← Mebane Faber 2007
  5. Faber TAA (12MA 변형)
  6. VAA (Keller-Keuning 13612U) ← 가장 최신, 학술
  7. VAA + 다른 defensive 조합

실행:
  python -m src.backtest_tactical
  python -m src.backtest_tactical --period 10y  # 3-asset 10년
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import pandas as pd

from . import cost_model as cm
from . import dual_momentum as dm
from . import metrics as mt
from . import strategy_faber as faber
from . import strategy_vaa as vaa


@dataclass
class BacktestRow:
    name: str
    cagr: float
    sharpe: float
    mdd: float
    calmar: float
    underwater_days: int
    recovery_days: int | None
    total_return: float
    rank_by_sharpe: int = 0  # 채워질 예정


def run_buy_and_hold(prices: pd.DataFrame) -> tuple[pd.Series, str]:
    """단일 자산 BH (가장 좋은 BH 자산 자동 선택, 비교용)."""
    best_sym = max(prices.columns, key=lambda s: prices[s].iloc[-1] / prices[s].iloc[0])
    eq = prices[best_sym] / prices[best_sym].iloc[0]
    return eq, f"BH ({best_sym})"


def run_equal_weight_bh(prices: pd.DataFrame, cost_model_fn=None) -> pd.Series:
    """4-asset 동일 weight BH (rebalancing 없음)."""
    norm = prices.div(prices.iloc[0])
    return norm.mean(axis=1)


def run_dm(prices, lookback=12, cost_model_fn=None):
    sig = dm.dual_momentum_signal(prices, lookback)
    eq, _, _ = dm.run_dual_momentum(prices, sig, cost_model_fn=cost_model_fn)
    return eq


def run_faber(prices, ma_months=10, cost_model_fn=None):
    sig = faber.faber_signal(prices, ma_months=ma_months)
    eq, _, _ = faber.run_faber_backtest(prices, sig, cost_model_fn=cost_model_fn)
    return eq


def run_vaa(prices, offensive, defensive, cost_model_fn=None):
    sig = vaa.vaa_signal(prices, offensive, defensive)
    eq, _, _ = vaa.run_vaa_backtest(prices, sig, cost_model_fn=cost_model_fn)
    return eq


def metrics_row(name: str, eq: pd.Series) -> BacktestRow:
    m = mt.compute_metrics(eq)
    return BacktestRow(
        name=name, cagr=m.cagr, sharpe=m.sharpe, mdd=m.mdd, calmar=m.calmar,
        underwater_days=m.underwater_days, recovery_days=m.recovery_days,
        total_return=m.total_return,
    )


def print_comparison(rows: list[BacktestRow], period_label: str) -> None:
    # Sharpe 기준 랭킹
    by_sharpe = sorted(rows, key=lambda r: -r.sharpe)
    for i, r in enumerate(by_sharpe, 1):
        r.rank_by_sharpe = i

    print()
    print("=" * 100)
    print(f"비교 결과 — {period_label}")
    print("=" * 100)
    print(
        f"  {'#':<3} {'전략':<35} {'누적':>10} {'CAGR':>9} "
        f"{'Sharpe':>7} {'MDD':>9} {'Calmar':>7} {'Underw.':>8}"
    )
    print("  " + "-" * 95)
    for r in rows:
        rec = f"{r.recovery_days}일" if r.recovery_days is not None else "진행중"
        print(
            f"  {r.rank_by_sharpe:<3} {r.name:<35} "
            f"{r.total_return*100:>+8.2f}%  "
            f"{r.cagr*100:>+7.2f}% "
            f"{r.sharpe:>7.2f} "
            f"{r.mdd*100:>+8.2f}% "
            f"{r.calmar:>7.2f} "
            f"{r.underwater_days:>6}일"
        )

    print()
    print("랭킹 (Sharpe 기준 best → worst):")
    for r in by_sharpe:
        print(f"  {r.rank_by_sharpe}. {r.name:<35} Sharpe {r.sharpe:.2f} | Calmar {r.calmar:.2f} | MDD {r.mdd*100:+.2f}%")


def run_3asset_10yr() -> list[BacktestRow]:
    """3-asset 10년 비교 — 069500/133690/148070."""
    print("\n" + "=" * 100)
    print("3-asset (069500 + 133690 + 148070) — 약 10년")
    print("=" * 100)

    prices = dm.load_multi_prices(["069500", "133690", "148070"])
    print(f"기간: {prices.index[0].date()} → {prices.index[-1].date()} ({len(prices)}일)")

    rows = []
    # 1. BH 069500
    bh_069500 = prices["069500"] / prices["069500"].iloc[0]
    rows.append(metrics_row("BH 069500 (KOSPI 200)", bh_069500))

    # 2. BH 4-asset equal weight
    rows.append(metrics_row("BH equal-weight 3-asset", run_equal_weight_bh(prices)))

    # 3. DM
    rows.append(metrics_row("DM (12-month, Antonacci)", run_dm(prices, 12, cm.get_cost_model)))

    # 4. Faber 10MA
    rows.append(metrics_row("Faber TAA (10MA, multi-asset)", run_faber(prices, 10, cm.get_cost_model)))

    # 5. Faber 12MA
    rows.append(metrics_row("Faber TAA (12MA 변형)", run_faber(prices, 12, cm.get_cost_model)))

    # 6. VAA
    rows.append(metrics_row(
        "VAA (offensive: 069500/133690)",
        run_vaa(prices, ["069500", "133690"], ["148070"], cm.get_cost_model),
    ))

    return rows


def run_4asset_5yr() -> list[BacktestRow]:
    """4-asset 5.7년 비교."""
    print("\n" + "=" * 100)
    print("4-asset (069500 + 133690 + 360750 + 148070) — 약 5.7년")
    print("=" * 100)

    prices = dm.load_multi_prices(["069500", "133690", "360750", "148070"])
    print(f"기간: {prices.index[0].date()} → {prices.index[-1].date()} ({len(prices)}일)")

    rows = []
    bh_069500 = prices["069500"] / prices["069500"].iloc[0]
    rows.append(metrics_row("BH 069500 (KOSPI 200)", bh_069500))

    rows.append(metrics_row("BH equal-weight 4-asset", run_equal_weight_bh(prices)))

    rows.append(metrics_row("DM (12-month, Antonacci)", run_dm(prices, 12, cm.get_cost_model)))

    rows.append(metrics_row("Faber TAA (10MA, multi-asset)", run_faber(prices, 10, cm.get_cost_model)))

    rows.append(metrics_row("Faber TAA (12MA 변형)", run_faber(prices, 12, cm.get_cost_model)))

    # VAA — 3 offensive (주식) + 1 defensive (채권)
    rows.append(metrics_row(
        "VAA (offensive 3 / defensive 148070)",
        run_vaa(prices, ["069500", "133690", "360750"], ["148070"], cm.get_cost_model),
    ))

    # VAA 변형 — defensive 에 069500 도 (보수적)
    rows.append(metrics_row(
        "VAA-defensive ( + 069500 def )",
        run_vaa(prices, ["133690", "360750"], ["148070", "069500"], cm.get_cost_model),
    ))

    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Path C: TAA 비교 백테스트")
    parser.add_argument("--period", choices=["10y", "5y", "both"], default="both",
                        help="기간 (10y=3-asset / 5y=4-asset / both)")
    args = parser.parse_args()

    if args.period in ("10y", "both"):
        rows10 = run_3asset_10yr()
        print_comparison(rows10, "3-asset / 약 10년")

    if args.period in ("5y", "both"):
        rows5 = run_4asset_5yr()
        print_comparison(rows5, "4-asset / 약 5.7년")

    print("\n" + "=" * 100)
    print("Path C 백테스트 완료")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    sys.exit(main())
