"""
스윙 트레이딩 전략 시그널.

진입 조건 (6개 AND — 모두 만족해야 매수):
  E1. 종가 > 일목 구름 상단 (구름 위)
  E2. 종가 > 200일 이평
  E3. 20MA > 60MA > 120MA (이평 정배열)
  E4. RSI(14) in [40, 65] (강세지만 과매수 아님)
  E5. 거래량 > 20일 평균 × 1.5 (의미 있는 매수세)
  E6. 전환선 > 기준선 (일목 강세)

청산 조건 (6개 OR — 하나라도 해당하면 전량 매도):
  X1. 동적 손절 (breakeven stop):
      - 최고 수익 < +5% → 진입가 대비 -3%
      - 최고 수익 >= +5% → 진입가 대비 0% (breakeven, 손실 없이 청산)
  X2. 고정 익절: 진입가 대비 +10%
  X3. 구름 이탈: 종가 < 구름 하단
  X4. 기준선 하향 2% 돌파: 종가 < 기준선 × 0.98 (노이즈 필터)
  X5. 과매수 + 이평 이탈: RSI > 75 AND 종가 < 20MA
  X6. 시간 손절: 진입 후 10거래일 경과했고 수익률 < 0
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# 튜닝 가능 파라미터
STOP_LOSS_PCT = -0.03
BREAKEVEN_TRIGGER_PCT = 0.05   # +5% 도달하면 손절선을 breakeven으로
TAKE_PROFIT_PCT = 0.10
RSI_ENTRY_MIN = 40
RSI_ENTRY_MAX = 65
RSI_OVERBOUGHT = 75
VOLUME_SPIKE_MULT = 1.5
TIME_STOP_DAYS = 10
KIJUN_EXIT_BUFFER = 0.02       # 기준선 이탈 판정에 2% 버퍼
REENTRY_COOLDOWN_DAYS = 5      # 같은 종목 청산 후 재진입 금지 기간


@dataclass
class Position:
    entry_date: pd.Timestamp
    entry_price: float
    qty: int
    breakeven_triggered: bool = False


@dataclass
class EntrySignal:
    valid: bool
    reasons: list[str]  # 만족한 조건들


@dataclass
class ExitSignal:
    should_exit: bool
    reason: str  # 청산 사유 (하나만)


def check_entry(row: pd.Series) -> EntrySignal:
    """지표 붙은 한 줄 → 진입 가능 여부.

    row: indicators.attach_all() 거친 DataFrame의 한 행.
    """
    reasons: list[str] = []

    close = row.get("close")
    if pd.isna(close):
        return EntrySignal(False, ["close 없음"])

    # E1. 구름 위
    cloud_top = row.get("cloud_top")
    if pd.isna(cloud_top) or close <= cloud_top:
        return EntrySignal(False, ["E1 구름 위 실패"])
    reasons.append("E1 구름 위")

    # E2. 200MA 위
    ma200 = row.get("ma200")
    if pd.isna(ma200) or close <= ma200:
        return EntrySignal(False, ["E2 200MA 아래"])
    reasons.append("E2 200MA 위")

    # E3. 이평 정배열 (20 > 60 > 120)
    ma20 = row.get("ma20")
    ma60 = row.get("ma60")
    ma120 = row.get("ma120")
    if any(pd.isna(x) for x in (ma20, ma60, ma120)):
        return EntrySignal(False, ["E3 이평 데이터 부족"])
    if not (ma20 > ma60 > ma120):
        return EntrySignal(False, ["E3 정배열 아님"])
    reasons.append("E3 정배열")

    # E4. RSI 구간
    rsi14 = row.get("rsi14")
    if pd.isna(rsi14) or not (RSI_ENTRY_MIN <= rsi14 <= RSI_ENTRY_MAX):
        return EntrySignal(False, [f"E4 RSI 범위 벗어남 ({rsi14:.1f})"])
    reasons.append(f"E4 RSI {rsi14:.1f}")

    # E5. 거래량 스파이크
    vol = row.get("volume")
    vol_ma = row.get("vol_ma20")
    if pd.isna(vol) or pd.isna(vol_ma) or vol <= vol_ma * VOLUME_SPIKE_MULT:
        return EntrySignal(False, ["E5 거래량 부족"])
    reasons.append("E5 거래량 스파이크")

    # E6. 전환선 > 기준선
    tenkan = row.get("tenkan")
    kijun = row.get("kijun")
    if pd.isna(tenkan) or pd.isna(kijun) or tenkan <= kijun:
        return EntrySignal(False, ["E6 전환선 ≤ 기준선"])
    reasons.append("E6 일목 강세")

    return EntrySignal(True, reasons)


def check_exit(
    row: pd.Series,
    position: Position,
    current_date: pd.Timestamp,
) -> ExitSignal:
    """지표 붙은 한 줄 + 포지션 정보 → 청산 여부."""
    close = row.get("close")
    if pd.isna(close):
        return ExitSignal(False, "")

    entry_price = position.entry_price
    pnl_pct = (close - entry_price) / entry_price

    # breakeven 트리거: 한 번 +5% 찍으면 이후 손절선을 0%로 상향
    if not position.breakeven_triggered and pnl_pct >= BREAKEVEN_TRIGGER_PCT:
        position.breakeven_triggered = True

    # X1. 동적 손절 (breakeven 여부에 따라 기준선 변동)
    effective_stop = 0.0 if position.breakeven_triggered else STOP_LOSS_PCT
    if pnl_pct <= effective_stop:
        tag = "손절(breakeven)" if position.breakeven_triggered else "손절"
        return ExitSignal(True, f"{tag} {pnl_pct*100:+.2f}%")

    # X2. 고정 익절
    if pnl_pct >= TAKE_PROFIT_PCT:
        return ExitSignal(True, f"익절 {pnl_pct*100:+.2f}%")

    # X3. 구름 이탈
    cloud_bottom = row.get("cloud_bottom")
    if pd.notna(cloud_bottom) and close < cloud_bottom:
        return ExitSignal(True, "구름 이탈")

    # X4. 기준선 하향 2% 이상 돌파 (버퍼로 노이즈 제거)
    kijun = row.get("kijun")
    if pd.notna(kijun) and close < kijun * (1 - KIJUN_EXIT_BUFFER):
        return ExitSignal(True, "기준선 이탈")

    # X5. RSI 과매수 + 이평 이탈
    rsi14 = row.get("rsi14")
    ma20 = row.get("ma20")
    if (
        pd.notna(rsi14)
        and pd.notna(ma20)
        and rsi14 > RSI_OVERBOUGHT
        and close < ma20
    ):
        return ExitSignal(True, f"과매수 이탈 (RSI {rsi14:.1f})")

    # X6. 시간 손절
    days_held = (current_date - position.entry_date).days
    if days_held >= TIME_STOP_DAYS and pnl_pct < 0:
        return ExitSignal(True, f"시간손절 {days_held}일 {pnl_pct*100:+.2f}%")

    return ExitSignal(False, "")
