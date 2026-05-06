"""safety.py — 실거래 가드 테스트.

실거래 사고 직결 모듈이라 가장 먼저 단위 테스트 고정.
"""
from __future__ import annotations

import pytest

from src import config, safety


def test_is_paper_when_paper(monkeypatch):
    monkeypatch.setattr(config, "KIS_ENV", "paper")
    assert safety.is_paper() is True


def test_is_paper_when_real(monkeypatch):
    monkeypatch.setattr(config, "KIS_ENV", "real")
    assert safety.is_paper() is False


def test_assert_paper_passes_in_paper(monkeypatch):
    monkeypatch.setattr(config, "KIS_ENV", "paper")
    safety.assert_paper(label="테스트")  # 예외 없어야 함


def test_assert_paper_raises_in_real(monkeypatch):
    monkeypatch.setattr(config, "KIS_ENV", "real")
    with pytest.raises(RuntimeError, match="실거래"):
        safety.assert_paper(label="테스트")


def test_block_execute_passes_when_not_executing(monkeypatch):
    monkeypatch.setattr(config, "KIS_ENV", "real")
    # execute=False 면 KIS_ENV 무관하게 차단 안 함 (드라이런 허용)
    assert safety.block_execute_if_real(execute=False) is False


def test_block_execute_passes_when_paper(monkeypatch):
    monkeypatch.setattr(config, "KIS_ENV", "paper")
    assert safety.block_execute_if_real(execute=True) is False


def test_block_execute_blocks_when_real_execute(monkeypatch, capsys):
    monkeypatch.setattr(config, "KIS_ENV", "real")
    blocked = safety.block_execute_if_real(execute=True)
    assert blocked is True
    captured = capsys.readouterr()
    assert "차단" in captured.out
    assert "real" in captured.out
