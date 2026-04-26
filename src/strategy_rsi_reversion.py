"""
RSI 평균회귀 전략.

박스권 + 강세장 종목에서 일시 과매도 → 반등 매매.

진입 (3 AND):
  R1. RSI(14) < 30 (과매도)
  R2. 종가 > 200MA (장기 상승추세 — 떨어지는 칼날 회피)
  R3. 거래량 > 20일 평균 × 1.0 (반등 동력)

청산 (4 OR):
  X1. RSI > 60 (반등 완료, 익절)
  X2. 진입가 대비 +5% (목표 도달)
  X3. 진입가 대비 -3% (손절)
  X4. 7거래일 경과 (평균회귀는 짧음)

특징:
  - 강추세 자산엔 잘 안 통함 (NVDA 같은 종목 RSI 30 거의 안 옴)
  - 박스권 + 약상승 추세에 강함 (069500, IWM 등)
  - v3 와 보완 관계: v3는 강세 진입, RSI는 약세 반등 진입
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# 진입 파라미터
RSI_OVERSOLD = 30
VOLUME_MIN_MULT = 1.0

# 청산 파라미터
RSI_TARGET = 60
TAKE_PROFIT_PCT = 0.05
STOP_LOSS_PCT = -0.03
TIME_STOP_DAYS = 7

# 재진입 쿨다운
REENTRY_COOLDOWN_DAYS = 3


@dataclass
class Position:
    entry_date: pd.Timestamp
    entry_price: float
    qty: int


@dataclass
class EntrySignal:
    valid: bool
    reasons: list[str]


@dataclass
class ExitSignal:
    should_exit: bool
    reason: str


def check_entry(row: pd.Series) -> EntrySignal:
    reasons: list[str] = []

    close = row.get("close")
    if pd.isna(close):
        return EntrySignal(False, ["close 없음"])

    # R1. RSI < 30
    rsi14 = row.get("rsi14")
    if pd.isna(rsi14) or rsi14 >= RSI_OVERSOLD:
        return EntrySignal(False, [f"R1 RSI {rsi14:.1f} 과매도 아님"])
    reasons.append(f"R1 RSI {rsi14:.1f} 과매도")

    # R2. 종가 > 200MA (강세장에서만 진입 — 떨어지는 칼날 회피)
    ma200 = row.get("ma200")
    if pd.isna(ma200) or close <= ma200:
        return EntrySignal(False, ["R2 200MA 아래 (떨어지는 칼날 회피)"])
    reasons.append("R2 200MA 위")

    # R3. 거래량 정상 이상
    vol = row.get("volume")
    vol_ma = row.get("vol_ma20")
    if pd.isna(vol) or pd.isna(vol_ma) or vol <= vol_ma * VOLUME_MIN_MULT:
        return EntrySignal(False, ["R3 거래량 부족"])
    reasons.append("R3 거래량 OK")

    # 보강 정보 (참고용)
    bb_pct = row.get("bb_pct")
    if pd.notna(bb_pct) and bb_pct < 0.2:
        reasons.append(f"BB 하단 (pct {bb_pct:.2f})")

    return EntrySignal(True, reasons)


def check_exit(
    row: pd.Series,
    position: Position,
    current_date: pd.Timestamp,
) -> ExitSignal:
    close = row.get("close")
    if pd.isna(close):
        return ExitSignal(False, "")

    pnl_pct = (close - position.entry_price) / position.entry_price

    # X3. 손절
    if pnl_pct <= STOP_LOSS_PCT:
        return ExitSignal(True, f"손절 {pnl_pct*100:+.2f}%")

    # X2. 익절 (목표가)
    if pnl_pct >= TAKE_PROFIT_PCT:
        return ExitSignal(True, f"익절 {pnl_pct*100:+.2f}%")

    # X1. RSI 반등 완료
    rsi14 = row.get("rsi14")
    if pd.notna(rsi14) and rsi14 > RSI_TARGET:
        return ExitSignal(True, f"RSI {rsi14:.1f} 반등 완료")

    # X4. 시간 손절
    days_held = (current_date - position.entry_date).days
    if days_held >= TIME_STOP_DAYS:
        return ExitSignal(True, f"시간만료 {days_held}일 ({pnl_pct*100:+.2f}%)")

    return ExitSignal(False, "")
