"""
운영 안전망 (Risk Guard).

[Codex 권장 — 수정형 kill switch]:
  "임의 -8% 대신: 노출 캡 + 슬리브별 쿨다운 + 사람 확인 단계"

이 모듈의 책임은 **자동 차단 X, 알람 / 가시성 O**:
  1. drawdown alarm — 30일 rolling 손실 임계 넘으면 telegram 알람
  2. no-trade cooldown — 60일 무거래 → "전략 비활성 의심" 알람
  3. position concentration — 단일 종목 비중 초과 알람

자동 차단을 안 하는 이유 (Codex 지적):
  -8% 같은 임계는 임의적. 임계 직전 -7.9% 에서 멈추는 건 노이즈.
  진짜 가치 = "사람이 알게 하는 것" — 운영자가 직접 의사결정.

운영 흐름:
  daily_swing_v3_kr 실행 시작
    → check_strategy_health() 호출
    → 알람 있으면 telegram 으로 보고
    → 매매는 그대로 진행 (사람이 멈출지 결정)
    → 5번 연속 같은 알람 시 자동 일시 정지 (--execute 비활성화 권장)

매번 실행 시 가시성 ↑ 가 본 모듈의 목적.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from . import config
from . import db


@dataclass
class StrategyHealth:
    strategy_tag: str
    env: str
    last_trade_date: str | None = None
    days_since_last_trade: int | None = None
    recent_trades_30d: int = 0
    recent_realized_pnl_30d: float = 0.0
    recent_realized_pnl_pct_30d: float = 0.0
    alarms: list[str] = field(default_factory=list)

    @property
    def has_alarms(self) -> bool:
        return len(self.alarms) > 0

    def format_for_telegram(self) -> str:
        """Telegram 알림용 한 줄 요약."""
        if not self.has_alarms:
            return f"✅ {self.strategy_tag}: 정상"
        return (
            f"⚠️ {self.strategy_tag} 알람 ({len(self.alarms)}건):\n"
            + "\n".join(f"  • {a}" for a in self.alarms)
        )


def _fetch_recent_trades(
    strategy_tag: str, env: str, days: int = 30
) -> list[dict]:
    """trades 테이블에서 N일치 strategy 별 거래 조회."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT executed_at, symbol, side, quantity, price, notes
            FROM trades
            WHERE strategy = ? AND env = ?
            AND DATE(executed_at) >= ?
            ORDER BY executed_at ASC
            """,
            (strategy_tag, env, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


def _fetch_last_trade_date(strategy_tag: str, env: str) -> str | None:
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT MAX(executed_at) AS last
            FROM trades
            WHERE strategy = ? AND env = ?
            """,
            (strategy_tag, env),
        ).fetchone()
    return row["last"] if row and row["last"] else None


def _estimate_realized_pnl(trades: list[dict]) -> tuple[float, float]:
    """
    근사 실현 P&L: sell trades 의 (price × qty) 합 - buy trades 의 (price × qty) 합.
    부분익절 / DCA 등 모두 단순 합산.

    반환: (절대 P&L, 비율 P&L wrt 매수 cost)
    """
    buy_cost = 0.0
    sell_proceeds = 0.0
    for t in trades:
        try:
            qty = int(t.get("quantity") or 0)
            price = float(t.get("price") or 0)
        except (ValueError, TypeError):
            continue
        amount = qty * price
        if t.get("side") == "buy":
            buy_cost += amount
        elif t.get("side") == "sell":
            sell_proceeds += amount
    pnl = sell_proceeds - buy_cost
    pnl_pct = pnl / buy_cost if buy_cost > 0 else 0.0
    return pnl, pnl_pct


