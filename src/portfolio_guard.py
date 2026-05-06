"""포트폴리오 max drawdown 가드레일.

전체 자본이 시드 대비 임계값 (-10%) 이하로 빠지면 자동 halt:
  - 모든 신규 진입 (BUY) 차단
  - 기존 보유 청산 룰 (손절 / trailing) 은 정상 동작
  - 회복 (-8% 이상) 시 자동 해제 (flapping 방지 hysteresis)

운영:
  daily_snapshot 마지막에 update_drawdown() 호출 → halt 상태 갱신
  각 strategy 진입 전 is_halted() 체크

사용:
  from . import portfolio_guard
  if portfolio_guard.is_halted():
      print("[차단] 포트폴리오 halt 활성")
      return

스키마:
  portfolio_state(env, halted_at, halt_reason, current_dd_pct, max_dd_pct, updated_at)
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from . import config
from . import db

# 시드 기준 (KR 자본 기준 — daily_snapshot 의 KR_SEED_KRW 와 동일)
SEED_KRW = 50_000_000

# 임계값
HALT_THRESHOLD_PCT = -10.0   # 시드 대비 -10% 이하 → halt
RECOVERY_THRESHOLD_PCT = -8.0  # -8% 위로 회복 → halt 해제 (hysteresis)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_state (
    env             TEXT    PRIMARY KEY,
    halted          INTEGER DEFAULT 0,        -- 0 / 1
    halted_at       TEXT,                     -- halt 시작 timestamp
    halt_reason     TEXT,                     -- 'max_dd' / manual 등
    current_dd_pct  REAL    DEFAULT 0.0,      -- 현재 drawdown %
    max_dd_pct      REAL    DEFAULT 0.0,      -- 역대 최저 drawdown %
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _ensure_schema() -> None:
    with db.connection() as conn:
        conn.executescript(_SCHEMA)


def _load() -> dict:
    """현재 env 의 portfolio_state. 없으면 기본값 dict."""
    _ensure_schema()
    with db.connection() as conn:
        row = conn.execute(
            "SELECT * FROM portfolio_state WHERE env = ?",
            (config.KIS_ENV,),
        ).fetchone()
    if row is None:
        return {
            "halted": False,
            "halted_at": None,
            "halt_reason": None,
            "current_dd_pct": 0.0,
            "max_dd_pct": 0.0,
        }
    return {
        "halted": bool(row["halted"]),
        "halted_at": row["halted_at"],
        "halt_reason": row["halt_reason"],
        "current_dd_pct": float(row["current_dd_pct"] or 0),
        "max_dd_pct": float(row["max_dd_pct"] or 0),
    }


def _save(state: dict) -> None:
    _ensure_schema()
    with db.connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO portfolio_state
                (env, halted, halted_at, halt_reason, current_dd_pct,
                 max_dd_pct, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                config.KIS_ENV,
                int(state["halted"]),
                state["halted_at"],
                state["halt_reason"],
                state["current_dd_pct"],
                state["max_dd_pct"],
                datetime.now().isoformat(),
            ),
        )


def update_drawdown(total_value_krw: int, seed_krw: int = SEED_KRW) -> dict:
    """현재 평가금액 기반으로 drawdown 갱신 + halt 상태 결정.

    daily_snapshot 마지막에 호출.

    반환: 갱신된 state dict (halted, current_dd_pct 등).
    """
    if seed_krw <= 0:
        return _load()

    current_dd = (total_value_krw - seed_krw) / seed_krw * 100
    state = _load()

    state["current_dd_pct"] = current_dd
    if current_dd < state["max_dd_pct"]:
        state["max_dd_pct"] = current_dd

    # Halt 결정 (hysteresis)
    if not state["halted"]:
        if current_dd <= HALT_THRESHOLD_PCT:
            state["halted"] = True
            state["halted_at"] = datetime.now().isoformat()
            state["halt_reason"] = f"max_dd ({current_dd:.2f}% ≤ {HALT_THRESHOLD_PCT}%)"
    else:
        if current_dd > RECOVERY_THRESHOLD_PCT:
            state["halted"] = False
            state["halted_at"] = None
            state["halt_reason"] = None

    _save(state)
    return state


def is_halted() -> bool:
    """현재 포트폴리오 halt 상태. 진입 모듈이 BUY 전 체크."""
    return _load()["halted"]


def status() -> dict:
    """디버깅용 / 헬스체크용 — 현재 state 그대로 반환."""
    return _load()


def manual_halt(reason: str = "manual") -> None:
    """수동 halt 활성 (예: 사용자가 시장 충격 인지 시)."""
    state = _load()
    state["halted"] = True
    state["halted_at"] = datetime.now().isoformat()
    state["halt_reason"] = reason
    _save(state)


def manual_resume() -> None:
    """수동 halt 해제."""
    state = _load()
    state["halted"] = False
    state["halted_at"] = None
    state["halt_reason"] = None
    _save(state)
