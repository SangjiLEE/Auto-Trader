"""
일일 계좌 스냅샷.

매일 장 마감 후 실행 권장 (15:45 KST). 계좌 상태를 DB에 기록해서
백테스트 예상 vs 실제 체결 성과 갭 측정에 사용.

실행:
  python -m src.daily_snapshot              # 한 번 기록
  python -m src.daily_snapshot --history    # 최근 기록 출력
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

import pandas as pd

from . import check_balance
from . import check_overseas_balance
from . import config
from . import db
from . import fear_greed
from . import kis_api
from . import kis_auth
from . import notify
from . import realized_pnl

KR_SEED_KRW = 50_000_000  # 실현수익률 분모
KR_STRATEGIES = ["vaa"]   # KR 시장 strategy 목록

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_snapshots (
    date          TEXT    NOT NULL,
    env           TEXT    NOT NULL,
    cash          INTEGER,
    stock_value   INTEGER,
    total_value   INTEGER,
    pnl_amount    INTEGER,
    positions     TEXT,                     -- JSON
    taken_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, env)
);
CREATE INDEX IF NOT EXISTS idx_snap_env_date ON daily_snapshots(env, date);
"""


def _ensure_schema() -> None:
    with db.connection() as conn:
        conn.executescript(_SCHEMA)


def take_snapshot(token: str) -> dict:
    """현재 계좌 상태 → dict + DB 저장."""
    data = check_balance.fetch_balance(token)
    output1 = data.get("output1", [])
    output2_list = data.get("output2", [])
    output2 = output2_list[0] if output2_list else {}

    def _to_int(v) -> int:
        try:
            return int(v or 0)
        except (ValueError, TypeError):
            return 0

    cash = _to_int(output2.get("dnca_tot_amt"))
    stock_value = _to_int(output2.get("scts_evlu_amt"))
    total_value = _to_int(output2.get("tot_evlu_amt"))
    pnl_amount = _to_int(output2.get("evlu_pfls_smtl_amt"))

    positions = []
    for h in output1:
        sym = (h.get("pdno") or "").strip()
        qty = _to_int(h.get("hldg_qty"))
        if not sym or qty <= 0:
            continue
        positions.append(
            {
                "symbol": sym,
                "name": h.get("prdt_name", ""),
                "qty": qty,
                "avg_price": float(h.get("pchs_avg_pric", "0") or 0),
                "current_price": _to_int(h.get("prpr")),
                "eval_amount": _to_int(h.get("evlu_amt")),
                "pnl_amount": _to_int(h.get("evlu_pfls_amt")),
            }
        )

    today = datetime.now().strftime("%Y-%m-%d")

    # 미국 잔고 (있으면 포함, 없거나 실패 시 빈 리스트)
    overseas_positions: list[dict] = []
    try:
        overseas_positions = check_overseas_balance.fetch_all_us_holdings(token)
    except Exception:
        pass  # 해외 조회 실패해도 국내 스냅샷은 진행

    _ensure_schema()
    with db.connection() as conn:
        # JSON 에 국내 + 해외 모두 저장
        all_positions = {
            "kr": positions,
            "us": overseas_positions,
        }
        conn.execute(
            """
            INSERT OR REPLACE INTO daily_snapshots
                (date, env, cash, stock_value, total_value, pnl_amount, positions)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                today,
                config.KIS_ENV,
                cash,
                stock_value,
                total_value,
                pnl_amount,
                json.dumps(all_positions, ensure_ascii=False),
            ),
        )

    return {
        "date": today,
        "env": config.KIS_ENV,
        "cash": cash,
        "stock_value": stock_value,
        "total_value": total_value,
        "pnl_amount": pnl_amount,
        "positions": positions,
        "overseas_positions": overseas_positions,
    }


def show_history(limit: int = 20) -> None:
    _ensure_schema()
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT date, env, cash, stock_value, total_value, pnl_amount
            FROM daily_snapshots
            WHERE env = ?
            ORDER BY date DESC
            LIMIT ?
            """,
            (config.KIS_ENV, limit),
        ).fetchall()

    if not rows:
        print("기록 없음. 먼저 스냅샷 한 번 기록하세요.")
        return

    print(
        f"\n{'날짜':<12} {'현금':>14} {'주식':>14} "
        f"{'총평가':>14} {'누적손익':>14} {'일변화':>10}"
    )
    print("-" * 82)
    prev_total = None
    for r in reversed(rows):  # 오래된 순
        day_change = ""
        if prev_total is not None and prev_total > 0:
            diff = r["total_value"] - prev_total
            diff_pct = diff / prev_total * 100
            day_change = f"{diff_pct:+.2f}%"
        print(
            f"{r['date']:<12} "
            f"{r['cash']:>14,} "
            f"{r['stock_value']:>14,} "
            f"{r['total_value']:>14,} "
            f"{r['pnl_amount']:>+14,} "
            f"{day_change:>10}"
        )
        prev_total = r["total_value"]


def _fmt_won(x: int) -> str:
    return f"{x:,}"


def _fmt_signed(x: int) -> str:
    """부호 붙은 원화 포맷."""
    return f"{x:+,}"


