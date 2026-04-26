"""
Enhanced Swing v3 — 체제별 어댑티브 전략.

체제별 룰 (사용자님 결정):

🟢 BULL (상승장):
  - 초기 진입 70% (확신 큰 사이즈)
  - 손절 -5% (변동성 견딤)
  - +3% 부분익절 비활성, +20% 부터 1차 부분 매도
  - 20MA 하향 이탈 시 즉시 청산 (추세 끊김)
  - 트레일링 -5% (더 길게)
  - 시간손절 30일

🟡 RANGE (횡보장):
  - 현재 v2 룰 유지 (잘 작동)
  - DCA 트리거 -1.5% (더 자주 추가매수)
  - 부분익절 +3%, +7%, +12%
  - 재진입 쿨다운 1일
  - 볼린저 하단부 진입 보강 표시

🔴 BEAR (하락장):
  - 신규 진입 차단 (가장 안전)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import market_regime as mr

# 진입 시그널 공통 파라미터
RSI_ENTRY_MIN = 30
RSI_ENTRY_MAX = 75
VOLUME_SPIKE_MULT = 1.2

# 체제별 파라미터
PARAMS_BULL = {
    "initial_buy_ratio": 0.70,
    "stop_loss_pct": -0.05,
    "profit_tier_1": None,            # +3% 비활성
    "profit_tier_2": 0.20,            # +20% 1차 익절
    "profit_tier_2_ratio": 0.30,
    "profit_tier_3": 0.30,            # +30% 트레일링 진입
    "trail_drawdown": 0.05,
    "use_ma20_breakdown_exit": True,
    "time_stop_days": 30,
    "dca_enabled": True,
    "dca_trigger": -0.02,
    "reentry_cooldown_days": 3,
    "block_entry": False,
}

PARAMS_RANGE = {
    "initial_buy_ratio": 0.50,
    "stop_loss_pct": -0.03,
    "profit_tier_1": 0.03,            # +3% 1차
    "profit_tier_1_ratio": 0.30,
    "profit_tier_2": 0.07,            # +7% 2차
    "profit_tier_2_ratio": 0.30,
    "profit_tier_3": 0.12,            # +12% 트레일링
    "trail_drawdown": 0.03,
    "use_ma20_breakdown_exit": False,
    "time_stop_days": 15,
    "dca_enabled": True,
    "dca_trigger": -0.015,            # -1.5%
    "reentry_cooldown_days": 1,
    "block_entry": False,
    "use_bb_lower_bonus": True,
}

PARAMS_BEAR = {
    "initial_buy_ratio": 0.0,
    "block_entry": True,
    # 나머지는 무시됨
    "stop_loss_pct": -0.03,
    "trail_drawdown": 0.03,
    "time_stop_days": 5,
    "dca_enabled": False,
    "reentry_cooldown_days": 7,
}


def get_params(regime: str) -> dict:
    if regime == mr.REGIME_BULL:
        return PARAMS_BULL
    if regime == mr.REGIME_BEAR:
        return PARAMS_BEAR
    return PARAMS_RANGE


@dataclass
class PositionV3:
    qty: int
    avg_price: float
    entry_date: pd.Timestamp
    initial_qty: int
    peak_price: float
    entry_regime: str                # 진입 시점 체제 (룰 잠금)
    pf_t1_done: bool = False
    pf_t2_done: bool = False
    trailing_active: bool = False
    dca_done: bool = False


@dataclass
class Action:
    type: str
    qty: int
    reason: str


@dataclass
class EntrySignal:
    valid: bool
    reasons: list[str] = field(default_factory=list)


def check_entry(row: pd.Series, regime: str) -> EntrySignal:
    """체제별 진입 시그널.

    BEAR: 무조건 차단.
    BULL/RANGE: 4 AND + (RANGE에선 BB 하단 보조 정보).
    """
    params = get_params(regime)
    if params.get("block_entry"):
        return EntrySignal(False, [f"{regime} 진입 차단"])

    reasons: list[str] = [f"체제: {regime}"]

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

    # RANGE 보강: BB 하단부면 평균회귀 진입 메리트 ↑
    if regime == mr.REGIME_RANGE and params.get("use_bb_lower_bonus"):
        bb_pct = row.get("bb_pct")
        if pd.notna(bb_pct) and bb_pct < 0.4:
            reasons.append(f"BB 하단부 (pct {bb_pct:.2f})")

    return EntrySignal(True, reasons)


def check_dca(row: pd.Series, position: PositionV3) -> Action | None:
    params = get_params(position.entry_regime)
    if position.dca_done or not params.get("dca_enabled"):
        return None
    close = row.get("close")
    if pd.isna(close):
        return None
    pnl = (close - position.avg_price) / position.avg_price
    if pnl <= params["dca_trigger"]:
        return Action(
            "BUY_DCA",
            position.initial_qty,
            f"DCA {params['dca_trigger']*100:+.1f}% ({position.entry_regime})",
        )
    return None


def check_exit_v3(
    row: pd.Series,
    position: PositionV3,
    current_date: pd.Timestamp,
) -> list[Action]:
    actions: list[Action] = []
    params = get_params(position.entry_regime)

    close = row.get("close")
    if pd.isna(close):
        return actions

    pnl = (close - position.avg_price) / position.avg_price

    if close > position.peak_price:
        position.peak_price = close

    # 1) 손절 (체제별)
    if pnl <= params["stop_loss_pct"]:
        actions.append(Action(
            "SELL_ALL", position.qty,
            f"손절 {pnl*100:+.2f}% ({position.entry_regime} 룰 {params['stop_loss_pct']*100:.0f}%)",
        ))
        return actions

    # 2) 20MA 이탈 (BULL 만 적용)
    if params.get("use_ma20_breakdown_exit"):
        ma20 = row.get("ma20")
        if pd.notna(ma20) and close < ma20:
            actions.append(Action(
                "SELL_ALL", position.qty,
                f"20MA 이탈 익절 {pnl*100:+.2f}% (BULL)",
            ))
            return actions

    # 3) 시간 손절
    days_held = (current_date - position.entry_date).days
    if days_held >= params["time_stop_days"] and pnl < 0.01:
        actions.append(Action(
            "SELL_ALL", position.qty,
            f"시간손절 {days_held}일 ({pnl*100:+.2f}%)",
        ))
        return actions

    # 4) +Tier1 부분익절 (RANGE 만; BULL은 None)
    pt1 = params.get("profit_tier_1")
    if pt1 is not None and not position.pf_t1_done and pnl >= pt1:
        ratio = params.get("profit_tier_1_ratio", 0.30)
        partial = int(position.initial_qty * ratio)
        partial = min(partial, position.qty)
        if partial > 0:
            actions.append(Action(
                "SELL_PARTIAL", partial,
                f"+{pt1*100:.0f}% 1차 익절 ({position.entry_regime})",
            ))
        position.pf_t1_done = True

    # 5) +Tier2 부분익절 (BULL: +20%, RANGE: +7%)
    pt2 = params.get("profit_tier_2")
    if pt2 is not None and not position.pf_t2_done and pnl >= pt2:
        ratio = params.get("profit_tier_2_ratio", 0.30)
        partial = int(position.initial_qty * ratio)
        partial = min(partial, position.qty - sum(a.qty for a in actions))
        if partial > 0:
            actions.append(Action(
                "SELL_PARTIAL", partial,
                f"+{pt2*100:.0f}% 부분익절 ({position.entry_regime})",
            ))
        position.pf_t2_done = True

    # 6) +Tier3 트레일링 활성화
    pt3 = params.get("profit_tier_3")
    if pt3 is not None and not position.trailing_active and pnl >= pt3:
        position.trailing_active = True

    # 7) 트레일링 발동
    if position.trailing_active and position.peak_price > 0:
        trail_threshold = position.peak_price * (1 - params["trail_drawdown"])
        if close <= trail_threshold:
            remaining = position.qty - sum(a.qty for a in actions)
            if remaining > 0:
                actions.append(Action(
                    "SELL_ALL", remaining,
                    f"트레일링 청산 (peak ${position.peak_price:.2f}, "
                    f"-{params['trail_drawdown']*100:.0f}%, {position.entry_regime})",
                ))

    return actions
