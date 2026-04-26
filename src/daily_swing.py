"""
일일 스윙 전략 실행.

로직:
  1. 최신 시세 데이터 갱신 (옵션)
  2. 스윙 유니버스의 지표 계산
  3. 현재 스윙 포지션 파악 (DB trades 에서 strategy='swing' 집계)
  4. 각 스윙 포지션에 대해 청산 시그널 체크
  5. 전체 자본의 15% 예산 내에서 신규 진입 시그널 체크
  6. 주문 실행 + Telegram 알림

평일 09:10 KST 자동 실행 (장 시작 후 10분).

실행:
  python -m src.daily_swing              # 드라이런
  python -m src.daily_swing --refresh    # 데이터 갱신 후 드라이런
  python -m src.daily_swing --execute    # 실제 주문 (프롬프트)
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date

import pandas as pd

from . import check_balance
from . import check_price
from . import config
from . import db
from . import indicators
from . import kis_api
from . import kis_auth
from . import load_candles
from . import notify
from . import place_order
from . import swing_strategy as strat

# ── 설정 ────────────────────────────────────────

# 스윙 유니버스 (DM 유니버스와 겹치지 않게 분리)
# 005930 삼성전자, 000660 SK하이닉스, 035420 NAVER — 대형주 기준
SWING_UNIVERSE = ["005930", "000660", "035420"]

SWING_ALLOCATION = 0.15    # 전체 자본의 15% (멀티 전략 새 배분)
MAX_POSITIONS = 3
STRATEGY_TAG = "swing"


def refresh_data() -> None:
    for sym in SWING_UNIVERSE:
        load_candles.load_symbol(sym, "KR", years=2)


def load_indicators_latest() -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    with db.connection() as conn:
        for sym in SWING_UNIVERSE:
            df = pd.read_sql_query(
                """
                SELECT date, open, high, low, close, volume
                FROM daily_candles
                WHERE symbol = ?
                ORDER BY date ASC
                """,
                conn,
                params=(sym,),
                parse_dates=["date"],
                index_col="date",
            )
            if not df.empty:
                result[sym] = indicators.attach_all(df)
    return result


def get_swing_positions_from_db() -> dict[str, dict]:
    """DB trades 에서 strategy='swing' 순 포지션 집계."""
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT symbol,
                   SUM(CASE WHEN side='buy' THEN quantity ELSE -quantity END) AS net_qty,
                   MAX(CASE WHEN side='buy' THEN executed_at END) AS last_buy,
                   AVG(CASE WHEN side='buy' THEN price END) AS avg_buy_price
            FROM trades
            WHERE strategy = ?
              AND env = ?
            GROUP BY symbol
            HAVING net_qty > 0
            """,
            (STRATEGY_TAG, config.KIS_ENV),
        ).fetchall()

    positions: dict[str, dict] = {}
    for r in rows:
        positions[r["symbol"]] = {
            "qty": int(r["net_qty"]),
            "avg_price": float(r["avg_buy_price"] or 0),
            "entry_date": (
                pd.Timestamp(r["last_buy"]) if r["last_buy"] else pd.Timestamp.now()
            ),
        }
    return positions