def _get_previous_snapshot() -> dict | None:
    """오늘 이전의 가장 최근 스냅샷 반환."""
    today = pd.Timestamp.now().strftime("%Y-%m-%d")
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT date, total_value, pnl_amount
            FROM daily_snapshots
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
        "total_value": row["total_value"],
        "pnl_amount": row["pnl_amount"],
    }


def _send_evening_report(snap: dict, prev: dict | None) -> None:
    """매일 장 마감 후(15:45) KR 저녁 보고서.

    [채널 분리 원칙] 미국 포지션은 #미장-일일-마감보고서 로 별도 발송 (us_closing_report).
    여기서는 KR 만 노출.
    """
    if not notify.is_enabled():
        return

    today_str = pd.Timestamp.now().strftime("%-m/%-d (%a)")
    mode = "모의" if snap["env"] == "paper" else "실거래"

    # 헤더 블록
    lines = [
        f"*【장 마감 — {today_str} [{mode}]】*",
        f"　총평가: {snap['total_value']:,}원",
        f"　누적손익: {snap['pnl_amount']:+,}원",
    ]

    # 공포·탐욕 (이모지 제거)
    fg = fear_greed.fetch_index()
    fg_value = fg.get("value", "?")
    fg_class = fg.get("classification", "?")
    lines.append(f"　공포·탐욕: {fg_value} ({fg_class})")

    if prev is not None and prev["total_value"] > 0:
        diff = snap["total_value"] - prev["total_value"]
        diff_pct = diff / prev["total_value"] * 100
        lines.append(f"　전일 대비: {diff:+,}원 ({diff_pct:+.2f}%)")
    lines.append("")

    # 국내 포지션
    lines.append("*◾️국내 포지션*")
    if snap["positions"]:
        for p in snap["positions"]:
            pnl_pct = 0
            if p["avg_price"] > 0:
                pnl_pct = (p["current_price"] - p["avg_price"]) / p["avg_price"] * 100
            lines.append(
                f"　{p['symbol']} {p['qty']}주 (수익률 {pnl_pct:+.2f}%)"
            )
    else:
        lines.append("　없음")

    # 전체 실현수익률 (KR strategy 합산)
    realized_map = realized_pnl.realized_for_strategies(KR_STRATEGIES)
    realized_krw = realized_map["krw"]
    realized_pct = realized_pnl.pct(realized_krw, KR_SEED_KRW)

    # 미실현 = total_value - cash - realized 의 단순 추정 대신
    # KR 보유 종목들의 평가손익 합산으로 직접 계산
    unrealized_krw = sum(p.get("pnl_amount", 0) for p in snap["positions"])
    unrealized_pct_v = realized_pnl.pct(unrealized_krw, KR_SEED_KRW)

    lines.append("")
    lines.append("*◾️전체 실현수익률*")
    lines.append(f"　실현 누적: ₩{int(realized_krw):+,} ({realized_pct:+.2f}%)")
    lines.append(f"　미실현: ₩{int(unrealized_krw):+,} ({unrealized_pct_v:+.2f}%)")
    lines.append(f"　(초기 KR 시드 ₩{KR_SEED_KRW:,} 대비)")

    notify.send("\n".join(lines), channel=notify.CHANNEL_KR_DAILY)


def main() -> int:
    parser = argparse.ArgumentParser(description="일일 계좌 스냅샷")
    parser.add_argument(
        "--history",
        action="store_true",
        help="최근 스냅샷 20개 출력 (기록 안 함)",
    )
    parser.add_argument(
        "--limit", type=int, default=20, help="--history 시 출력 개수"
    )
    args = parser.parse_args()

    if args.history:
        show_history(args.limit)
        return 0

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

    # 스냅샷 저장 전에 어제 기록 먼저 조회 (비교용)
    prev_snap = _get_previous_snapshot()

    try:
        snap = take_snapshot(token)
    except kis_api.KISAPIError as e:
        print(f"[스냅샷 실패] {e}")
        return 3

    # Telegram 저녁 보고
    _send_evening_report(snap, prev_snap)

    print(f"[{snap['date']}] {snap['env']}")
    print(f"  현금:       {_fmt_won(snap['cash']):>15} 원")
    print(f"  주식평가:   {_fmt_won(snap['stock_value']):>15} 원")
    print(f"  총평가:     {_fmt_won(snap['total_value']):>15} 원")
    print(f"  평가손익:   {_fmt_signed(snap['pnl_amount']):>15} 원")
    print(f"  국내 종목:  {len(snap['positions'])}개")
    for p in snap["positions"]:
        print(
            f"    {p['symbol']:<8} {p['qty']:>5}주 @ 평단 "
            f"{int(p['avg_price']):>8,} / 현재 {p['current_price']:>8,} "
            f"/ 손익 {p['pnl_amount']:>+12,}"
        )

    overseas = snap.get("overseas_positions") or []
    print(f"  미국 종목:  {len(overseas)}개")
    for p in overseas:
        print(
            f"    {p['symbol']:<8} {p['qty']:>5}주 @ 평단 "
            f"${p['avg_price_usd']:>7,.2f} / 현재 ${p['current_price_usd']:>7,.2f} "
            f"/ 손익 ${p['pnl_usd']:>+10,.2f}"
        )
    print("\nDB 기록 완료. 매일 반복하면 손익 추이 축적.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
