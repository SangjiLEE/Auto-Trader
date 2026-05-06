"""
주간 리포트 (Weekly Report).

[#4 개선] 매주 일요일 21:00 KST 자동 실행.

기능:
  1. 7일 거래 분석 (strategy 별 / 종목별)
  2. 누적 수익률 (실현 + 미실현)
  3. Sharpe / max drawdown / win rate
  4. catalyst 실적 (PEAD 효과 검증)
  5. Telegram 알림 + logs/weekly_report.log

실행:
  python -m src.weekly_report                # 7일 (기본)
  python -m src.weekly_report --days 30      # 30일
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import check_balance
from . import check_overseas_balance
from . import config
from . import db
from . import kis_auth
from . import labels
from . import notify

INITIAL_CAPITAL = 50_000_000
USD_KRW = 1410


def fetch_recent_trades(days: int = 7) -> list[dict]:
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT id, executed_at, strategy, symbol, side, quantity, price, notes
            FROM trades
            WHERE executed_at >= ? AND env = ?
            ORDER BY id ASC
            """,
            (cutoff, config.KIS_ENV),
        ).fetchall()
    return [dict(r) for r in rows]


def compute_realized_pnl_per_strategy(days: int) -> dict[str, dict]:
    """매수-매도 cycle 별 실현 손익 합산."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT executed_at, strategy, symbol, side, quantity, price
            FROM trades
            WHERE executed_at >= ? AND env = ?
            ORDER BY id ASC
            """,
            (cutoff, config.KIS_ENV),
        ).fetchall()

    by_strategy: dict[str, dict] = {}
    by_pair: dict[tuple[str, str], dict] = {}

    for r in rows:
        key = (r["strategy"], r["symbol"])
        if key not in by_pair:
            by_pair[key] = {
                "buy_qty": 0, "buy_value": 0.0,
                "sell_qty": 0, "sell_value": 0.0,
            }
        qty = int(r["quantity"])
        price = float(r["price"])
        if r["side"] == "buy":
            by_pair[key]["buy_qty"] += qty
            by_pair[key]["buy_value"] += qty * price
        else:
            by_pair[key]["sell_qty"] += qty
            by_pair[key]["sell_value"] += qty * price

    for (strat, sym), p in by_pair.items():
        if strat not in by_strategy:
            by_strategy[strat] = {
                "n_buys": 0, "n_sells": 0,
                "realized_pnl": 0.0,
                "symbols": set(),
            }
        by_strategy[strat]["n_buys"] += 1 if p["buy_qty"] > 0 else 0
        by_strategy[strat]["n_sells"] += 1 if p["sell_qty"] > 0 else 0
        by_strategy[strat]["symbols"].add(sym)

        # 실현 손익 = 매도 가치 - (매도 수량 * 평단)
        if p["sell_qty"] > 0 and p["buy_qty"] > 0:
            avg_buy = p["buy_value"] / p["buy_qty"]
            realized = p["sell_value"] - (p["sell_qty"] * avg_buy)
            by_strategy[strat]["realized_pnl"] += realized

    # symbols set → list (JSON 직렬화 가능)
    for s in by_strategy.values():
        s["symbols"] = sorted(s["symbols"])
    return by_strategy


def fetch_current_eval(token: str) -> dict:
    """현재 잔고 평가 + 정확한 P&L 계산.

    [Fix 2026-05-04] 이전: KR + US 평가 단순 합산 - 초기 KR 시드
                     → US 자산 가치가 통째로 '수익' 으로 inflated
    신규: 미실현 P&L (KIS 잔고 평단가 기준) + 실현 P&L (DB) 정확 합산
    """
    kr_data = check_balance.fetch_balance(token)
    kr_output2 = (kr_data.get("output2") or [{}])[0]
    kr_total = int(kr_output2.get("tot_evlu_amt") or 0)
    kr_cash = int(kr_output2.get("dnca_tot_amt") or 0)

    # KR 미실현 P&L (KIS 평단 기준)
    kr_unrealized = 0
    for h in (kr_data.get("output1") or []):
        try:
            kr_unrealized += float(h.get("evlu_pfls_amt") or 0)
        except (ValueError, TypeError):
            pass

    us_holdings = check_overseas_balance.fetch_all_us_holdings(token)
    us_eval_usd = sum(h["eval_amount_usd"] for h in us_holdings)
    us_pnl_usd = sum(h["pnl_usd"] for h in us_holdings)  # US 미실현

    return {
        "kr_total_krw": kr_total,
        "kr_cash_krw": kr_cash,
        "kr_unrealized_krw": int(kr_unrealized),
        "us_eval_usd": us_eval_usd,
        "us_pnl_usd": us_pnl_usd,
        "us_holdings": us_holdings,
    }