def log_trade(
    symbol: str, side: str, qty: int, price: int, order_id: str, msg: str
) -> None:
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO trades
                (symbol, side, quantity, price, executed_at, env, strategy,
                 order_id, notes)
            VALUES (?, ?, ?, ?, datetime('now'), ?, ?, ?, ?)
            """,
            (
                symbol,
                side,
                qty,
                price,
                config.KIS_ENV,
                STRATEGY_TAG,
                order_id,
                msg,
            ),
        )


def _send_morning_report(
    total_equity: int,
    swing_budget: int,
    positions: dict[str, dict],
    scan_results: dict[str, str],
    orders: list[tuple[str, str, int, str]],
    execute_results: list[dict] | None = None,
) -> None:
    """
    매일 아침(09:10) 보내는 상태 보고.

    거래 유무와 무관하게 "시스템 살아있음 + 오늘 결정" 알림.
    """
    if not notify.is_enabled():
        return

    today = pd.Timestamp.now().strftime("%-m/%-d (%a)")
    mode = "모의" if config.KIS_ENV == "paper" else "실거래"

    lines = [
        f"환경: {mode}",
        f"총평가: {total_equity:>13,}원",
        f"스윙 예산({SWING_ALLOCATION*100:.0f}%): {swing_budget:>10,}원",
        "",
    ]

    if positions:
        lines.append("[스윙 포지션]")
        for sym, info in positions.items():
            lines.append(f"  {sym}: {info['qty']}주 @ {int(info['avg_price']):,}")
    else:
        lines.append("[스윙 포지션] 없음 (대기)")
    lines.append("")

    lines.append("[오늘 시그널 스캔]")
    for sym, status in scan_results.items():
        lines.append(f"  {sym}: {status}")
    lines.append("")

    if orders and execute_results is not None:
        lines.append("[오늘 거래 결과]")
        for r in execute_results:
            if r["status"] == "OK":
                lines.append(f"  ✓ {r['side']} {r['symbol']} {r['qty']}주")
            else:
                lines.append(f"  ✗ {r['side']} {r['symbol']}: 실패")
    elif orders:
        lines.append("[계획 (드라이런)]")
        for side, sym, qty, _ in orders:
            lines.append(f"  {side} {sym} {qty}주")
    else:
        lines.append("거래 없음 (조건 미충족)")

    msg = f"<b>🌅 장 시작 — {today}</b>\n\n" + "\n".join(lines)
    notify.send(msg)


def confirm_execution() -> bool:
    print("\n" + "!" * 64)
    print("스윙 주문을 실제 전송합니다.")
    print("진행: y / yes,  취소: 아무거나")
    print("!" * 64)
    try:
        ans = input("[y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def main() -> int:
    parser = argparse.ArgumentParser(description="일일 스윙")
    parser.add_argument("--execute", action="store_true", help="실제 주문")
    parser.add_argument("--refresh", action="store_true", help="시세 데이터 갱신")
    parser.add_argument("--yes", action="store_true", help="프롬프트 스킵 (자동용)")
    args = parser.parse_args()

    if args.execute and config.KIS_ENV != "paper":
        print("[차단] 실거래 모드에선 실행 금지. KIS_ENV=paper 확인.")
        return 1

    print("=" * 64)
    print(f"일일 스윙 {'실행' if args.execute else '드라이런'}")
    print("=" * 64)
    print(f"유니버스: {SWING_UNIVERSE} | 배정: {SWING_ALLOCATION*100:.0f}%")

    if args.refresh:
        print("\n[준비] 데이터 갱신 중...")
        try:
            refresh_data()
        except Exception as e:
            print(f"  [갱신 실패] {e}")
            return 2

    # 지표 계산
    symbol_data = load_indicators_latest()
    if not symbol_data:
        print("[실패] 지표 데이터 없음. load_candles 먼저.")
        return 3

    # KIS 연결
    try:
        config.validate()
        token = kis_auth.get_access_token()
    except Exception as e:
        print(f"[KIS 연결 실패] {e}")
        return 4

    # 계좌 잔고
    try:
        balance = check_balance.fetch_balance(token)
    except kis_api.KISAPIError as e:
        print(f"[잔고 조회 실패] {e}")
        return 5

    output2_list = balance.get("output2", [])
    output2 = output2_list[0] if output2_list else {}

    def _to_int(v) -> int:
        try:
            return int(v or 0)
        except (ValueError, TypeError):
            return 0

    total_equity = _to_int(output2.get("tot_evlu_amt"))

    # 스윙 예산
    swing_budget = int(total_equity * SWING_ALLOCATION)
    position_size = swing_budget // MAX_POSITIONS

    print(f"\n총 평가금액    : {total_equity:>14,} 원")
    print(f"스윙 예산 ({SWING_ALLOCATION*100:.0f}%): {swing_budget:>14,} 원")
    print(f"1포지션 목표    : {position_size:>14,} 원")

    # 현재 스윙 포지션
    swing_positions = get_swing_positions_from_db()
    print(f"\n현재 스윙 포지션: {len(swing_positions)}개")
    for sym, info in swing_positions.items():
        print(f"  {sym}: {info['qty']}주 @ 평단 {int(info['avg_price']):,}")

    orders: list[tuple[str, str, int, str]] = []  # (side, symbol, qty, reason)
    scan_results: dict[str, str] = {}  # 종목별 시그널 스캔 결과 (Telegram 보고용)

    # 1. 청산 체크
    print("\n[청산 시그널 체크]")
    current_date = pd.Timestamp(date.today())
    for sym, info in swing_positions.items():
        if sym not in symbol_data:
            print(f"  {sym}: 데이터 없음, 스킵")
            scan_results[sym] = "데이터 없음"
            continue
        df = symbol_data[sym]
        if df.empty:
            continue
        latest = df.iloc[-1]
        pos = strat.Position(
            entry_date=info["entry_date"],
            entry_price=info["avg_price"],
            qty=info["qty"],
        )
        exit_signal = strat.check_exit(latest, pos, current_date)
        if exit_signal.should_exit:
            print(f"  {sym}: 청산 — {exit_signal.reason}")
            orders.append(("SELL", sym, info["qty"], exit_signal.reason))
            scan_results[sym] = f"청산 — {exit_signal.reason}"
        else:
            pnl_pct = (latest["close"] - info["avg_price"]) / info["avg_price"]
            print(f"  {sym}: 유지 (수익률 {pnl_pct*100:+.2f}%)")
            scan_results[sym] = f"보유 유지 ({pnl_pct*100:+.2f}%)"

    # 2. 진입 체크
    print("\n[진입 시그널 체크]")
    projected_positions = len(swing_positions) - sum(1 for o in orders if o[0] == "SELL")

    for sym in SWING_UNIVERSE:
        if sym in swing_positions:
            continue
        if projected_positions >= MAX_POSITIONS:
            print(f"  {sym}: 최대 포지션 도달, 스킵")
            break
        if sym not in symbol_data:
            continue
        df = symbol_data[sym]
        if len(df) < 2:
            continue
        # 어제 종가 기준 시그널 (look-ahead 방지)
        prev_row = df.iloc[-2]
        entry_signal = strat.check_entry(prev_row)
        if entry_signal.valid:
            latest_price = float(df.iloc[-1]["close"])
            qty = int(position_size // latest_price)
            if qty <= 0:
                print(f"  {sym}: 포지션 크기 부족 (현재가 {int(latest_price):,})")
                scan_results[sym] = "포지션 크기 부족"
                continue
            print(f"  {sym}: 진입 — {', '.join(entry_signal.reasons)}")
            print(
                f"    목표 {qty}주 @ ~{int(latest_price):,} "
                f"= ~{qty * int(latest_price):,}원"
            )
            orders.append(("BUY", sym, qty, "스윙 진입"))
            projected_positions += 1
            scan_results[sym] = f"진입 신호 ✓"
        else:
            reason = entry_signal.reasons[0] if entry_signal.reasons else "?"
            print(f"  {sym}: 시그널 없음 ({reason})")
            scan_results[sym] = f"대기 ({reason})"

    # 3. 주문 요약
    if not orders:
        print("\n변경 없음. 종료.")
        # 매일 아침 Telegram 보고 (거래 유무와 무관)
        _send_morning_report(
            total_equity, swing_budget, swing_positions, scan_results, orders
        )
        return 0

    print(f"\n총 {len(orders)}개 주문 계획:")
    for side, sym, qty, reason in orders:
        print(f"  [{side}] {sym} {qty}주  ({reason})")

    if not args.execute:
        print("\n※ DRY RUN - 실제 주문 안 감")
        print("※ 실제 실행: python -m src.daily_swing --execute")
        return 0

    # 4. 확인 후 실행
    if not args.yes:
        if not confirm_execution():
            print("취소됨.")
            return 0

    print("\n" + "=" * 64)
    print("주문 전송 중...")
    print("=" * 64)

    results = []
    for i, (side, sym, qty, reason) in enumerate(orders, 1):
        side_lower = side.lower()
        print(f"\n[{i}/{len(orders)}] {side} {sym} {qty}주...")
        try:
            result = place_order.place_market_order(
                sym, qty, side_lower, token
            )
            out = result.get("output", {})
            odno = out.get("ODNO", "?")
            msg = result.get("msg1", "")
            print(f"  성공. 주문번호 {odno}")

            # 체결가 근사용 시세 (없으면 0)
            price = 0
            try:
                price_data = check_price.fetch_price(sym, token)
                price = _to_int(price_data.get("output", {}).get("stck_prpr"))
            except Exception:
                pass

            log_trade(sym, side_lower, qty, price, odno, msg)
            results.append(
                {
                    "status": "OK",
                    "side": side,
                    "symbol": sym,
                    "qty": qty,
                    "price": price,
                    "odno": odno,
                    "msg": msg,
                }
            )
        except kis_api.KISAPIError as e:
            print(f"  실패: {e}")
            results.append(
                {
                    "status": "ERROR",
                    "side": side,
                    "symbol": sym,
                    "qty": qty,
                    "error": str(e),
                }
            )

        if i < len(orders):
            time.sleep(0.6)

    ok = sum(1 for r in results if r["status"] == "OK")
    fail = len(results) - ok
    print("\n" + "=" * 64)
    print(f"완료: 성공 {ok} / 실패 {fail}")
    print("=" * 64)

    # Telegram 아침 보고 (거래 결과 포함)
    _send_morning_report(
        total_equity, swing_budget, swing_positions, scan_results, orders, results
    )

    return 0 if fail == 0 else 6


if __name__ == "__main__":
    sys.exit(main())
