"""src/portfolio_guard.py 단위 테스트.

halt / recover hysteresis 검증.
"""
from __future__ import annotations

from src import portfolio_guard


def test_initial_state_not_halted(temp_db):
    assert not portfolio_guard.is_halted()


def test_normal_drawdown_no_halt(temp_db):
    """-5% 면 halt 안 됨 (-10% 미만)."""
    state = portfolio_guard.update_drawdown(47_500_000, seed_krw=50_000_000)
    assert not state["halted"]
    assert state["current_dd_pct"] == -5.0


def test_threshold_crash_halts(temp_db):
    """-10% 이하 → halt 활성."""
    state = portfolio_guard.update_drawdown(45_000_000, seed_krw=50_000_000)
    assert state["halted"]
    assert state["halt_reason"] is not None
    assert "max_dd" in state["halt_reason"]


def test_deep_crash_halts(temp_db):
    """-15% 도 halt."""
    state = portfolio_guard.update_drawdown(42_500_000, seed_krw=50_000_000)
    assert state["halted"]
    assert state["current_dd_pct"] == -15.0


def test_hysteresis_partial_recovery_keeps_halt(temp_db):
    """-12% halt 후 -9% 회복 → halt 유지 (-8% 임계 미달)."""
    portfolio_guard.update_drawdown(44_000_000, seed_krw=50_000_000)
    assert portfolio_guard.is_halted()

    # 회복 -9% 는 RECOVERY (-8%) 미만 → halt 유지
    state = portfolio_guard.update_drawdown(45_500_000, seed_krw=50_000_000)
    assert state["halted"], "halt 유지되어야 함 (-9% 는 -8% 임계 미달)"


def test_full_recovery_releases_halt(temp_db):
    """halt 후 -7% 회복 → 자동 해제."""
    portfolio_guard.update_drawdown(44_000_000, seed_krw=50_000_000)
    assert portfolio_guard.is_halted()

    state = portfolio_guard.update_drawdown(46_500_000, seed_krw=50_000_000)
    assert not state["halted"]
    assert state["halt_reason"] is None


def test_max_dd_persists(temp_db):
    """max_dd_pct 는 역대 최저로 누적."""
    portfolio_guard.update_drawdown(45_000_000, seed_krw=50_000_000)  # -10
    portfolio_guard.update_drawdown(42_500_000, seed_krw=50_000_000)  # -15
    portfolio_guard.update_drawdown(48_000_000, seed_krw=50_000_000)  # -4 (recover)

    state = portfolio_guard.status()
    assert state["max_dd_pct"] == -15.0  # 역대 최저 유지


def test_manual_halt_resume(temp_db):
    portfolio_guard.manual_halt("market shock detected")
    state = portfolio_guard.status()
    assert state["halted"]
    assert state["halt_reason"] == "market shock detected"

    portfolio_guard.manual_resume()
    assert not portfolio_guard.is_halted()


def test_zero_seed_returns_safe(temp_db):
    """분모 0 보호."""
    state = portfolio_guard.update_drawdown(50_000_000, seed_krw=0)
    assert not state["halted"]
