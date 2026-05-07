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
    assert any("실제 에러 신호" in i for i in result["issues"])


def test_zsh_benign_warning_only_is_healthy(temp_logs):
    """launchd 의 무해한 zsh 경고만 있는 .err 는 false alarm 안 발생."""
    _touch(temp_logs / "daily_swing_v3_us.log", age_hours=2)
    err = temp_logs / "daily_swing_v3_us.err"
    err.write_text(
        "shell-init: error retrieving current directory: getcwd: cannot access\n"
        "chdir: error retrieving current directory: getcwd: cannot access\n"
    )
    os.utime(err, (time.time() - 3600, time.time() - 3600))
    result = trigger_check.run_check()
    assert result["healthy"] is True


def test_traceback_keyword_triggers_alert(temp_logs):
    """Python Traceback 이 .err 에 있으면 unhealthy."""
    _touch(temp_logs / "daily_swing_v3_us.log", age_hours=2)
    err = temp_logs / "daily_swing_v3_us.err"
    err.write_text(
        "shell-init: error retrieving current directory\n"
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in <module>\n'
        "    raise ValueError\n"
    )
    os.utime(err, (time.time() - 3600, time.time() - 3600))
    result = trigger_check.run_check()
    assert result["healthy"] is False


def test_korean_error_keyword(temp_logs):
    """한국어 '오류' 키워드도 매치."""
    _touch(temp_logs / "daily_swing_v3_us.log", age_hours=2)
    err = temp_logs / "daily_swing_v3_us.err"
    err.write_text("API 오류 [40580000] 모의투자 장종료 입니다.\n")
    os.utime(err, (time.time() - 3600, time.time() - 3600))
    result = trigger_check.run_check()
    assert result["healthy"] is False


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
