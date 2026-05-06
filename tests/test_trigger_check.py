"""trigger_check.py 단위 테스트."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from src import trigger_check


@pytest.fixture
def temp_logs(tmp_path, monkeypatch):
    """LOGS_DIR 를 임시 경로로 교체."""
    monkeypatch.setattr(trigger_check, "LOGS_DIR", tmp_path)
    return tmp_path


def _touch(path: Path, age_hours: float = 0):
    path.write_text("dummy")
    if age_hours > 0:
        mtime = time.time() - age_hours * 3600
        os.utime(path, (mtime, mtime))


def test_healthy_when_log_recent(temp_logs):
    _touch(temp_logs / "daily_swing_v3_us.log", age_hours=2)
    result = trigger_check.run_check()
    assert result["healthy"] is True
    assert result["issues"] == []


def test_unhealthy_when_log_missing(temp_logs):
    result = trigger_check.run_check()
    assert result["healthy"] is False
    assert any("log 파일 부재" in i for i in result["issues"])


def test_unhealthy_when_log_stale(temp_logs):
    _touch(temp_logs / "daily_swing_v3_us.log", age_hours=30)
    result = trigger_check.run_check()
    assert result["healthy"] is False
    assert any("미갱신" in i for i in result["issues"])


def test_unhealthy_when_err_recent_and_nonempty(temp_logs):
    _touch(temp_logs / "daily_swing_v3_us.log", age_hours=2)
    err = temp_logs / "daily_swing_v3_us.err"
    err.write_text("ERROR\n")
    os.utime(err, (time.time() - 3600, time.time() - 3600))
    result = trigger_check.run_check()
    assert result["healthy"] is False
    assert any("err 파일 비어있지 않음" in i for i in result["issues"])


def test_old_err_ignored(temp_logs):
    _touch(temp_logs / "daily_swing_v3_us.log", age_hours=2)
    err = temp_logs / "daily_swing_v3_us.err"
    err.write_text("ERROR")
    mtime = time.time() - 30 * 3600
    os.utime(err, (mtime, mtime))
    result = trigger_check.run_check()
    assert result["healthy"] is True


def test_format_report_healthy():
    r = {"checked_at": "2026-05-07T00:05:00", "n_targets": 1, "issues": [], "healthy": True}
    out = trigger_check.format_report(r)
    assert "HEALTHY" in out
    assert "🚨" not in out


def test_format_report_unhealthy():
    r = {
        "checked_at": "2026-05-07T00:05:00",
        "n_targets": 1,
        "issues": ["daily_swing_v3_us: log 미갱신 30h"],
        "healthy": False,
    }
    out = trigger_check.format_report(r)
    assert "TRIGGER 실패" in out
    assert "🚨" in out
