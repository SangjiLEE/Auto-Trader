"""
포트폴리오-레벨 Cash Ledger (read-only).

[Codex 권장 — top 3 #3]:
"70/15/15 슬리브는 개념적, 실제 강제 X. DM, KR swing, US swing, F&G overlays 가
 모두 같은 account cash 에서 작동 → cross-strategy capital drift 가능"

이 모듈은:
  - 슬리브별 자본 사용 비중 실시간 측정
  - 목표 비중 (70/15/15) 대비 drift 알람
  - 자동 차단 X (운영자 가시성 ↑)

전체 cash reservation / locking 시스템 (강제 enforcement) 은 follow-up.
지금은 read-only 가시성만으로도 cross-sleeve drift 발견 가능.

운영 흐름:
  daily_swing 실행 → check_sleeve_drift() → 알람 → telegram 보고
"""
from __future__ import annotations

from dataclasses import dataclass


# ─── 슬리브 정의 (현재 운영 시스템 기준) ───

SLEEVE_DEFINITIONS = {
    "DM": {
        "target_pct": 0.70,
        "symbols": ["069500", "133690", "360750", "148070"],
        "description": "Dual Momentum — 4 ETF universe, 월간 리밸런싱",
    },
    "V3_KR": {
        "target_pct": 0.15,
        "symbols": ["069500", "005930", "035420"],
        "description": "v3 KR swing — 일간 체제 어댑티브",
    },
    "V3_US": {
        "target_pct": 0.15,
        "symbols": ["AAPL", "NVDA", "TSLA"],
        "description": "v3 US swing — 일간 체제 어댑티브",
    },
}

# Drift 알람 임계 — 목표 비중 ± N%p 초과 시 알람
DRIFT_ALARM_THRESHOLD = 0.05  # ±5%p


def is_kr_symbol(symbol: str) -> bool:
    return symbol.isdigit() and len(symbol) == 6


def is_us_symbol(symbol: str) -> bool:
    return symbol.isalpha() and not is_kr_symbol(symbol)


@dataclass
class SleeveUsage:
    sleeve: str
    target_pct: float
    current_value: float
    current_pct: float
    drift_pct: float       # current - target (음수 = 미사용, 양수 = 초과)
    overlap_with: list[str]  # 다른 슬리브와 종목 겹침 (예: 069500)


def compute_sleeve_usage(
    positions: dict[str, int],
    prices: dict[str, float],
    total_equity: float,
) -> dict[str, SleeveUsage]:
    """
    각 슬리브의 실제 자본 사용 측정.

    [주의] DM 과 V3_KR 모두 069500 사용 → 종목 겹침. 단순 sum 시 이중 카운트.
    이 함수는 "그 슬리브에 속한 종목 평가금액" 합산. drift 해석 시 겹침 인지 필요.
    """
    if total_equity <= 0:
        return {}

    # 종목별 슬리브 멤버십 (다중 가능)
    symbol_to_sleeves: dict[str, list[str]] = {}
    for sleeve_name, info in SLEEVE_DEFINITIONS.items():
        for sym in info["symbols"]:
            symbol_to_sleeves.setdefault(sym, []).append(sleeve_name)

    usage: dict[str, SleeveUsage] = {}
    for sleeve_name, info in SLEEVE_DEFINITIONS.items():
        sleeve_symbols = info["symbols"]
        sleeve_value = 0.0
        overlapping_syms = []
        for sym in sleeve_symbols:
            qty = positions.get(sym, 0)
            price = prices.get(sym, 0.0)
            if qty > 0 and price > 0:
                sleeve_value += qty * price
                if len(symbol_to_sleeves.get(sym, [])) > 1:
                    overlapping_syms.append(sym)

        current_pct = sleeve_value / total_equity
        usage[sleeve_name] = SleeveUsage(
            sleeve=sleeve_name,
            target_pct=info["target_pct"],
            current_value=sleeve_value,
            current_pct=current_pct,
            drift_pct=current_pct - info["target_pct"],
            overlap_with=overlapping_syms,
        )
    return usage


def check_sleeve_drift(
    positions: dict[str, int],
    prices: dict[str, float],
    total_equity: float,
    drift_threshold: float = DRIFT_ALARM_THRESHOLD,
) -> list[str]:
    """
    슬리브 drift 알람.

    음수 drift (-) = 슬리브 미사용 (자본 노는 중)
    양수 drift (+) = 슬리브 초과 사용 (다른 슬리브 자본 침범 의심)
    """
    usage = compute_sleeve_usage(positions, prices, total_equity)
    alarms: list[str] = []
    for sleeve_name, u in usage.items():
        if abs(u.drift_pct) > drift_threshold:
            sign = "초과" if u.drift_pct > 0 else "미사용"
            alarms.append(
                f"Drift: {sleeve_name} 비중 {u.current_pct*100:.1f}% "
                f"(목표 {u.target_pct*100:.0f}%, {sign} {u.drift_pct*100:+.1f}%p)"
            )
    return alarms


def format_ledger_report(usage: dict[str, SleeveUsage]) -> str:
    """슬리브 사용 현황 telegram 보고 포맷."""
    if not usage:
        return ""
    lines = ["📊 Sleeve Ledger"]
    for sleeve_name, u in usage.items():
        emoji = "✅" if abs(u.drift_pct) <= DRIFT_ALARM_THRESHOLD else "⚠️"
        lines.append(
            f"  {emoji} {sleeve_name}: {u.current_pct*100:.1f}% "
            f"(목표 {u.target_pct*100:.0f}%, drift {u.drift_pct*100:+.1f}%p)"
        )
        if u.current_value > 0:
            lines.append(f"     평가 ₩{u.current_value:,.0f}")
        if u.overlap_with:
            lines.append(f"     ⚠️ 종목 겹침: {', '.join(u.overlap_with)}")
    return "\n".join(lines)


def main() -> int:
    """단독 실행 — 시뮬 데이터로 표시 (KIS API 호출 없음)."""
    import sys
    print("[Portfolio Ledger] 슬리브 정의:")
    for name, info in SLEEVE_DEFINITIONS.items():
        print(f"\n  {name}: 목표 {info['target_pct']*100:.0f}%")
        print(f"    종목: {info['symbols']}")
        print(f"    설명: {info['description']}")

    print("\n시뮬 예시 (5천만원 자본, 현재 069500 + NVDA 보유):")
    # 시뮬: 069500 200주 @ 33,000 = 660만 / NVDA 5주 @ 850 = $4250 ≈ 575만
    sim_positions = {"069500": 200, "NVDA": 5}
    sim_prices = {"069500": 33000, "NVDA": 850 * 1350}  # USD → KRW 환산
    total = 50_000_000

    usage = compute_sleeve_usage(sim_positions, sim_prices, total)
    print()
    print(format_ledger_report(usage))

    alarms = check_sleeve_drift(sim_positions, sim_prices, total)
    if alarms:
        print("\n🛡 알람:")
        for a in alarms:
            print(f"  • {a}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
