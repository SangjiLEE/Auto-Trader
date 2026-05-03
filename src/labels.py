"""
Strategy / 메트릭 한글 라벨.

DB 의 strategy 컬럼은 영어 (ASCII) 그대로 유지.
Telegram 알림 / 보고서에는 한글 표시.
"""
from __future__ import annotations

# Strategy 한글 매핑 — DB strategy 컬럼 → 사용자 표시명
STRATEGY_KR: dict[str, str] = {
    "vaa": "경계형 자산배분 (VAA)",
    "faber_us": "추세 추종 분산 (Faber)",
    "swing_v3": "체제 적응 스윙 (v3)",
    "catalyst": "호재 단타 (Catalyst)",
    "dual_momentum": "이중 모멘텀 (DM, legacy)",
}

# 시장 체제
REGIME_KR: dict[str, str] = {
    "BULL": "🟢 상승장",
    "BEAR": "🔴 하락장",
    "RANGE": "🟡 횡보장",
}

# 거래 액션
ACTION_KR: dict[str, str] = {
    "BUY": "🟢 매수",
    "SELL": "🔴 매도",
    "buy": "매수",
    "sell": "매도",
}


def strategy_kr(name: str) -> str:
    """영어 strategy 명 → 한글 표시명. 매핑 없으면 원본 반환."""
    return STRATEGY_KR.get(name, name)


def regime_kr(name: str) -> str:
    return REGIME_KR.get(name, name)


def action_kr(name: str) -> str:
    return ACTION_KR.get(name, name)
