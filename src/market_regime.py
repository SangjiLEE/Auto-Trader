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


# ─── 슬리브별 시장-와이드 체제 (Codex 권장 — VIX 단독 대신) ───

# KR 슬리브 → 069500 (KODEX 200)
# US 슬리브 → SPY
SLEEVE_BENCHMARKS = {
    "KR": "069500",
    "US": "SPY",
}


def detect_market_regime(sleeve: str = "KR") -> str:
    """
    슬리브 (KR/US) 의 시장-와이드 체제.

    [Codex 권장 — Phase 2]:
      기존 detect_regime() 은 거래 종목 자체의 200MA 사용 → 자기 참조적.
      슬리브 진입 차단 결정에는 시장-와이드 신호 (KOSPI 200, SPY) 가 적절.

    KR 슬리브: 069500 (KODEX 200) 의 종가/MA/ADX
    US 슬리브: SPY 의 종가/MA/ADX

    개별 종목 시그널은 그대로 detect_regime() 사용 (BULL/RANGE/BEAR 룰 결정).
    슬리브 BEAR 면 → 그 슬리브 전체 신규 진입 차단.

    반환: 'BULL' | 'BEAR' | 'RANGE' | 'UNAVAILABLE' (데이터 없음)
    """
    from . import db
    from . import indicators

    sleeve = sleeve.upper()
    benchmark = SLEEVE_BENCHMARKS.get(sleeve)
    if not benchmark:
        return "UNAVAILABLE"

    with db.connection() as conn:
        df = pd.read_sql_query(
            "SELECT date, open, high, low, close, volume FROM daily_candles "
            "WHERE symbol = ? ORDER BY date DESC LIMIT 250",
            conn, params=(benchmark,), parse_dates=["date"], index_col="date",
        )

    if df.empty or len(df) < 200:
        return "UNAVAILABLE"

    df = df.sort_index()
    df = indicators.attach_all(df)
    return detect_regime(df.iloc[-1])


def get_sleeve_regime_summary() -> dict:
    """KR / US 슬리브 체제 한눈 요약. 운영 보고서용."""
    return {
        "KR": detect_market_regime("KR"),
        "US": detect_market_regime("US"),
    }
