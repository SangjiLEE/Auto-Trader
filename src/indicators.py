"""
기술적 지표 계산 (pandas 순수 구현, 외부 라이브러리 불필요).

지원 지표:
  - 이동평균 (Simple Moving Average, MA)
  - 상대강도지수 (RSI, Wilder's smoothing)
  - 일목균형지표 (Ichimoku Kinko Hyo)
  - 거래량 평균

반환: 원본 인덱스 보존한 Series 또는 DataFrame.
"""
from __future__ import annotations

import pandas as pd


def moving_average(prices: pd.Series, window: int) -> pd.Series:
    """단순 이동평균 (SMA)."""
    return prices.rolling(window=window, min_periods=window).mean()


def rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index (Wilder 스무딩 방식).

    RSI = 100 - 100 / (1 + avg_gain / avg_loss)
    """
    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    # Wilder smoothing = EMA with alpha = 1/period
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss
    rsi_series = 100 - (100 / (1 + rs))
    return rsi_series


def ichimoku(
    df: pd.DataFrame,
    conversion: int = 9,
    base: int = 26,
    span_b: int = 52,
    shift: int = 26,
) -> pd.DataFrame:
    """
    일목균형지표 (Ichimoku Kinko Hyo).

    컬럼:
      - tenkan (전환선): (9일 최고 + 9일 최저) / 2
      - kijun (기준선): (26일 최고 + 26일 최저) / 2
      - span_a (선행스팬1): (전환선 + 기준선) / 2, 26일 앞으로 이동
      - span_b (선행스팬2): (52일 최고 + 52일 최저) / 2, 26일 앞으로 이동
      - chikou (후행스팬): 종가를 26일 뒤로 이동

    구름 상단 = max(span_a, span_b), 하단 = min(span_a, span_b).

    입력 df: high, low, close 컬럼 필요.
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]

    tenkan = (high.rolling(conversion).max() + low.rolling(conversion).min()) / 2
    kijun = (high.rolling(base).max() + low.rolling(base).min()) / 2

    # 선행스팬들을 미래로 shift → 오늘 시점에서 '오늘의 구름'은 26일 전 계산값
    span_a = ((tenkan + kijun) / 2).shift(shift)
    span_b = (
        (high.rolling(span_b).max() + low.rolling(span_b).min()) / 2
    ).shift(shift)

    chikou = close.shift(-shift)

    cloud_top = pd.concat([span_a, span_b], axis=1).max(axis=1)
    cloud_bottom = pd.concat([span_a, span_b], axis=1).min(axis=1)

    return pd.DataFrame(
        {
            "tenkan": tenkan,
            "kijun": kijun,
            "span_a": span_a,
            "span_b": span_b,
            "chikou": chikou,
            "cloud_top": cloud_top,
            "cloud_bottom": cloud_bottom,
        }
    )


def volume_average(volume: pd.Series, window: int = 20) -> pd.Series:
    """거래량 이동평균."""
    return volume.rolling(window=window, min_periods=window).mean()


def bollinger_bands(
    prices: pd.Series, window: int = 20, num_std: float = 2.0
) -> pd.DataFrame:
    """
    볼린저 밴드.

    middle = N일 SMA
    upper = middle + (num_std × N일 표준편차)
    lower = middle - (num_std × N일 표준편차)

    반환 DataFrame 컬럼: bb_middle, bb_upper, bb_lower, bb_width, bb_pct
    bb_width = (upper - lower) / middle  (변동성)
    bb_pct = (price - lower) / (upper - lower)  (밴드 내 위치, 0~1)
    """
    middle = prices.rolling(window=window, min_periods=window).mean()
    std = prices.rolling(window=window, min_periods=window).std()
    upper = middle + num_std * std
    lower = middle - num_std * std
    width = (upper - lower) / middle
    pct = (prices - lower) / (upper - lower)
    return pd.DataFrame(
        {
            "bb_middle": middle,
            "bb_upper": upper,
            "bb_lower": lower,
            "bb_width": width,
            "bb_pct": pct,
        }
    )


def atr(
    df: pd.DataFrame, period: int = 14
) -> pd.Series:
    """
    Average True Range (Wilder smoothing).

    변동성 측정. 손절폭 동적 조정에 유용.
    True Range = max(high-low, |high-prev_close|, |low-prev_close|).

    df 필요 컬럼: high, low, close
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # Wilder smoothing = EMA with alpha = 1/period
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def adx(
    df: pd.DataFrame, period: int = 14
) -> pd.Series:
    """
    Average Directional Index (Wilder).

    추세 강도 측정 (방향 무관, 0~100):
      - ADX < 20: 추세 없음 (박스권, 단타 적합)
      - 20-25: 약한 추세
      - 25-50: 강한 추세 (단타 부적합)
      - > 50: 매우 강한 추세

    df 필요 컬럼: high, low, close
    """
    high = df["high"]
    low = df["low"]
    close = df["close"]

    # True Range
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr_smooth = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    # +DM, -DM
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    plus_dm_raw = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm_raw = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    plus_di = (
        100
        * plus_dm_raw.ewm(alpha=1 / period, adjust=False).mean()
        / atr_smooth.replace(0, pd.NA)
    )
    minus_di = (
        100
        * minus_dm_raw.ewm(alpha=1 / period, adjust=False).mean()
        / atr_smooth.replace(0, pd.NA)
    )

    di_sum = (plus_di + minus_di).replace(0, pd.NA)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def attach_all(df: pd.DataFrame) -> pd.DataFrame:
    """
    주가 DataFrame 에 모든 지표 컬럼 추가.

    입력 df 필요 컬럼: open, high, low, close, volume
    출력 컬럼 추가:
      - ma20, ma60, ma120, ma200
      - rsi14
      - tenkan, kijun, span_a, span_b, chikou, cloud_top, cloud_bottom
      - vol_ma20
    """
    result = df.copy()

    # 이동평균
    for window in [20, 60, 120, 200]:
        result[f"ma{window}"] = moving_average(df["close"], window)

    # RSI
    result["rsi14"] = rsi(df["close"], 14)

    # 일목균형
    ichi = ichimoku(df)
    for col in ichi.columns:
        result[col] = ichi[col]

    # 거래량 평균
    result["vol_ma20"] = volume_average(df["volume"], 20)

    # 볼린저 밴드 (20일, 2σ)
    bb = bollinger_bands(df["close"], window=20, num_std=2.0)
    for col in bb.columns:
        result[col] = bb[col]

    # ATR (14일, 변동성)
    result["atr14"] = atr(df, period=14)

    # ADX (14일, 추세 강도) — 단타 스크리너에서 활용
    result["adx14"] = adx(df, period=14)

    return result
