"""
백테스트 메트릭 통합 모듈.

[Phase 1] Cross-model consensus (Claude + Codex):
- 기존: Sharpe + MDD + total_return 만
- 추가: Calmar (CAGR/|MDD|), MDD duration, recovery time, Ulcer Index
- 목적: "MDD -25% 한 번" 인지 "MDD -25% + 3년 underwater" 인지 구분
       (Schwager "Hedge Fund Market Wizards" 표준)

이 모듈을 dual_momentum / swing_backtest_v3 / swing_backtest_v4 에서
import 해서 사용.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

PERIODS_PER_YEAR = 252


@dataclass
class BacktestMetrics:
    # 기본 (기존 호환)
    total_return: float          # 누적 수익률 (0.20 = +20%)
    cagr: float                   # 연복리 수익률
    sharpe: float                 # 연환산 Sharpe
    mdd: float                    # 최대 낙폭 (음수)
    # 확장 (Phase 1 신규)
    calmar: float                 # CAGR / |MDD|
    mdd_duration_days: int        # peak → trough 거리 (일)
    recovery_days: int | None     # trough → recovery (None = 진행 중)
    underwater_days: int          # peak → recovery (or 현재까지)
    ulcer_index: float            # sqrt(mean(dd^2))  (volatility-of-downside)
    pct_positive_days: float      # 일 단위 양수 수익일 비율
    best_day: float               # 최고 일일 수익률
    worst_day: float              # 최악 일일 수익률
    final_equity: float           # 최종 자본
    initial_capital: float        # 초기 자본

    def to_dict(self) -> dict:
        return asdict(self)


def _drawdown_series(equity: pd.Series) -> pd.Series:
    """drawdown series (항상 ≤ 0). dd[i] = (equity[i] - cummax[i]) / cummax[i]."""
    if equity.empty:
        return equity
    running_max = equity.cummax()
    return (equity - running_max) / running_max


def _mdd_timing(equity: pd.Series) -> dict:
    """
    최대 낙폭의 시점 분석.

    반환:
      mdd: float (음수)
      mdd_duration_days: peak → trough 일수
      recovery_days: trough → 회복 (None = 아직 회복 안됨)
      underwater_days: peak → 회복 (또는 현재까지)
    """
    empty_result = {
        "mdd": 0.0, "mdd_duration_days": 0,
        "recovery_days": None, "underwater_days": 0,
    }
    if len(equity) < 2:
        return empty_result

    eq = equity.dropna()
    if len(eq) < 2:
        return empty_result

    dd = _drawdown_series(eq)
    if dd.empty or dd.isna().all():
        return empty_result

    mdd = float(dd.min())
    if pd.isna(mdd) or mdd >= 0:
        return empty_result

    # MDD trough 시점
    trough_idx = dd.idxmin()

    # MDD 직전 peak
    pre_trough = eq.loc[:trough_idx]
    peak_value = float(pre_trough.cummax().iloc[-1])
    peak_idx = pre_trough[pre_trough >= peak_value].index[0]

    # 회복 시점 = trough 이후 peak_value 이상 처음 돌파
    post_trough = eq.loc[trough_idx:]
    recovered_mask = post_trough >= peak_value
    if recovered_mask.any():
        recovery_idx = recovered_mask[recovered_mask].index[0]
        recovery_days = (recovery_idx - trough_idx).days
        underwater_days = (recovery_idx - peak_idx).days
    else:
        recovery_idx = None
        recovery_days = None
        underwater_days = (eq.index[-1] - peak_idx).days

    return {
        "mdd": mdd,
        "mdd_duration_days": (trough_idx - peak_idx).days,
        "recovery_days": recovery_days,
        "underwater_days": underwater_days,
    }


def _ulcer_index(equity: pd.Series) -> float:
    """Ulcer Index = sqrt(mean(dd^2)). drawdown 의 RMS, downside volatility 측정."""
    dd = _drawdown_series(equity)
    return float((dd ** 2).mean() ** 0.5)


def compute_metrics(
    equity_curve: dict | pd.Series | list,
    initial_capital: float | None = None,
    periods_per_year: int = PERIODS_PER_YEAR,
) -> BacktestMetrics:
    """
    equity curve 입력 → 모든 메트릭 dict.

    equity_curve:
      - dict[Timestamp, float] (swing_backtest_v3 형식)
      - pd.Series (dual_momentum 형식)
      - list (단순 sequence)
    initial_capital:
      - dual_momentum 처럼 누적 수익률 형태면 1.0 자동 (eq[0])
      - 자본금 단위면 명시 (예: 5_000_000)
    """
    if isinstance(equity_curve, dict):
        eq = pd.Series(equity_curve).sort_index()
    elif isinstance(equity_curve, list):
        eq = pd.Series(equity_curve)
    else:
        eq = equity_curve.sort_index()

    if eq.empty:
        return BacktestMetrics(
            total_return=0.0, cagr=0.0, sharpe=0.0, mdd=0.0,
            calmar=0.0, mdd_duration_days=0, recovery_days=None,
            underwater_days=0, ulcer_index=0.0, pct_positive_days=0.0,
            best_day=0.0, worst_day=0.0,
            final_equity=initial_capital or 1.0,
            initial_capital=initial_capital or 1.0,
        )

    if initial_capital is None:
        initial_capital = float(eq.iloc[0])

    final = float(eq.iloc[-1])
    total_return = (final - initial_capital) / initial_capital

    days = len(eq)
    years = days / periods_per_year if days > 0 else 1
    cagr = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0.0

    returns = eq.pct_change().fillna(0)
    sharpe = 0.0
    if returns.std() > 0:
        sharpe = float(
            (returns.mean() * periods_per_year)
            / (returns.std() * (periods_per_year ** 0.5))
        )

    timing = _mdd_timing(eq)
    mdd = timing["mdd"]

    # Calmar
    calmar = cagr / abs(mdd) if mdd < 0 else (float("inf") if cagr > 0 else 0.0)

    return BacktestMetrics(
        total_return=total_return,
        cagr=cagr,
        sharpe=sharpe,
        mdd=mdd,
        calmar=calmar,
        mdd_duration_days=timing["mdd_duration_days"],
        recovery_days=timing["recovery_days"],
        underwater_days=timing["underwater_days"],
        ulcer_index=_ulcer_index(eq),
        pct_positive_days=float((returns > 0).sum()) / len(returns) if len(returns) else 0.0,
        best_day=float(returns.max()) if len(returns) else 0.0,
        worst_day=float(returns.min()) if len(returns) else 0.0,
        final_equity=final,
        initial_capital=initial_capital,
    )


def format_summary(m: BacktestMetrics, *, currency: str = "원") -> str:
    """메트릭 dict → 사람이 읽을 수 있는 한 블록 요약 텍스트."""
    rec_str = (
        f"{m.recovery_days}일" if m.recovery_days is not None else "진행 중"
    )
    return (
        f"  초기/최종     : {m.initial_capital:>12,.0f} → {m.final_equity:>12,.0f} {currency}\n"
        f"  누적 수익     : {m.total_return*100:>+11.2f}%\n"
        f"  CAGR          : {m.cagr*100:>+11.2f}%\n"
        f"  Sharpe        : {m.sharpe:>12.2f}\n"
        f"  MDD           : {m.mdd*100:>+11.2f}%\n"
        f"  Calmar        : {m.calmar:>12.2f}    [CAGR/|MDD|]\n"
        f"  MDD duration  : {m.mdd_duration_days:>9}일    [peak→trough]\n"
        f"  Recovery      : {rec_str:>11}    [trough→peak 복귀]\n"
        f"  Underwater    : {m.underwater_days:>9}일    [peak→recovery]\n"
        f"  Ulcer Index   : {m.ulcer_index*100:>11.2f}%    [downside RMS]\n"
        f"  +수익일 비율  : {m.pct_positive_days*100:>11.2f}%\n"
        f"  최고/최악일   : {m.best_day*100:>+5.2f}% / {m.worst_day*100:>+5.2f}%"
    )
