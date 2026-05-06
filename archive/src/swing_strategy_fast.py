"""
빠른 스윙 (1~2일 보유) 전략 시그널.

기존 swing_strategy 보다 룰 완화 + 청산 가속:

진입 (4 AND, 기존 6 AND 에서 줄임):
  F1. 종가 > 20일 MA (단기 우상향)
  F2. 전환선 > 기준선 (일목 단기 강세)
  F3. RSI(14) ∈ [30, 75] (강세 구간 넓게)
  F4. 거래량 > 20일 평균 × 1.2

추가 보강 (선택적, 1개라도 만족하면 가산점):
  B1. 볼린저 하단 근처 반등 (bb_pct < 0.2 → > 0.3 으로 회복)
  B2. ATR 비율 적절 (변동성이 너무 크지 않음)

청산 (5 OR):
  X1. 손절 -1.5% (또는 ATR 기반 동적 손절)
  X2. 익절 +3% (또는 볼린저 상단 도달)
  X3. 시간 손절 2일
  X4. 종가 < 20MA (단기 추세 깨짐)
  X5. RSI > 80 (강한 과매수)
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# 빠른 스윙 파라미터
STOP_LOSS_PCT = -0.015           # -1.5% (기존 -3%)
BREAKEVEN_TRIGGER_PCT = 0.015    # +1.5% 도달 시 손절선 0%로
TAKE_PROFIT_PCT = 0.03           # +3% (기존 +10%)
RSI_ENTRY_MIN = 30               # 30~75 (기존 40~65)
RSI_ENTRY_MAX = 75
RSI_OVERBOUGHT = 80              # 80 (기존 75)
VOLUME_SPIKE_MULT = 1.2          # 1.2배 (기존 1.5배)
TIME_STOP_DAYS = 2               # 2일 (기존 10일)
REENTRY_COOLDOWN_DAYS = 2        # 2일 (기존 5일)

# ATR 기반 동적 손절 (선택적, 활성화 시 STOP_LOSS_PCT 무시)
USE_ATR_STOP = True
ATR_STOP_MULT = 1.5              # 종가 - 1.5×ATR 이 손절선


@dataclass
class Position:
    entry_date: pd.Timestamp
    entry_price: float
    qty: int
    breakeven_triggered: bool = False
    initial_atr: float = 0.0  # 진입 시점 ATR 저장 (동적 손절용)


@dataclass
class EntrySignal:
    valid: bool
    reasons: list[str]


@dataclass
class ExitSignal:
    should_exit: bool
    reason: str


def check_entry(row: pd.Series) -> EntrySignal:
    """빠른 스윙 진입 여부 체크 (4 AND)."""
    reasons: list[str] = []

    close = row.get("close")
    if pd.isna(close):
        return EntrySignal(False, ["close 없음"])

    # F1. 20MA 위
    ma20 = row.get("ma20")
    if pd.isna(ma20) or close <= ma20:
        return EntrySignal(False, ["F1 20MA 아래"])
    reasons.append("F1 20MA 위")

    # F2. 전환선 > 기준선
    tenkan = row.get("tenkan")
    kijun = row.get("kijun")
    if pd.isna(tenkan) or pd.isna(kijun) or tenkan <= kijun:
        return EntrySignal(False, ["F2 전환선 ≤ 기준선"])
    reasons.append("F2 일목 강세")

    # F3. RSI 강세
    rsi14 = row.get("rsi14")
    if pd.isna(rsi14) or not (RSI_ENTRY_MIN <= rsi14 <= RSI_ENTRY_MAX):
        return EntrySignal(False, [f"F3 RSI 범위 밖 ({rsi14:.1f})"])
    reasons.append(f"F3 RSI {rsi14:.1f}")

    # F4. 거래량 스파이크
    vol = row.get("volume")
    vol_ma = row.get("vol_ma20")
    if pd.isna(vol) or pd.isna(vol_ma) or vol <= vol_ma * VOLUME_SPIKE_MULT:
        return EntrySignal(False, ["F4 거래량 부족"])
    reasons.append("F4 거래량 OK")

    # 보강 정보 (참고용, 진입 막지는 않음)
    bb_pct = row.get("bb_pct")
    if pd.notna(bb_pct):
        if bb_pct < 0.3:
            reasons.append(f"BB 하단부 (pct {bb_pct:.2f})")
        elif bb_pct > 0.8:
            reasons.append(f"BB 상단 근접 (pct {bb_pct:.2f})")

    return EntrySignal(True, reasons)


def check_exit(
    row: pd.Series,
    position: Position,
    current_date: pd.Timestamp,
) -> ExitSignal:
    """빠른 스윙 청산 여부 체크."""
    close = row.get("close")
    if pd.isna(close):
        return ExitSignal(False, "")

    entry_price = position.entry_price
    pnl_pct = (close - entry_price) / entry_price

    # breakeven 트리거 (+1.5% 찍으면 손절선 0%로)
    if not position.breakeven_triggered and pnl_pct >= BREAKEVEN_TRIGGER_PCT:
        position.breakeven_triggered = True

    # X1. 동적 손절 (ATR 기반 또는 고정 -1.5%)
    if position.breakeven_triggered:
        if pnl_pct <= 0:
            return ExitSignal(True, f"손절(breakeven) {pnl_pct*100:+.2f}%")
    else:
        if USE_ATR_STOP and position.initial_atr > 0:
            atr_stop_price = entry_price - ATR_STOP_MULT * position.initial_atr
            if close <= atr_stop_price:
                return ExitSignal(
                    True,
                    f"ATR손절 {pnl_pct*100:+.2f}% (-{ATR_STOP_MULT}×ATR)",
                )
        else:
            if pnl_pct <= STOP_LOSS_PCT:
                return ExitSignal(True, f"손절 {pnl_pct*100:+.2f}%")

    # X2. 고정 익절
    if pnl_pct >= TAKE_PROFIT_PCT:
        return ExitSignal(True, f"익절 {pnl_pct*100:+.2f}%")

    # 추가: 볼린저 상단 도달 시 익절
    bb_upper = row.get("bb_upper")
    if pd.notna(bb_upper) and close >= bb_upper and pnl_pct > 0.005:
        return ExitSignal(True, f"BB 상단 익절 {pnl_pct*100:+.2f}%")

    # X4. 20MA 이탈
    ma20 = row.get("ma20")
    if pd.notna(ma20) and close < ma20:
        return ExitSignal(True, "20MA 이탈")

    # X5. RSI 과매수
    rsi14 = row.get("rsi14")
    if pd.notna(rsi14) and rsi14 > RSI_OVERBOUGHT:
        return ExitSignal(True, f"RSI 과매수 {rsi14:.1f}")

    # X3. 시간 손절
    days_held = (current_date - position.entry_date).days
    if days_held >= TIME_STOP_DAYS and pnl_pct < TAKE_PROFIT_PCT * 0.3:
        return ExitSignal(True, f"시간손절 {days_held}일 {pnl_pct*100:+.2f}%")

    return ExitSignal(False, "")