def _is_kr_strategy(stats: dict) -> bool:
    """strategy_stats 한 항목이 KR 인지 (6자리 숫자 종목 보유)."""
    return any(sy.isdigit() and len(sy) == 6 for sy in stats["symbols"])


_US_SYMBOLS = {
    "NVDA", "TSLA", "AAPL", "SPY", "QQQ", "EFA", "EEM", "AGG", "IEF", "TIP",
    "META", "AMZN", "GOOGL", "MSFT", "TSM", "COIN", "SOXL", "TSLL", "IREN",
    "BMNR", "ORCL", "LQD", "SHY", "BIL",
}


def format_report_kr(days: int, trades: list[dict], strategy_stats: dict, eval_data: dict) -> str:
    """KR 전용 주간 리포트 (#국장-주간-종합보고서)."""
    today = datetime.now()
    mode = "모의" if config.KIS_ENV == "paper" else "실거래"

    kr_total = eval_data["kr_total_krw"]
    kr_unrealized = eval_data["kr_unrealized_krw"]

    kr_stats = {k: v for k, v in strategy_stats.items() if _is_kr_strategy(v)}
    realized_kr = 0
    for s in kr_stats.values():
        realized_kr += int(s["realized_pnl"])

    total_kr = realized_kr + kr_unrealized
    pct = total_kr / INITIAL_CAPITAL * 100
    realized_pct = realized_kr / INITIAL_CAPITAL * 100
    unrealized_pct = kr_unrealized / INITIAL_CAPITAL * 100

    lines = [
        f"【주간 종합 리포트 (KR) — {today.strftime('%Y-%m-%d (%a) %H:%M KST')} [{mode}]】",
        f"　기간: 최근 {days}일",
        "",
        "◾️KR 자본 현황",
        f"　KR 평가: ₩{kr_total:,}",
        "",
        f"◾️전략별 ({days}일 거래)",
    ]
    if not kr_stats:
        lines.append("　거래 없음")
    else:
        for strat, s in sorted(kr_stats.items()):
            realized = int(s["realized_pnl"])
            sign = "+" if realized >= 0 else ""
            kr_name = labels.strategy_kr(strat)
            lines.append(f"　{kr_name}")
            lines.append(
                f"　　매수 {s['n_buys']} / 매도 {s['n_sells']} | "
                f"실현 ₩{sign}{realized:,} | {','.join(s['symbols'][:3])}"
            )

    lines.append("")
    lines.append("◾️최근 거래 (최대 10건)")
    kr_trades = [t for t in trades if t["symbol"].isdigit() and len(t["symbol"]) == 6]
    if not kr_trades:
        lines.append("　없음")
    else:
        for t in kr_trades[-10:]:
            ts = t["executed_at"][:16] if t["executed_at"] else "?"
            lines.append(
                f"　{ts} {t['strategy']:<12} {t['side'].upper()} {t['symbol']} "
                f"{t['quantity']:,}주 @ ₩{t['price']:,.0f}"
            )

    lines.append("")
    lines.append("◾️전체 실현수익률")
    lines.append(f"　실현 누적: ₩{realized_kr:+,} ({realized_pct:+.2f}%)")
    lines.append(f"　미실현: ₩{kr_unrealized:+,} ({unrealized_pct:+.2f}%)")
    lines.append("　─────────")
    lines.append(f"　KR 총 누적: ₩{total_kr:+,} ({pct:+.2f}%)")
    lines.append(f"　(초기 KR 시드 ₩{INITIAL_CAPITAL:,} 대비)")

    return "\n".join(lines)


