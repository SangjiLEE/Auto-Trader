"""
미국장 마감 후 보고서.

US 정규장 마감 = 06:00 KST (DST) / 07:00 KST (표준시).
이 스크립트는 평일 한국시간 06:15 KST 에 실행 (Tue~Sat KST).
미국 잔고 + 직전 미국 close 대비 변동을 Telegram 으로 푸시.

실행:
  python -m src.us_closing_report
"""
from __future__ import annotations

import sys
from datetime import datetime

import pandas as pd

from . import check_overseas_balance
from . import config
from . import db
from . import kis_api
from . import kis_auth
from . import notify
from . import realized_pnl

US_STRATEGIES = ["faber_us", "swing_v3", "catalyst"]
# US 슬리브 분모 = 총 ALLOCATION 합계 / USD_KRW
USD_KRW_ESTIMATE = 1410
US_SLEEVE_KRW_RATIO = 0.45  # Faber 15% + NVDA v3 5% + Catalyst 25%

# 미국 종가 스냅샷 전용 테이블 (KR 스냅샷과 분리)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS us_close_snapshots (
    date          TEXT    NOT NULL,
    env           TEXT    NOT NULL,
    n_positions   INTEGER,
    eval_total    REAL,
    pnl_total     REAL,
    positions     TEXT,
    taken_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, env)
);
"""


def _ensure_schema() -> None:
    with db.connection() as conn:
        conn.executescript(_SCHEMA)


def _get_previous_us_snapshot() -> dict | None:
    """오늘 이전 가장 최근 US 스냅샷."""
    _ensure_schema()
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT date, eval_total, pnl_total
            FROM us_close_snapshots
            WHERE env = ? AND date < ?
            ORDER BY date DESC
            LIMIT 1
            """,
            (config.KIS_ENV, today),
        ).fetchone()
    if not row:
        return None
    return {
        "date": row["date"],
        "eval_total": float(row["eval_total"] or 0),
        "pnl_total": float(row["pnl_total"] or 0),
    }


def take_us_snapshot(token: str) -> dict:
    """미국 잔고 스냅샷 → DB 저장 + 반환."""
    holdings = check_overseas_balance.fetch_all_us_holdings(token)

    eval_total = sum(h["eval_amount_usd"] for h in holdings)
    pnl_total = sum(h["pnl_usd"] for h in holdings)

    today = datetime.now().strftime("%Y-%m-%d")

    import json
    _ensure_schema()
    with db.connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO us_close_snapshots
                (date, env, n_positions, eval_total, pnl_total, positions)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                today,
                config.KIS_ENV,
                len(holdings),
                eval_total,
                pnl_total,
                json.dumps(holdings, ensure_ascii=False),
            ),
        )

    return {
        "date": today,
        "n_positions": len(holdings),
        "eval_total": eval_total,
        "pnl_total": pnl_total,
        "positions": holdings,
    }


def _send_us_closing_report(snap: dict, prev: dict | None) -> None:
    if not notify.is_enabled():
        return

    today_str = pd.Timestamp.now().strftime("%-m/%-d (%a) %H:%M KST")
    mode = "모의" if config.KIS_ENV == "paper" else "실거래"

    # 헤더 블록
    lines = [
        f"*【미국장 마감 — {today_str} [{mode}]】*",
        f"　평가 합계: ${snap['eval_total']:,.2f}",
        f"　누적 손익: ${snap['pnl_total']:+,.2f}",
    ]

    if prev is not None and prev["eval_total"] > 0:
        diff = snap["eval_total"] - prev["eval_total"]
        diff_pct = diff / prev["eval_total"] * 100
        lines.append(f"　전 영업일 대비: ${diff:+,.2f} ({diff_pct:+.2f}%)")
    lines.append("")

    # 미국 보유 포지션
    lines.append("*◾️미국 보유 포지션*")
    if snap["positions"]:
        for p in snap["positions"]:
            pnl_pct = 0.0
            if p["avg_price_usd"] > 0:
                pnl_pct = (
                    (p["current_price_usd"] - p["avg_price_usd"])
                    / p["avg_price_usd"]
                    * 100
                )
            lines.append(
                f"　{p['symbol']:<6} {p['qty']:>4,}주 "
                f"@ ${p['avg_price_usd']:,.2f} → ${p['current_price_usd']:,.2f} "
                f"({pnl_pct:+.2f}%)"
            )
    else:
        lines.append("　없음")

    # 전체 실현수익률 (US strategy 합산)
    realized_map = realized_pnl.realized_for_strategies(US_STRATEGIES)
    realized_usd = realized_map["usd"]

    # US 슬리브 분모 (KR 시드 ₩50M * 45% / USD_KRW)
    sleeve_usd = 50_000_000 * US_SLEEVE_KRW_RATIO / USD_KRW_ESTIMATE
    realized_pct_v = realized_pnl.pct(realized_usd, sleeve_usd)

    # 미실현 = snap 의 pnl_total (KIS 평단 기준)
    unrealized_usd = snap["pnl_total"]
    unr_pct = realized_pnl.pct(unrealized_usd, sleeve_usd)

    lines.append("")
    lines.append("*◾️전체 실현수익률*")
    lines.append(f"　실현 누적: ${realized_usd:+,.2f} ({realized_pct_v:+.2f}%)")
    lines.append(f"　미실현: ${unrealized_usd:+,.2f} ({unr_pct:+.2f}%)")
    lines.append(f"　(US 슬리브 ≈ ${sleeve_usd:,.0f} 대비)")

    notify.send("\n".join(lines), channel=notify.CHANNEL_US_DAILY)


def main() -> int:
    try:
        config.validate()
    except ValueError as e:
        print(f"[설정 오류] {e}")
        return 1

    try:
        token = kis_auth.get_access_token()
    except kis_auth.KISAuthError as e:
        print(f"[인증 실패] {e}")
        return 2

    prev = _get_previous_us_snapshot()

    try:
        snap = take_us_snapshot(token)
    except kis_api.KISAPIError as e:
        print(f"[스냅샷 실패] {e}")
        return 3

    print(f"[{snap['date']}] {config.KIS_ENV} (US close)")
    print(f"  보유 포지션 : {snap['n_positions']}개")
    print(f"  평가 합계   : ${snap['eval_total']:>12,.2f}")
    print(f"  누적 손익   : ${snap['pnl_total']:>+12,.2f}")
    for p in snap["positions"]:
        print(
            f"    {p['symbol']:<6} {p['qty']:>4}주 "
            f"@ ${p['avg_price_usd']:.2f} → ${p['current_price_usd']:.2f}"
        )

    _send_us_closing_report(snap, prev)
    print("\nDB 기록 + Telegram 송신 완료.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
