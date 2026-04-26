"""
멀티 전략 백테스트: Dual Momentum (Tier 1) + Swing (Tier 2).

로직:
  1. 전체 자본을 두 슬롯으로 분리 (기본 70% DM / 30% Swing)
  2. 각 슬롯이 독립 자본으로 운영 — 서로 간섭 없음
  3. 최종 equity = DM equity + Swing equity
  4. 개별 및 합산 지표 비교 출력

목적: 스윙 전략의 남는 자본을 Dual Momentum에 투입해서 자본 효율 극대화.
      + 두 전략이 서로 다른 시기에 수익 나서 MDD 분산 효과.

실행:
  python -m src.multi_strategy_backtest                    # 70/30 기본
  python -m src.multi_strategy_backtest --dm 0.5           # 50/50
  python -m src.multi_strategy_backtest --capital 5000000
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from . import dual_momentum as dm
from . import swing_backtest as sb

# 기본 설정
INITIAL_CAPITAL = 10_000_000
DM_ALLOCATION = 0.70           # 70% DM
SWING_ALLOCATION = 0.30        # 30% Swing

DM_UNIVERSE = ["069500", "133690", "148070"]
DM_LOOKBACK = 12
DM_COST = 0.003

SWING_UNIVERSE = ["069500", "133690", "005930"]


# ── 각 슬롯 실행 ────────────────────────────────

def run_dm_slot(capital: float) -> pd.Series:
    """DM 슬롯. 초기 자본 기준 equity 시리즈 반환."""
    prices = dm.load_multi_prices(DM_UNIVERSE)
    if prices.empty:
        return pd.Series(dtype=float)
    signal = dm.dual_momentum_signal(prices, DM_LOOKBACK)
    equity_ratio, _, _ = dm.run_dual_momentum(prices, signal, DM_COST)
    return equity_ratio * capital


def run_swing_slot(capital: float) -> tuple[pd.Series, list]:
    """Swing 슬롯. (equity 시리즈, 거래 목록)."""
    symbol_data: dict[str, pd.DataFrame] = {}
    for sym in SWING_UNIVERSE:
        df = sb.load_symbol_with_indicators(sym)
        if df is not None:
            symbol_data[sym] = df
    if not symbol_data:
        return pd.Series(dtype=float), []
    state = sb.run_backtest(symbol_data, initial_capital=capital)
    equity = pd.Series(state.equity_curve)
    return equity, state.completed_trades


# ── 합산 ──────────────────────────────────────

def combine_equity(
    dm_eq: pd.Series,
    swing_eq: pd.Series,
    dm_initial: float,
    swing_initial: float,
) -> pd.Series:
    """두 시리즈 날짜 정렬 → 합산. 결측은 해당 슬롯 초기값으로 채움."""
    if dm_eq.empty and swing_eq.empty:
        return pd.Series(dtype=float)

    all_dates = sorted(
        set(dm_eq.index.tolist() if not dm_eq.empty else [])
        | set(swing_eq.index.tolist() if not swing_eq.empty else [])
    )
    dm_aligned = dm_eq.reindex(all_dates).ffill().fillna(dm_initial)
    swing_aligned = swing_eq.reindex(all_dates).ffill().fillna(swing_initial)
    return dm_aligned + swing_aligned


# ── 지표 계산 ──────────────────────────────────

def compute_metrics(equity: pd.Series, initial: float) -> dict:
    if equity.empty:
        return {"initial": initial, "final": initial, "total_return": 0,
                "cagr": 0, "sharpe": 0, "mdd": 0, "days": 0}

    final = float(equity.iloc[-1])
    total_return = (final - initial) / initial
    days = len(equity)
    years = days / 252 if days > 0 else 1
    cagr = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

    returns = equity.pct_change().fillna(0)
    sharpe = 0.0
    if returns.std() > 0:
        sharpe = float((returns.mean() * 252) / (returns.std() * (252 ** 0.5)))

    running_max = equity.cummax()
    mdd = float(((equity - running_max) / running_max).min())

    return {
        "initial": initial,
        "final": final,
        "total_return": total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "mdd": mdd,
        "days": days,
    }


# ── 출력 ──────────────────────────────────────

def print_slot_row(label: str, m: dict) -> None:
    print(
        f"  {label:<14} "
        f"{m['initial']:>13,.0f}  →  "
        f"{m['final']:>13,.0f}  "
        f"({m['total_return']*100:>+7.2f}%)  "
        f"CAGR {m['cagr']*100:>+6.2f}%  "
        f"Sharpe {m['sharpe']:>5.2f}  "
        f"MDD {m['mdd']*100:>+6.2f}%"
    )


def print_comparison(
    dm_m: dict,
    swing_m: dict,
    combined_m: dict,
    dm_alloc: float,
    swing_alloc: float,
) -> None:
    print("\n" + "=" * 96)
    print("결과 비교")
    print("=" * 96)
    print(
        f"  {'슬롯':<14} "
        f"{'초기 자본':>13}     "
        f"{'최종 자본':>13}   "
        f"{'누적':>9}   "
        f"{'CAGR':>10}   "
        f"{'Sharpe':>7}   "
        f"{'MDD':>9}"
    )
    print("-" * 96)
    print_slot_row(f"DM ({dm_alloc*100:.0f}%)", dm_m)
    print_slot_row(f"Swing ({swing_alloc*100:.0f}%)", swing_m)
    print("-" * 96)
    print_slot_row("전체", combined_m)


# ── 메인 ──────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="멀티 전략 백테스트")
    parser.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    parser.add_argument(
        "--dm",
        type=float,
        default=DM_ALLOCATION,
        help="DM 비중 (0~1, 기본 0.7)",
    )
    args = parser.parse_args()

    if not 0 <= args.dm <= 1:
        print("--dm 은 0과 1 사이 값")
        return 1

    dm_alloc = args.dm
    swing_alloc = 1 - args.dm
    dm_capital = args.capital * dm_alloc
    swing_capital = args.capital * swing_alloc

    print("=" * 96)
    print("멀티 전략 백테스트")
    print("=" * 96)
    print(f"  초기 자본: {args.capital:,.0f} 원")
    print(f"  Tier 1 (DM) {dm_alloc*100:.0f}%    : "
          f"{dm_capital:,.0f} 원  ({DM_UNIVERSE})")
    print(f"  Tier 2 (Swing) {swing_alloc*100:.0f}%: "
          f"{swing_capital:,.0f} 원  ({SWING_UNIVERSE})")
    print()

    print("[1/3] DM 슬롯 실행...")
    dm_equity = run_dm_slot(dm_capital)

    print("[2/3] Swing 슬롯 실행...")
    swing_equity, swing_trades = run_swing_slot(swing_capital)

    print("[3/3] 합산 + 지표 계산...")
    combined = combine_equity(
        dm_equity, swing_equity, dm_capital, swing_capital
    )

    dm_m = compute_metrics(dm_equity, dm_capital)
    swing_m = compute_metrics(swing_equity, swing_capital)
    combined_m = compute_metrics(combined, args.capital)

    print_comparison(dm_m, swing_m, combined_m, dm_alloc, swing_alloc)

    # Swing 거래 요약
    if swing_trades:
        wins = [t for t in swing_trades if t["pnl"] > 0]
        losses = [t for t in swing_trades if t["pnl"] <= 0]
        print(f"\n  Swing 거래: 총 {len(swing_trades)}회 "
              f"(승 {len(wins)} / 패 {len(losses)}, "
              f"승률 {len(wins)/len(swing_trades)*100:.1f}%)")

    # 전략 상관관계
    if not dm_equity.empty and not swing_equity.empty:
        dm_ret = dm_equity.pct_change().dropna()
        swing_ret = swing_equity.pct_change().dropna()
        common = dm_ret.index.intersection(swing_ret.index)
        if len(common) > 30:
            corr = dm_ret.loc[common].corr(swing_ret.loc[common])
            print(f"\n  DM-Swing 일별 수익률 상관계수: {corr:+.3f}")
            if corr < 0.3:
                print("    → 낮은 상관관계: 분산 효과 유의미")
            elif corr < 0.6:
                print("    → 중간 상관관계: 일부 분산 효과")
            else:
                print("    → 높은 상관관계: 분산 효과 제한적")

    return 0


if __name__ == "__main__":
    sys.exit(main())
