"""src/weekly_report.py 단위 테스트.

KR/US 분리 + 종목 분류 검증.
"""
from __future__ import annotations

from src import weekly_report


def test_is_kr_strategy_kr_symbol():
    assert weekly_report._is_kr_strategy({"symbols": ["069500"]})


def test_is_kr_strategy_us_symbol():
    assert not weekly_report._is_kr_strategy({"symbols": ["NVDA"]})


def test_is_kr_strategy_mixed_kr_takes_precedence():
    """KR 종목이 하나라도 있으면 KR 로 분류."""
    assert weekly_report._is_kr_strategy({"symbols": ["NVDA", "069500"]})


def test_is_kr_strategy_empty():
    assert not weekly_report._is_kr_strategy({"symbols": []})


def test_us_symbols_set_contains_majors():
    """US universe 가 모든 strategy 의 종목을 포함."""
    expected = {"NVDA", "AAPL", "SPY", "QQQ", "EFA"}
    assert expected.issubset(weekly_report._US_SYMBOLS)


def test_format_kr_with_kr_only_stats():
    """KR 종목만 있는 stats → KR 리포트에만 포함."""
    eval_data = {
        "kr_total_krw": 50_000_000,
        "kr_unrealized_krw": 0,
        "us_eval_usd": 0.0,
        "us_pnl_usd": 0.0,
        "us_holdings": [],
    }
    stats = {
        "vaa": {
            "n_buys": 1, "n_sells": 0, "realized_pnl": 0.0,
            "symbols": ["069500"],
        },
    }
    report = weekly_report.format_report_kr(7, [], stats, eval_data)
    assert "069500" in report
    assert "VAA" in report or "vaa" in report
    assert "주간 종합 리포트 (KR)" in report


def test_format_us_with_us_only_stats():
    """US 종목만 있는 stats → US 리포트에만 포함."""
    eval_data = {
        "kr_total_krw": 50_000_000,
        "kr_unrealized_krw": 0,
        "us_eval_usd": 1000.0,
        "us_pnl_usd": 50.0,
        "us_holdings": [],
    }
    stats = {
        "swing_v3": {
            "n_buys": 1, "n_sells": 0, "realized_pnl": 0.0,
            "symbols": ["NVDA"],
        },
    }
    report = weekly_report.format_report_us(7, [], stats, eval_data)
    assert "NVDA" in report
    assert "주간 종합 리포트 (US)" in report


def test_format_kr_excludes_us_strategies():
    """US strategy 는 KR 리포트에서 제외."""
    eval_data = {
        "kr_total_krw": 50_000_000,
        "kr_unrealized_krw": 0,
        "us_eval_usd": 1000.0,
        "us_pnl_usd": 0.0,
        "us_holdings": [],
    }
    stats = {
        "vaa": {"n_buys": 1, "n_sells": 0, "realized_pnl": 0.0,
                "symbols": ["069500"]},
        "catalyst": {"n_buys": 1, "n_sells": 0, "realized_pnl": 0.0,
                     "symbols": ["AAPL"]},
    }
    kr_report = weekly_report.format_report_kr(7, [], stats, eval_data)
    assert "AAPL" not in kr_report
    assert "069500" in kr_report


def test_format_us_excludes_kr_strategies():
    """KR strategy 는 US 리포트에서 제외."""
    eval_data = {
        "kr_total_krw": 50_000_000,
        "kr_unrealized_krw": 0,
        "us_eval_usd": 1000.0,
        "us_pnl_usd": 0.0,
        "us_holdings": [],
    }
    stats = {
        "vaa": {"n_buys": 1, "n_sells": 0, "realized_pnl": 0.0,
                "symbols": ["069500"]},
        "catalyst": {"n_buys": 1, "n_sells": 0, "realized_pnl": 0.0,
                     "symbols": ["AAPL"]},
    }
    us_report = weekly_report.format_report_us(7, [], stats, eval_data)
    assert "069500" not in us_report
    assert "AAPL" in us_report


def test_kr_realized_percentage_present():
    """KR 리포트에 % 표시 (이전 버그 회귀 방지)."""
    eval_data = {
        "kr_total_krw": 50_000_000,
        "kr_unrealized_krw": 500_000,
        "us_eval_usd": 0.0,
        "us_pnl_usd": 0.0,
        "us_holdings": [],
    }
    stats = {}
    report = weekly_report.format_report_kr(7, [], stats, eval_data)
    assert "%" in report
    # 미실현 line: "₩+500,000 (+1.00%)"
    assert "+500,000" in report
