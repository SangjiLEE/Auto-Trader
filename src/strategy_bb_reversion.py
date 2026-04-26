"""
볼린저 밴드 하단 터치 평균회귀 전략.

추세 필터 없는 순수 변동성 기반 평균회귀.

진입 (2 AND):
  B1. 종가 < BB 하단 (2σ 이탈, 통계적 과매도)
  B2. 거래량 > 20일 평균 × 1.0 (반등 동력)

청산 (4 OR):
  X1. 종가 > BB 중앙선 (평균 회귀 완료, 익절)
  X2. 진입가 +5% (목표 도달)
  X3. 진입가 -3% (손절)
  X4. 5일 경과 (회귀 안 일어남)

특징:
  - 추세 무관 → 강추세 종목엔 손실 (떨어지는 칼날)
  - 횡보·박스권 종목엔 강함
  - RSI 평균회귀보다 진입 빈도 높을 가능성
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

VOLUME_MIN_MULT = 1.0
TAKE_PROFIT_PCT = 0.05
STOP_LOSS_PCT = -0.03
TIME_STOP_DAYS = 5
REENTRY_COOLDOWN_DAYS = 2


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

    # B1. 종가 < BB 하단
    bb_lower = row.get("bb_lower")
    if pd.isna(bb_lower) or close >= bb_lower:
        bb_pct = row.get("bb_pct")
        return EntrySignal(False, [
            f"B1 BB 하단 위 (pct {bb_pct:.2f})" if pd.notna(bb_pct) else "B1 BB 하단 위"
        ])
    reasons.append(f"B1 BB 하단 이탈 (close ${close:.2f} < lower ${bb_lower:.2f})")

    # B2. 거래량
    vol = row.get("volume")
    vol_ma = row.get("vol_ma20")
    if pd.isna(vol) or pd.isna(vol_ma) or vol <= vol_ma * VOLUME_MIN_MULT:
        return EntrySignal(False, ["B2 거래량 부족"])
    reasons.append("B2 거래량 OK")

    # 보강 정보
    rsi14 = row.get("rsi14")
    if pd.notna(rsi14):
        reasons.append(f"RSI {rsi14:.1f}")

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

    # X2. 익절
    if pnl_pct >= TAKE_PROFIT_PCT:
        return ExitSignal(True, f"익절 {pnl_pct*100:+.2f}%")

    # X1. BB 중앙 도달
    bb_middle = row.get("bb_middle")
    if pd.notna(bb_middle) and close >= bb_middle:
        return ExitSignal(True, f"BB 중앙 도달 ({pnl_pct*100:+.2f}%)")

    # X4. 시간 손절
    days_held = (current_date - position.entry_date).days
    if days_held >= TIME_STOP_DAYS:
        return ExitSignal(True, f"시간만료 {days_held}일 ({pnl_pct*100:+.2f}%)")

    return ExitSignal(False, "")
