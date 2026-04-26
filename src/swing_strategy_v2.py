"""
Enhanced Swing v2 — 부분 진입 + 계단식 익절 + 재진입.

진입:
  E1. 종가 > 20MA
  E2. 전환선 > 기준선
  E3. RSI(14) ∈ [30, 75]
  E4. 거래량 > 20MA × 1.2
  → 신호 성립 시 슬롯의 50% 매수 (1차)

DCA (추가 매수):
  - 진입가 대비 -2% 하락 + 아직 추가매수 안 함
  → 추가 50% 매수 (평단 낮춤)

청산 (계단식):
  - +3% 도달: 초기 수량의 30% 매도 (수익 확정)
  - +7% 도달: 추가 30% 매도
  - +12% 도달: 트레일링 모드 진입
    - peak 대비 -3% 하회 시 나머지 매도

손절 / 시간손절:
  - 평단 -3% 하회: 전량 청산
  - 진입 후 15일 + 수익 < +1%: 시간 청산

재진입:
  - 직전 청산 후 3일 이상 경과
  - 직전 청산가 대비 -2% 이상 하락
  - 진입 시그널 재유효
  → 신규 진입
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

# 진입 조건 파라미터
RSI_ENTRY_MIN = 30
RSI_ENTRY_MAX = 75
RSI_OVERBOUGHT = 80
VOLUME_SPIKE_MULT = 1.2

# 청산 임계값
PROFIT_TIER_1 = 0.03
PROFIT_TIER_2 = 0.07
PROFIT_TIER_3 = 0.12
TRAIL_DRAWDOWN = 0.03    # +12% 후 peak 대비 -3% 트레일링
STOP_LOSS_PCT = -0.03    # 평단 -3%
TIME_STOP_DAYS = 15
TIME_STOP_PROFIT_THRESHOLD = 0.01

# 부분 매도 비율
PROFIT_SELL_RATIO_T1 = 0.30
PROFIT_SELL_RATIO_T2 = 0.30
# T3 는 트레일링 후 나머지 전체

# DCA
DCA_TRIGGER = -0.02
DCA_RATIO = 1.0  # 초기 수량의 100% 추가 (총 200% 까지)

# 재진입
REENTRY_COOLDOWN_DAYS = 3
REENTRY_PRICE_DROP = -0.02


@dataclass
class PositionV2:
    """다단계 포지션. 부분 매도 / DCA 추적."""
    qty: int                 # 현재 보유 수량
    avg_price: float         # 가중평균 매입가
    entry_date: pd.Timestamp # 최초 진입일
    initial_qty: int         # 1차 매수 수량 (부분 매도 사이즈 산정용)
    peak_price: float        # 최고 도달가 (트레일링용)
    pf_t1_done: bool = False # +3% 부분익절 완료
    pf_t2_done: bool = False # +7% 부분익절 완료
    trailing_active: bool = False  # +12% 도달 → 트레일링 모드
    dca_done: bool = False   # DCA 추가매수 완료


@dataclass
class Action:
    """전략이 반환하는 행동 단위."""
    type: str   # "BUY_INITIAL" | "BUY_DCA" | "SELL_PARTIAL" | "SELL_ALL"
    qty: int
    reason: str


@dataclass
class EntrySignal:
    valid: bool
    reasons: list[str] = field(default_factory=list)


def check_entry(row: pd.Series) -> EntrySignal:
    """v2 진입 시그널 (4 AND, fast 룰과 같음)."""
    reasons: list[str] = []

    close = row.get("close")
    if pd.isna(close):
        return EntrySignal(False, ["close 없음"])

    ma20 = row.get("ma20")
    if pd.isna(ma20) or close <= ma20:
        return EntrySignal(False, ["E1 20MA 아래"])
    reasons.append("E1 20MA 위")

    tenkan = row.get("tenkan")
    kijun = row.get("kijun")
    if pd.isna(tenkan) or pd.isna(kijun) or tenkan <= kijun:
        return EntrySignal(False, ["E2 전환선 ≤ 기준선"])
    reasons.append("E2 일목 강세")

    rsi14 = row.get("rsi14")
    if pd.isna(rsi14) or not (RSI_ENTRY_MIN <= rsi14 <= RSI_ENTRY_MAX):
        return EntrySignal(False, [f"E3 RSI {rsi14:.1f} 범위 밖"])
    reasons.append(f"E3 RSI {rsi14:.1f}")

    vol = row.get("volume")
    vol_ma = row.get("vol_ma20")
    if pd.isna(vol) or pd.isna(vol_ma) or vol <= vol_ma * VOLUME_SPIKE_MULT:
        return EntrySignal(False, ["E4 거래량 부족"])
    reasons.append("E4 거래량 OK")

    return EntrySignal(True, reasons)


def check_dca(row: pd.Series, position: PositionV2) -> Action | None:
    """DCA 추가매수 가능 여부."""
    if position.dca_done:
        return None
    close = row.get("close")
    if pd.isna(close):
        return None

    pnl = (close - position.avg_price) / position.avg_price
    if pnl <= DCA_TRIGGER:
        # 추가 매수 (initial_qty 만큼)
        return Action("BUY_DCA", position.initial_qty, f"DCA -{abs(DCA_TRIGGER)*100:.0f}% 하락")
    return None


def check_exit_v2(
    row: pd.Series,
    position: PositionV2,
    current_date: pd.Timestamp,
) -> list[Action]:
    """v2 청산 — 여러 actions 가능 (계단식)."""
    actions: list[Action] = []

    close = row.get("close")
    if pd.isna(close):
        return actions

    pnl = (close - position.avg_price) / position.avg_price

    # peak 갱신
    if close > position.peak_price:
        position.peak_price = close

    # 1) 손절 (먼저 체크 — 다른 결정보다 우선)
    if pnl <= STOP_LOSS_PCT:
        actions.append(Action("SELL_ALL", position.qty, f"손절 {pnl*100:+.2f}%"))
        return actions

    # 2) 시간 손절
    days_held = (current_date - position.entry_date).days
    if days_held >= TIME_STOP_DAYS and pnl < TIME_STOP_PROFIT_THRESHOLD:
        actions.append(Action(
            "SELL_ALL", position.qty,
            f"시간손절 {days_held}일 ({pnl*100:+.2f}%)",
        ))
        return actions

    # 3) +3% 부분익절
    if not position.pf_t1_done and pnl >= PROFIT_TIER_1:
        partial = int(position.initial_qty * PROFIT_SELL_RATIO_T1)
        partial = min(partial, position.qty)
        if partial > 0:
            actions.append(Action(
                "SELL_PARTIAL", partial,
                f"+{PROFIT_TIER_1*100:.0f}% 1차 부분익절",
            ))
        position.pf_t1_done = True

    # 4) +7% 부분익절
    if not position.pf_t2_done and pnl >= PROFIT_TIER_2:
        partial = int(position.initial_qty * PROFIT_SELL_RATIO_T2)
        partial = min(partial, position.qty - sum(a.qty for a in actions))
        if partial > 0:
            actions.append(Action(
                "SELL_PARTIAL", partial,
                f"+{PROFIT_TIER_2*100:.0f}% 2차 부분익절",
            ))
        position.pf_t2_done = True

    # 5) +12% 트레일링 모드
    if not position.trailing_active and pnl >= PROFIT_TIER_3:
        position.trailing_active = True

    # 6) 트레일링 발동 (peak 대비 -3% 하회)
    if position.trailing_active and position.peak_price > 0:
        trail_threshold = position.peak_price * (1 - TRAIL_DRAWDOWN)
        if close <= trail_threshold:
            remaining = position.qty - sum(a.qty for a in actions)
            if remaining > 0:
                actions.append(Action(
                    "SELL_ALL", remaining,
                    f"트레일링 청산 (peak ${position.peak_price:.2f}, -{TRAIL_DRAWDOWN*100:.0f}%)",
                ))

    return actions
