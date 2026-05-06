"""실현 P&L 헬퍼.

DB `trades` 테이블에서 strategy 별 실현 손익 누적 합산.
weekly_report.compute_realized_pnl_per_strategy 와 동일 로직 (재사용용).

매수-매도 cycle:
  실현 = sell_value - (sell_qty * buy_avg)
  buy_avg = sum(buy_qty * buy_price) / sum(buy_qty)
"""
from __future__ import annotations

from . import config
from . import db


def realized_for_strategy(strategy: str) -> tuple[float, str]:
    """단일 strategy 의 실현 P&L 누적 + 통화 자동 판별.

    반환: (realized_pnl, currency)
      currency: '₩' (KR 6자리 숫자 종목) 또는 '$' (US).
    """
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT symbol, side, quantity, price
            FROM trades
            WHERE strategy = ? AND env = ?
            ORDER BY id ASC
            """,
            (strategy, config.KIS_ENV),
        ).fetchall()

    by_sym: dict[str, dict] = {}
    has_kr = False
    for r in rows:
        sym = r["symbol"]
        if sym.isdigit() and len(sym) == 6:
            has_kr = True
        if sym not in by_sym:
            by_sym[sym] = {
                "buy_qty": 0, "buy_value": 0.0,
                "sell_qty": 0, "sell_value": 0.0,
            }
        qty = int(r["quantity"])
        price = float(r["price"])
        if r["side"] == "buy":
            by_sym[sym]["buy_qty"] += qty
            by_sym[sym]["buy_value"] += qty * price
        else:
            by_sym[sym]["sell_qty"] += qty
            by_sym[sym]["sell_value"] += qty * price

    realized = 0.0
    for p in by_sym.values():
        if p["sell_qty"] > 0 and p["buy_qty"] > 0:
            avg_buy = p["buy_value"] / p["buy_qty"]
            realized += p["sell_value"] - (p["sell_qty"] * avg_buy)

    return realized, ("₩" if has_kr else "$")


def realized_for_strategies(strategies: list[str]) -> dict[str, float]:
    """여러 strategy 의 실현 P&L 합계.

    반환: {"krw": ..., "usd": ...} (자동 통화 분리)
    """
    krw = 0.0
    usd = 0.0
    for s in strategies:
        amt, cur = realized_for_strategy(s)
        if cur == "₩":
            krw += amt
        else:
            usd += amt
    return {"krw": krw, "usd": usd}


def pct(realized: float, denominator: float) -> float:
    """실현 / 분모 * 100. 분모 0 이하면 0.0 반환."""
    if denominator <= 0:
        return 0.0
    return realized / denominator * 100
