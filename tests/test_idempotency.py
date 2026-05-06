"""src/idempotency.py 단위 테스트.

중복 주문 방지 키 검증.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from src import db, idempotency


def _insert_trade_with_key(symbol, side, qty, key, executed_at=None):
    if executed_at is None:
        executed_at = datetime.now().isoformat()
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO trades
                (symbol, side, quantity, price, executed_at, env, strategy,
                 idempotency_key)
            VALUES (?, ?, ?, ?, ?, 'paper', 'vaa', ?)
            """,
            (symbol, side.lower(), qty, 0.0, executed_at, key),
        )


def test_make_key_format():
    key = idempotency.make_key("vaa", "069500", "BUY", 100, date(2026, 5, 1))
    assert key == "vaa:069500:BUY:100:2026-05-01"


def test_make_key_normalizes_side_uppercase():
    """side='buy' / 'BUY' / 'Buy' 모두 같은 키."""
    k1 = idempotency.make_key("vaa", "069500", "BUY", 100, date(2026, 5, 1))
    k2 = idempotency.make_key("vaa", "069500", "buy", 100, date(2026, 5, 1))
    k3 = idempotency.make_key("vaa", "069500", "Buy", 100, date(2026, 5, 1))
    assert k1 == k2 == k3


def test_already_executed_false_when_no_match(temp_db):
    idempotency._ensure_idempotency_schema()
    assert not idempotency.already_executed("nonexistent:key")


def test_already_executed_detects_duplicate(temp_db):
    idempotency._ensure_idempotency_schema()
    key = idempotency.make_key("vaa", "069500", "BUY", 100)
    _insert_trade_with_key("069500", "BUY", 100, key)
    assert idempotency.already_executed(key)


def test_old_record_outside_window(temp_db):
    """24h 보다 오래된 기록은 SKIP 안 함."""
    idempotency._ensure_idempotency_schema()
    key = idempotency.make_key("vaa", "069500", "BUY", 100)
    old_time = (datetime.now() - timedelta(days=2)).isoformat()
    _insert_trade_with_key("069500", "BUY", 100, key, executed_at=old_time)
    assert not idempotency.already_executed(key, within_hours=24)


def test_different_qty_different_key(temp_db):
    """수량 다르면 다른 키 → 분할 매매 차단 X."""
    idempotency._ensure_idempotency_schema()
    k1 = idempotency.make_key("vaa", "069500", "BUY", 100)
    k2 = idempotency.make_key("vaa", "069500", "BUY", 50)
    _insert_trade_with_key("069500", "BUY", 100, k1)
    assert idempotency.already_executed(k1)
    assert not idempotency.already_executed(k2)


def test_different_side_different_key():
    k_buy = idempotency.make_key("vaa", "069500", "BUY", 100)
    k_sell = idempotency.make_key("vaa", "069500", "SELL", 100)
    assert k_buy != k_sell


def test_schema_migration_idempotent(temp_db):
    """_ensure_idempotency_schema 두 번 호출해도 안전."""
    idempotency._ensure_idempotency_schema()
    idempotency._ensure_idempotency_schema()  # 두 번째도 OK
    with db.connection() as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
        assert "idempotency_key" in cols
