"""src/realized_pnl.py 단위 테스트.

이전 +35% inflated 같은 회귀 방지.
"""
from __future__ import annotations

import sqlite3

from src import db, realized_pnl


def _insert_trade(symbol, side, qty, price, strategy="vaa", when="2026-05-01 10:00:00"):
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO trades
                (symbol, side, quantity, price, executed_at, env, strategy)
            VALUES (?, ?, ?, ?, ?, 'paper', ?)
            """,
            (symbol, side, qty, price, when, strategy),
        )


def test_no_trades_returns_zero(temp_db):
    realized, currency = realized_pnl.realized_for_strategy("vaa")
    assert realized == 0.0
    # No KR trade → currency defaults to USD
    assert currency == "$"


def test_single_buy_no_realized(temp_db):
    """매수만 있고 매도 없으면 실현 0 (미실현은 별도 계산)."""
    _insert_trade("069500", "buy", 100, 50000)
    realized, _ = realized_pnl.realized_for_strategy("vaa")
    assert realized == 0.0


def test_full_cycle_profit(temp_db):
    """100 @ 50000 → 100 @ 55000 = +500,000 실현."""
    _insert_trade("069500", "buy", 100, 50000, when="2026-05-01 10:00:00")
    _insert_trade("069500", "sell", 100, 55000, when="2026-05-02 10:00:00")
    realized, currency = realized_pnl.realized_for_strategy("vaa")
    assert realized == 500_000.0
    assert currency == "₩"


def test_full_cycle_loss(temp_db):
    """100 @ 50000 → 100 @ 45000 = -500,000."""
    _insert_trade("069500", "buy", 100, 50000)
    _insert_trade("069500", "sell", 100, 45000, when="2026-05-02 10:00:00")
    realized, _ = realized_pnl.realized_for_strategy("vaa")
    assert realized == -500_000.0


def test_partial_sell(temp_db):
    """100 @ 100 → sell 50 @ 110 → 실현 = 50*(110-100) = 500."""
    _insert_trade("AAPL", "buy", 100, 100.0, strategy="catalyst")
    _insert_trade("AAPL", "sell", 50, 110.0, strategy="catalyst", when="2026-05-02 10:00:00")
    realized, currency = realized_pnl.realized_for_strategy("catalyst")
    assert realized == 500.0
    assert currency == "$"


def test_multiple_buys_avg(temp_db):
    """평균 매수가 기반 실현.
    100 @ 100 + 100 @ 200 → avg = 150
    sell 100 @ 180 → 100 * (180 - 150) = 3000.
    """
    _insert_trade("AAPL", "buy", 100, 100.0, strategy="catalyst")
    _insert_trade("AAPL", "buy", 100, 200.0, strategy="catalyst", when="2026-05-02")
    _insert_trade("AAPL", "sell", 100, 180.0, strategy="catalyst", when="2026-05-03")
    realized, _ = realized_pnl.realized_for_strategy("catalyst")
    assert realized == 3000.0


def test_kr_classification(temp_db):
    """6자리 숫자 → KR (₩)."""
    _insert_trade("069500", "buy", 10, 1000)
    _insert_trade("069500", "sell", 10, 1100, when="2026-05-02")
    _, currency = realized_pnl.realized_for_strategy("vaa")
    assert currency == "₩"


def test_us_classification(temp_db):
    """알파벳 ticker → US ($)."""
    _insert_trade("NVDA", "buy", 10, 100, strategy="swing_v3")
    _insert_trade("NVDA", "sell", 10, 110, strategy="swing_v3", when="2026-05-02")
    _, currency = realized_pnl.realized_for_strategy("swing_v3")
    assert currency == "$"


def test_realized_for_strategies_aggregates(temp_db):
    """KR + US 자동 분리."""
    _insert_trade("069500", "buy", 10, 1000, strategy="vaa")
    _insert_trade("069500", "sell", 10, 1100, strategy="vaa", when="2026-05-02")
    _insert_trade("NVDA", "buy", 10, 100, strategy="swing_v3")
    _insert_trade("NVDA", "sell", 10, 110, strategy="swing_v3", when="2026-05-02")

    result = realized_pnl.realized_for_strategies(["vaa", "swing_v3"])
    assert result["krw"] == 1000.0  # 10 * (1100-1000)
    assert result["usd"] == 100.0   # 10 * (110-100)


def test_pct_basic():
    assert realized_pnl.pct(500_000, 50_000_000) == 1.0
    assert realized_pnl.pct(-1_000_000, 50_000_000) == -2.0


def test_pct_zero_denominator():
    """분모 0 이면 0 (ZeroDivisionError 방지)."""
    assert realized_pnl.pct(100, 0) == 0.0
    assert realized_pnl.pct(100, -10) == 0.0