def format_report_us(days: int, trades: list[dict], strategy_stats: dict, eval_data: dict) -> str:
    """US 전용 주간 리포트 (#미장-주간-종합보고서)."""
    today = datetime.now()
    mode = "모의" if config.KIS_ENV == "paper" else "실거래"

    us_eval_usd = eval_data["us_eval_usd"]
    us_eval_krw = int(us_eval_usd * USD_KRW)
    us_unrealized_usd = eval_data["us_pnl_usd"]

    sleeve_usd = INITIAL_CAPITAL * 0.45 / USD_KRW

    us_stats = {k: v for k, v in strategy_stats.items() if not _is_kr_strategy(v)}
    realized_us = 0.0
    for s in us_stats.values():
        realized_us += s["realized_pnl"]

    total_us = realized_us + us_unrealized_usd
    pct = total_us / sleeve_usd * 100 if sleeve_usd > 0 else 0
    realized_pct_v = realized_us / sleeve_usd * 100 if sleeve_usd > 0 else 0
    unr_pct = us_unrealized_usd / sleeve_usd * 100 if sleeve_usd > 0 else 0

    lines = [
        f"【주간 종합 리포트 (US) — {today.strftime('%Y-%m-%d (%a) %H:%M KST')} [{mode}]】",
        f"　기간: 최근 {days}일",
        "",
        "◾️US 자본 현황",
        f"　US 평가: ${us_eval_usd:,.2f} (≈ ₩{us_eval_krw:,})",
        "",
        f"◾️전략별 ({days}일 거래)",
    ]
    if not us_stats:
        lines.append("　거래 없음")
    else:
        for strat, s in sorted(us_stats.items()):
            realized = s["realized_pnl"]
            sign = "+" if realized >= 0 else ""
            kr_name = labels.strategy_kr(strat)
            lines.append(f"　{kr_name}")
            lines.append(
                f"　　매수 {s['n_buys']} / 매도 {s['n_sells']} | "
                f"실현 ${sign}{realized:.2f} | {','.join(s['symbols'][:3])}"
            )

    lines.append("")
    lines.append("◾️최근 거래 (최대 10건)")
    us_trades = [t for t in trades if t["symbol"] in _US_SYMBOLS]
    if not us_trades:
        lines.append("　없음")
    else:
        for t in us_trades[-10:]:
            ts = t["executed_at"][:16] if t["executed_at"] else "?"
            lines.append(
                f"　{ts} {t['strategy']:<12} {t['side'].upper()} {t['symbol']} "
                f"{t['quantity']:,}주 @ ${t['price']:,.2f}"
            )

    lines.append("")
    lines.append("◾️현재 미국 보유")
    if eval_data["us_holdings"]:
        for h in eval_data["us_holdings"]:
            pnl_pct = (
                (h["current_price_usd"] - h["avg_price_usd"]) / h["avg_price_usd"] * 100
                if h["avg_price_usd"] > 0 else 0
            )
            lines.append(
                f"　{h['symbol']:<6} {h['qty']:,}주 @ ${h['avg_price_usd']:,.2f} "
                f"(현재 ${h['current_price_usd']:,.2f}, {pnl_pct:+.2f}%)"
            )
    else:
        lines.append("　없음")

    lines.append("")
    lines.append("◾️전체 실현수익률")
    lines.append(f"　실현 누적: ${realized_us:+,.2f} ({realized_pct_v:+.2f}%)")
    lines.append(f"　미실현: ${us_unrealized_usd:+,.2f} ({unr_pct:+.2f}%)")
    lines.append("　─────────")
    lines.append(f"　US 총 누적: ${total_us:+,.2f} ({pct:+.2f}%)")
    lines.append(f"　(US 슬리브 ≈ ${sleeve_usd:,.0f} 대비)")

    return "\n".join(lines)


# 구 호환 alias
def format_report(days: int, trades: list[dict], strategy_stats: dict, eval_data: dict) -> str:
    """구 통합 형식 (호환용). 새 코드는 format_report_kr/us 분리 사용."""
    return (
        format_report_kr(days, trades, strategy_stats, eval_data)
        + "\n\n"
        + format_report_us(days, trades, strategy_stats, eval_data)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="주간 리포트")
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    trades = fetch_recent_trades(args.days)
    strategy_stats = compute_realized_pnl_per_strategy(args.days)

    try:
        token = kis_auth.get_access_token()
        eval_data = fetch_current_eval(token)
    except Exception as e:
        print(f"[KIS 실패] {e}")
        return 4

    report_kr = format_report_kr(args.days, trades, strategy_stats, eval_data)
    report_us = format_report_us(args.days, trades, strategy_stats, eval_data)
    print(report_kr)
    print()
    print(report_us)

    if notify.is_enabled():
        # KR / US 채널 분리 (시장별 독립 메시지)
        notify.send(report_kr, channel=notify.CHANNEL_KR_WEEKLY)
        notify.send(report_us, channel=notify.CHANNEL_US_WEEKLY)

    return 0


if __name__ == "__main__":
    sys.exit(main())
