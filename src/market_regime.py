"""
시장 체제 분류기.

각 거래일을 BULL / BEAR / RANGE 셋 중 하나로 분류.

기준 (단일 종목 기준):
  BULL : 종가 > 200MA × 1.05 AND 60MA > 200MA AND ADX > 20
  BEAR : 종가 < 200MA × 0.95 AND 60MA < 200MA AND ADX > 20
  RANGE: 그 외 (구분이 명확하지 않은 모든 구간)

200MA × 5% 버퍼는 노이즈 줄이기. ADX > 20 은 추세 강도 확인.
"""
from __future__ import annotations

import pandas as pd

REGIME_BULL = "BULL"
REGIME_BEAR = "BEAR"
REGIME_RANGE = "RANGE"


def detect_regime(row: pd.Series) -> str:
    close = row.get("close")
    ma60 = row.get("ma60")
    ma200 = row.get("ma200")
    adx = row.get("adx14")

    if pd.isna(close) or pd.isna(ma60) or pd.isna(ma200) or pd.isna(adx):
        return REGIME_RANGE

    above_200 = close > ma200 * 1.05
    below_200 = close < ma200 * 0.95
    ma_aligned_up = ma60 > ma200
    ma_aligned_down = ma60 < ma200
    trending = adx > 20

    if above_200 and ma_aligned_up and trending:
        return REGIME_BULL
    if below_200 and ma_aligned_down and trending:
        return REGIME_BEAR
    return REGIME_RANGE


def regime_series(df: pd.DataFrame) -> pd.Series:
    """DataFrame 의 각 행 → regime 라벨 시리즈."""
    return df.apply(detect_regime, axis=1)


def regime_distribution(df: pd.DataFrame) -> dict:
    """체제별 비율 (0~1)."""
    series = regime_series(df)
    counts = series.value_counts()
    total = len(series)
    return {
        REGIME_BULL: counts.get(REGIME_BULL, 0) / total if total else 0,
        REGIME_BEAR: counts.get(REGIME_BEAR, 0) / total if total else 0,
        REGIME_RANGE: counts.get(REGIME_RANGE, 0) / total if total else 0,
    }