def check_strategy_health(
    strategy_tag: str,
    env: str | None = None,
    lookback_days: int = 30,
    drawdown_alarm_pct: float = -0.08,
    cooldown_alarm_days: int = 60,
) -> StrategyHealth:
    """
    Strategy 의 운영 건강 체크. 알람 발생 시 alarms 리스트에 명시.

    체크 항목:
      1. drawdown alarm: 최근 N일 실현 P&L 비율 < drawdown_alarm_pct
      2. cooldown alarm: 마지막 거래로부터 cooldown_alarm_days 일 초과
      3. (확장 가능) position concentration, etc.
    """
    env = env or config.KIS_ENV
    health = StrategyHealth(strategy_tag=strategy_tag, env=env)

    # 최근 N일 거래
    recent = _fetch_recent_trades(strategy_tag, env, days=lookback_days)
    health.recent_trades_30d = len(recent)

    if recent:
        pnl, pnl_pct = _estimate_realized_pnl(recent)
        health.recent_realized_pnl_30d = pnl
        health.recent_realized_pnl_pct_30d = pnl_pct
        if pnl_pct < drawdown_alarm_pct:
            health.alarms.append(
                f"DD alarm: {lookback_days}일 실현 P&L {pnl_pct*100:+.2f}% "
                f"< 임계 {drawdown_alarm_pct*100:+.0f}%"
            )

    # 마지막 거래 날짜
    last_date_str = _fetch_last_trade_date(strategy_tag, env)
    health.last_trade_date = last_date_str
    if last_date_str:
        try:
            last_dt = datetime.fromisoformat(last_date_str.replace("Z", "+00:00").split(".")[0])
            days_idle = (datetime.now() - last_dt).days
            health.days_since_last_trade = days_idle
            if days_idle > cooldown_alarm_days:
                health.alarms.append(
                    f"Cooldown alarm: 마지막 거래 {days_idle}일 전 "
                    f"(임계 {cooldown_alarm_days}일) — 전략 비활성 의심"
                )
        except (ValueError, TypeError):
            pass
    else:
        # 거래 기록 없음 — 신규 운영이면 정상, 오래 운영 중이면 이상
        health.days_since_last_trade = None
        health.alarms.append("거래 기록 없음 (신규 또는 운영 중단 의심)")

    return health


def check_position_concentration(
    positions: dict[str, int],
    prices: dict[str, float],
    total_equity: float,
    max_per_position_pct: float = 0.10,
) -> list[str]:
    """
    단일 종목이 전체 자산의 max_per_position_pct 초과 시 알람.

    기본 10% — KIS 모의 5천만원 기준 종목당 500만원 한계.
    """
    alarms: list[str] = []
    if total_equity <= 0:
        return alarms
    for sym, qty in positions.items():
        price = prices.get(sym, 0)
        if price <= 0 or qty <= 0:
            continue
        position_value = qty * price
        weight = position_value / total_equity
        if weight > max_per_position_pct:
            alarms.append(
                f"Concentration: {sym} 비중 {weight*100:.1f}% > "
                f"한계 {max_per_position_pct*100:.0f}%"
            )
    return alarms


def format_health_report(healths: list[StrategyHealth]) -> str:
    """여러 strategy 의 health 를 합쳐 telegram 한 메시지로."""
    lines = ["🛡 Risk Guard Report"]
    any_alarm = False
    for h in healths:
        if h.has_alarms:
            any_alarm = True
        lines.append("")
        lines.append(h.format_for_telegram())
        if h.recent_trades_30d > 0:
            lines.append(
                f"  최근 30일: {h.recent_trades_30d}거래 | "
                f"P&L {h.recent_realized_pnl_pct_30d*100:+.2f}%"
            )
        if h.days_since_last_trade is not None:
            lines.append(f"  마지막 거래: {h.days_since_last_trade}일 전")

    if not any_alarm:
        return ""  # 알람 없음 — 보고 X (조용한 정상)
    return "\n".join(lines)


def main() -> int:
    """단독 실행 — 모든 strategy 의 건강 체크."""
    import sys
    strategies = ["dual_momentum", "swing_v3"]
    print(f"[Risk Guard] env={config.KIS_ENV}")

    healths = []
    for tag in strategies:
        h = check_strategy_health(tag)
        healths.append(h)
        print(f"\n=== {tag} ===")
        print(f"  최근 30일 거래: {h.recent_trades_30d}건")
        if h.recent_trades_30d > 0:
            print(f"  실현 P&L: {h.recent_realized_pnl_30d:+,.0f}원 "
                  f"({h.recent_realized_pnl_pct_30d*100:+.2f}%)")
        print(f"  마지막 거래: {h.last_trade_date or '없음'}")
        if h.days_since_last_trade is not None:
            print(f"  ({h.days_since_last_trade}일 전)")
        if h.alarms:
            print("  ⚠️  알람:")
            for a in h.alarms:
                print(f"    - {a}")
        else:
            print("  ✅ 정상")

    report = format_health_report(healths)
    if report:
        print(f"\n--- Telegram 보고 ---\n{report}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
