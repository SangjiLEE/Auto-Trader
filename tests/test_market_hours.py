"""market_hours.py — 휴장일 + KIS 모의투자 시간 가드 테스트."""
from __future__ import annotations

from datetime import date, datetime, time

import pytest

from src import market_hours


# ── 한국 휴장일 ──────────────────────────────

def test_kr_weekday_open():
    assert market_hours.is_kr_market_open(date(2026, 5, 7)) is True  # Thu


def test_kr_saturday_closed():
    assert market_hours.is_kr_market_open(date(2026, 5, 9)) is False


def test_kr_sunday_closed():
    assert market_hours.is_kr_market_open(date(2026, 5, 10)) is False


def test_kr_holiday_childrens_day():
    assert market_hours.is_kr_market_open(date(2026, 5, 5)) is False


def test_kr_holiday_new_year():
    assert market_hours.is_kr_market_open(date(2026, 1, 1)) is False


def test_kr_holiday_chuseok():
    assert market_hours.is_kr_market_open(date(2026, 9, 24)) is False


# ── 미국 휴장일 ──────────────────────────────

def test_us_weekday_open():
    assert market_hours.is_us_market_open(date(2026, 5, 7)) is True


def test_us_saturday_closed():
    assert market_hours.is_us_market_open(date(2026, 5, 9)) is False


def test_us_holiday_independence_day():
    assert market_hours.is_us_market_open(date(2026, 7, 3)) is False


def test_us_holiday_thanksgiving():
    assert market_hours.is_us_market_open(date(2026, 11, 26)) is False


def test_us_holiday_christmas():
    assert market_hours.is_us_market_open(date(2026, 12, 25)) is False


# ── KIS 모의투자 시간 가드 ───────────────────

def test_kis_paper_open_at_10am():
    now = datetime(2026, 5, 7, 10, 0)  # Thu 10:00
    assert market_hours.is_kis_paper_open(now) is True


def test_kis_paper_closed_at_15pm():
    now = datetime(2026, 5, 7, 15, 0)  # Thu 15:00 (정규장 중이지만 모의 cutoff 후)
    assert market_hours.is_kis_paper_open(now) is False


def test_kis_paper_closed_at_8am():
    now = datetime(2026, 5, 7, 8, 0)
    assert market_hours.is_kis_paper_open(now) is False


def test_kis_paper_closed_on_weekend():
    now = datetime(2026, 5, 9, 10, 0)  # Sat 10:00
    assert market_hours.is_kis_paper_open(now) is False


def test_kis_paper_closed_on_holiday():
    now = datetime(2026, 5, 5, 10, 0)  # 어린이날
    assert market_hours.is_kis_paper_open(now) is False


# ── assert_kis_paper_market_open ─────────────

def test_assert_passes_in_open_hours(monkeypatch):
    """월요일 10:00 KST = 운영시간 → 예외 없음."""
    fake_now = datetime(2026, 5, 4, 10, 0)
    monkeypatch.setattr(
        market_hours, "datetime",
        type("FakeDT", (), {"now": staticmethod(lambda: fake_now)})
    )
    market_hours.assert_kis_paper_market_open()


def test_assert_raises_on_holiday(monkeypatch):
    fake_now = datetime(2026, 5, 5, 10, 0)  # 어린이날
    monkeypatch.setattr(
        market_hours, "datetime",
        type("FakeDT", (), {"now": staticmethod(lambda: fake_now)})
    )
    with pytest.raises(RuntimeError, match="휴장일"):
        market_hours.assert_kis_paper_market_open()


def test_assert_raises_after_cutoff(monkeypatch):
    fake_now = datetime(2026, 5, 4, 15, 0)  # 월요일 15:00 (cutoff 후)
    monkeypatch.setattr(
        market_hours, "datetime",
        type("FakeDT", (), {"now": staticmethod(lambda: fake_now)})
    )
    with pytest.raises(RuntimeError, match="운영 시간 외"):
        market_hours.assert_kis_paper_market_open()
