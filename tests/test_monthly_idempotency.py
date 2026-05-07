"""monthly_rebalance.py 의 멱등 가드 (_already_done_this_month) 테스트."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest

from src import config, monthly_rebalance


@pytest.fixture
def memory_db(monkeypatch):
    """db.connection() 을 in-memory SQLite 로 교체."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            side TEXT,
            quantity INTEGER,
            price REAL,
            executed_at TEXT,
            env TEXT,
            strategy TEXT,
            order_id TEXT,
            notes TEXT
        )
        """
    )
    conn.commit()

    @contextmanager
    def fake_connection():
        try:
            yield conn
        finally:
            pass

    from src import db as db_module
    monkeypatch.setattr(db_module, "connection", fake_connection)
    monkeypatch.setattr(monthly_rebalance.db, "connection", fake_connection)
    monkeypatch.setattr(config, "KIS_ENV", "paper")
    yield conn
    conn.close()


def _insert_trade(conn, **kwargs):
    cols = list(kwargs.keys())
    placeholders = ", ".join("?" for _ in cols)
    conn.execute(
        f"INSERT INTO trades ({', '.join(cols)}) VALUES ({placeholders})",
        tuple(kwargs.values()),
    )
    conn.commit()


def test_empty_db_returns_false(memory_db):
    assert monthly_rebalance._already_done_this_month() is False


def test_dm_success_this_month_returns_true(memory_db):
    _insert_trade(
        memory_db,
        strategy="dual_momentum",
        env="paper",
        order_id="K12345",
        executed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        symbol="069500",
        side="BUY",
        quantity=85,
        price=115500,
        notes="test",
    )
    assert monthly_rebalance._already_done_this_month() is True


def test_dm_failed_trade_returns_false(memory_db):
    """order_id NULL 은 실패 — 멱등 가드 통과 안 시킴."""
    _insert_trade(
        memory_db,
        strategy="dual_momentum",
        env="paper",
        order_id=None,
        executed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        symbol="069500",
        side="BUY",
        quantity=85,
        price=115500,
        notes="test",
    )
    assert monthly_rebalance._already_done_this_month() is False


def test_other_strategy_returns_false(memory_db):
    _insert_trade(
        memory_db,
        strategy="v3_kr",
        env="paper",
        order_id="K12345",
        executed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        symbol="069500",
        side="BUY",
        quantity=10,
        price=115500,
        notes="test",
    )
    assert monthly_rebalance._already_done_this_month() is False


def test_other_env_returns_false(memory_db, monkeypatch):
    """KIS_ENV=paper 인데 trade 가 real 이면 무시."""
    _insert_trade(
        memory_db,
        strategy="dual_momentum",
        env="real",
        order_id="K12345",
        executed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        symbol="069500",
        side="BUY",
        quantity=85,
        price=115500,
        notes="test",
    )
    assert monthly_rebalance._already_done_this_month() is False


def test_last_month_trade_returns_false(memory_db):
    """지난 달 trade 는 이번 달 멱등에 영향 X."""
    last_month = (datetime.now().replace(day=1) - timedelta(days=1)).replace(day=15)
    _insert_trade(
        memory_db,
        strategy="dual_momentum",
        env="paper",
        order_id="K12345",
        executed_at=last_month.strftime("%Y-%m-%d %H:%M:%S"),
        symbol="069500",
        side="BUY",
        quantity=85,
        price=115500,
        notes="test",
    )
    assert monthly_rebalance._already_done_this_month() is False
