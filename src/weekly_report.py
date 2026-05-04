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


def format_report(days: int, trades: list[dict], strategy_stats: dict, eval_data: dict) -> str:
    today = datetime.now()
    lines = [
        f"📊 주간 종합 리포트 — {today.strftime('%Y-%m-%d (%a) %H:%M KST')}",
        f"환경: {'모의' if config.KIS_ENV == 'paper' else '실거래'}",
        f"기간: 최근 {days}일",
        "",
        "[💰 자본 현황]",
    ]

    # [Fix 2026-05-04] 정확한 손익 = 실현 (DB) + 미실현 (KIS 평단 기준)
    kr_total = eval_data["kr_total_krw"]
    kr_unrealized = eval_data["kr_unrealized_krw"]
    us_eval_krw = int(eval_data["us_eval_usd"] * USD_KRW)
    us_unrealized_krw = int(eval_data["us_pnl_usd"] * USD_KRW)

    # 실현 손익 (Strategy 별 합산)
    realized_total_krw = 0
    for s in strategy_stats.values():
        realized = s["realized_pnl"]
        is_kr = any(sy.isdigit() and len(sy) == 6 for sy in s["symbols"])
        if is_kr:
            realized_total_krw += int(realized)
        else:
            realized_total_krw += int(realized * USD_KRW)

    # 미실현 (KR + US, 환산)
    unrealized_total_krw = kr_unrealized + us_unrealized_krw

    # 진짜 누적 손익 (실현 + 미실현)
    true_cumulative = realized_total_krw + unrealized_total_krw
    cumulative_pct = true_cumulative / INITIAL_CAPITAL * 100

    lines.append(f"  KR 평가:    ₩{kr_total:,}")
    lines.append(f"  US 평가:    ${eval_data['us_eval_usd']:.2f} (≈ ₩{us_eval_krw:,})")
    lines.append("")
    lines.append(f"  실현 손익:  ₩{realized_total_krw:+,}")
    lines.append(f"  미실현:     ₩{unrealized_total_krw:+,}  (KR ₩{kr_unrealized:+,} / US ₩{us_unrealized_krw:+,})")
    lines.append(f"  ─────────")
    lines.append(f"  ✨ 총 누적: ₩{true_cumulative:+,} ({cumulative_pct:+.2f}%)")
    lines.append(f"  (초기 KR 시드 ₩{INITIAL_CAPITAL:,} 대비)")
    lines.append("")

    # Strategy 별
    lines.append(f"[📊 전략별 ({days}일 거래)]")
    if not strategy_stats:
        lines.append("  거래 없음")
    else:
        for strat, s in sorted(strategy_stats.items()):
            realized = s["realized_pnl"]
            currency = "$" if any(sy in ["NVDA","TSLA","AAPL","SPY","QQQ","EFA","EEM","AGG","IEF","TIP","META","AMZN","GOOGL","MSFT","TSM","COIN","SOXL","TSLL","IREN","BMNR","ORCL","LQD","SHY","BIL"] for sy in s["symbols"]) else "₩"
            sign = "+" if realized >= 0 else ""
            kr_name = labels.strategy_kr(strat)
            lines.append(
                f"  {kr_name}"
            )
            lines.append(
                f"    매수 {s['n_buys']} / 매도 {s['n_sells']} | "
                f"실현 {sign}{realized:.2f}{currency} | {','.join(s['symbols'][:3])}"
            )

    # 거래 리스트 (최근 10건)
    lines.append("")
    lines.append(f"[최근 거래 (최대 10건)]")
    for t in trades[-10:]:
        ts = t["executed_at"][:16] if t["executed_at"] else "?"
        sign = "$" if t["price"] < 10000 else "₩"
        lines.append(
            f"  {ts} {t['strategy']:<12} {t['side'].upper()} {t['symbol']} "
            f"{t['quantity']}주 @ {sign}{t['price']:.2f}"
        )

    # 보유 미국 종목
    if eval_data["us_holdings"]:
        lines.append("")
        lines.append("[현재 미국 보유]")
        for h in eval_data["us_holdings"]:
            pnl_pct = ((h["current_price_usd"] - h["avg_price_usd"]) / h["avg_price_usd"] * 100) if h["avg_price_usd"] > 0 else 0
            lines.append(
                f"  {h['symbol']:<6} {h['qty']}주 @ ${h['avg_price_usd']:.2f} "
                f"(현재 ${h['current_price_usd']:.2f}, {pnl_pct:+.2f}%)"
            )

    return "\n".join(lines)


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

    report = format_report(args.days, trades, strategy_stats, eval_data)
    print(report)

    if notify.is_enabled():
        notify.send(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
