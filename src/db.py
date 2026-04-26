"""
SQLite DB 연결과 스키마.

DB 파일: 프로젝트 루트의 data.db (.gitignore 로 보호됨).
나중에 PostgreSQL로 옮길 때도 같은 스키마·헬퍼 구조 유지 가능.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH = Path(__file__).parent.parent / "data.db"

SCHEMA = """
-- 일봉 시계열
CREATE TABLE IF NOT EXISTS daily_candles (
    symbol       TEXT     NOT NULL,
    market       TEXT     NOT NULL,   -- 'KR' | 'US'
    date         DATE     NOT NULL,
    open         REAL     NOT NULL,
    high         REAL     NOT NULL,
    low          REAL     NOT NULL,
    close        REAL     NOT NULL,
    volume       INTEGER  NOT NULL,
    adj_close    REAL,                 -- 배당·분할 보정 종가 (미국주)
    change_rate  REAL,                 -- 등락률 (0.02 = 2%)
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, date)
);

CREATE INDEX IF NOT EXISTS idx_candles_symbol_date
    ON daily_candles(symbol, date);

-- 종목 메타데이터 (나중에 이름·섹터 채움)
CREATE TABLE IF NOT EXISTS symbols (
    symbol      TEXT  PRIMARY KEY,
    name        TEXT,
    market      TEXT  NOT NULL,
    sector      TEXT,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 실거래·모의 체결 로그 (Week 3+ 에 사용)
CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT    NOT NULL,
    side         TEXT    NOT NULL,     -- 'buy' | 'sell'
    quantity     INTEGER NOT NULL,
    price        REAL    NOT NULL,
    executed_at  TIMESTAMP NOT NULL,
    env          TEXT    NOT NULL,     -- 'paper' | 'real'
    strategy     TEXT,
    order_id     TEXT,
    notes        TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_executed ON trades(executed_at);
"""


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    """with 블록 종료 시 자동 커밋·close."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_schema() -> None:
    """스키마 생성. 이미 있으면 스킵."""
    with connection() as conn:
        conn.executescript(SCHEMA)


def reset() -> None:
    """데이터 + 스키마 초기화. 파괴적 작업. 디버깅용."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_schema()


if __name__ == "__main__":
    # python -m src.db → 스키마만 초기화
    init_schema()
    print(f"DB 스키마 준비 완료: {DB_PATH}")
