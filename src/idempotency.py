"""주문 멱등성 보장.

launchd 가 같은 시간 두 번 트리거되거나 사용자 수동 실행 + 자동 실행이 겹쳤을 때
중복 주문 방지.

키 형식:
  {strategy}:{symbol}:{side}:{qty}:{YYYY-MM-DD}
  → 같은 strategy 가 같은 종목 같은 사이드 같은 수량을 같은 날 두 번 실행하면 차단

DB:
  trades 테이블에 idempotency_key TEXT 컬럼 + 인덱스.
  schema migration 은 _ensure_idempotency_schema() 가 자동 처리.

사용:
  key = idempotency.make_key("vaa", "069500", "BUY", 100)
  if idempotency.already_executed(key):
      print("[멱등] skip")
      continue
  # ... place_order ...
  log_trade(..., idempotency_key=key)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from . import config
from . import db


_SCHEMA_MIGRATION = """
-- idempotency_key 컬럼이 없으면 추가
-- (sqlite ALTER TABLE 은 멱등 ADD COLUMN 미지원 → PRAGMA 로 체크)
"""


def _ensure_idempotency_schema() -> None:
    """trades 테이블에 idempotency_key 컬럼 추가 (없을 때만)."""
    with db.connection() as conn:
        cols = conn.execute("PRAGMA table_info(trades)").fetchall()
        col_names = {c["name"] for c in cols}
        if "idempotency_key" not in col_names:
            conn.execute("ALTER TABLE trades ADD COLUMN idempotency_key TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_idem "
            "ON trades(idempotency_key, executed_at)"
        )


def make_key(strategy: str, symbol: str, side: str, qty: int,
             when: Optional[date] = None) -> str:
    """멱등 키 생성. when 미지정 시 오늘."""
    when = when or date.today()
    side_norm = side.upper()
    return f"{strategy}:{symbol}:{side_norm}:{qty}:{when.isoformat()}"


def already_executed(key: str, within_hours: int = 24) -> bool:
    """동일 키가 within_hours 내 trades 에 존재하는지."""
    _ensure_idempotency_schema()
    cutoff = (datetime.now() - timedelta(hours=within_hours)).isoformat()
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM trades
            WHERE idempotency_key = ? AND env = ? AND executed_at >= ?
            LIMIT 1
            """,
            (key, config.KIS_ENV, cutoff),
        ).fetchone()
    return row is not None
