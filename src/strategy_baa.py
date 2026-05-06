"""
Bold Asset Allocation (BAA-G12).

출처: Wouter J. Keller, Jan Willem Keuning,
     "Generalized Protective Momentum" (2022 working paper)
     https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4166845

VAA (2017) 의 진화형. 핵심 차이:
  - VAA: winner-take-all (1자산 100%)
  - BAA: top-N 분산 (top-6 offensive 또는 top-3 defensive)
  - Canary 신호 더 단순 (TIP, BIL 만 평가)

룰 (G12 = Global 12-asset):
  Canary universe (위기 조기 경보):
    - TIP (TIPS)
    - BIL (T-Bill)
    이 두 자산의 13612U momentum score 평가:
      - 모두 양수 → "Risk On" (Aggressive 모드)
      - 하나라도 음수 → "Risk Off" (Balanced/Defensive 모드)

  Aggressive (Risk On):
    Offensive 12-asset 중 13612U top 6 자산을 1/6 weight 매수

  Balanced (Risk Off):
    Defensive universe 중 SMA filter 통과한 자산 동일 weight

장점:
  - Top-N 분산 → VAA 대비 변동성 ↓, 거래 빈도 ↑
  - 매월 1회 점검이지만 6 자산 weight 매번 재조정
  - 사용자 우려 ("매매 적다") 해결
"""
from __future__ import annotations

import pandas as pd

from . import strategy_vaa as vaa


def baa_g12_signal(
    prices: pd.DataFrame,
    canary: list[str],
    offensive: list[str],
    defensive: list[str],
    top_n_offensive: int = 6,
    sma_months: int = 12,
) -> pd.DataFrame:
    """
    BAA-G12 신호 — 매월 weight 분배.

    반환: monthly index, columns = 자산, value = weight (0~1, sum to 1.0)
    """
    monthly = prices.resample("ME").last()
    if not monthly.empty and not prices.empty:
        if prices.index[-1] < monthly.index[-1]:
            monthly = monthly.iloc[:-1]

    score = vaa.momentum_13612u(monthly)

    # Defensive SMA filter (12개월 이동평균 위만 매수 가능)
    sma = monthly.rolling(window=sma_months, min_periods=sma_months).mean()
    above_sma = (monthly > sma).astype(float)

    weights_list = []
    for dt in monthly.index:
        weights = pd.Series(0.0, index=prices.columns)

        # 카나리 신호
        canary_syms = [s for s in canary if s in score.columns]
        canary_scores = score.loc[dt, canary_syms]
        if canary_scores.empty or canary_scores.isna().any():
            weights_list.append(weights)
            continue

        if (canary_scores > 0).all():
            # Risk On — Aggressive: top N offensive
            off_syms = [s for s in offensive if s in score.columns]
            off_scores = score.loc[dt, off_syms].dropna()
            if len(off_scores) > 0:
                top = off_scores.nlargest(min(top_n_offensive, len(off_scores)))
                # 각 자산 1/N weight
                w = 1.0 / len(top)
                for sym in top.index:
                    weights[sym] = w
        else:
            # Risk Off — Balanced: defensive SMA-filtered, equal weight
            def_syms = [s for s in defensive if s in score.columns]
            def_scores = score.loc[dt, def_syms].dropna()
            # SMA 필터: 가격이 SMA 위인 자산만
            valid_def = []
            for sym in def_scores.index:
                if sym in above_sma.columns and above_sma.loc[dt, sym] > 0:
                    valid_def.append(sym)
            if not valid_def:
                # 모두 SMA 아래 → CASH (= BIL 100% 또는 weight 0)
                if "BIL" in prices.columns:
                    weights["BIL"] = 1.0
            else:
                w = 1.0 / len(valid_def)
                for sym in valid_def:
                    weights[sym] = w

        weights_list.append(weights)

    return pd.DataFrame(weights_list, index=monthly.index)


def run_baa_backtest(
    prices: pd.DataFrame,
    weights_monthly: pd.DataFrame,
    cost_model_fn=None,
    cost: float = 0.003,
) -> tuple[pd.Series, pd.Series, pd.DataFrame]:
    """
    BAA weight schedule → 일별 운영 + 비용.

    반환: (equity, returns, daily_weights)
    """
    daily_weights = weights_monthly.reindex(prices.index, method="ffill")
    daily_weights_lagged = daily_weights.shift(1).fillna(0.0)

    # 자산별 일별 수익
    asset_returns = prices.pct_change().fillna(0.0)

    # 전략 수익 = sum(weights × returns)
    strategy_returns = (daily_weights_lagged * asset_returns).sum(axis=1)

    # 비용: weight 변화 절댓값 × cost
    weight_changes = daily_weights_lagged.diff().abs().fillna(daily_weights_lagged.abs())
    if cost_model_fn is None:
        total_change = weight_changes.sum(axis=1)
        cost_drag = total_change * (cost / 2)
    else:
        cost_drag = pd.Series(0.0, index=prices.index)
        for sym in prices.columns:
            sym_cm = cost_model_fn(str(sym))
            sym_changes = daily_weights_lagged[sym].diff().fillna(daily_weights_lagged[sym])
            buy_changes = sym_changes.clip(lower=0)
            sell_changes = (-sym_changes).clip(lower=0)
            cost_drag = (
                cost_drag
                + buy_changes * sym_cm.buy_total
                + sell_changes * sym_cm.sell_total
            )
    strategy_returns = strategy_returns - cost_drag

    equity = (1 + strategy_returns).cumprod()
    return equity, strategy_returns, daily_weights_lagged
