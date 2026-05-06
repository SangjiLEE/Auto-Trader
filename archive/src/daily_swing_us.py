"""
미국주식 일일 스윙 전략 자동 실행.

KR 스윙(daily_swing.py)과 동일한 로직, 미국 시장용:
  - 유니버스: SPY, QQQ, VTI, AAPL, NVDA, TSLA
  - 가격: USD
  - 주문: KIS 해외주식 API (지정가 + 0.5% 버퍼 = 시장가 효과)
  - 잔고: 3개 거래소(NASD/NYSE/AMEX) 합산
  - 스윙 자본 슬롯: 전체 평가의 15%
  - DB 태그: 'swing_us'

평일 23:45 KST 자동 실행 (DST/표준시 모두 정규장 시간 내).

실행:
  python -m src.daily_swing_us              # 드라이런
  python -m src.daily_swing_us --refresh    # 데이터 갱신 후 드라이런
  python -m src.daily_swing_us --execute    # 실제 주문
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date

import pandas as pd

from . import check_overseas_balance
from . import check_overseas_price
from . import config
from . import db
from . import indicators
from . import kis_api
from . import kis_auth
from . import kis_overseas
from . import load_candles
from . import notify
from . import place_overseas_order
from . import swing_strategy as strat

# ── 설정 ────────────────────────────────────────

SWING_UNIVERSE = ["SPY", "QQQ", "VTI", "AAPL", "NVDA", "TSLA"]

SWING_ALLOCATION = 0.15    # 전체 자본의 15% 를 미국 스윙에 배정
MAX_POSITIONS = 3
STRATEGY_TAG = "swing_us"

# 주문 가격 버퍼 (시장가 효과를 위한 limit 가격 가산)
ORDER_PRICE_BUFFER = 0.005


def refresh_data() -> None:
    """FDR 로 최신 시세 갱신."""
    for sym in SWING_UNIVERSE:
        load_candles.load_symbol(sym, "US", years=2)


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
    """DB trades에서 strategy='swing_us' 순 포지션 집계."""
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
    symbol: str, side: str, qty: int, price_usd: float, order_id: str, msg: str
) -> None:
    """USD 가격을 그대로 trades.price 에 저장 (price 컬럼은 REAL 이라 OK)."""
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
                price_usd,
                config.KIS_ENV,
                STRATEGY_TAG,
                order_id,
                msg,
            ),
        )


def confirm_execution() -> bool:
    print("\n" + "!" * 64)
    print("미국 스윙 주문을 실제 전송합니다.")
    print("진행: y / yes,  취소: 아무거나")
    print("!" * 64)
    try:
        ans = input("[y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def _send_morning_report(
    swing_budget_usd: float,
    positions: dict[str, dict],
    scan_results: dict[str, str],
    orders: list[tuple[str, str, int, str]],
    execute_results: list[dict] | None = None,
) -> None:
    """미국 장 시작 후 보내는 상태 보고 (한국시간 23:45 무렵)."""
    if not notify.is_enabled():
        return

    today = pd.Timestamp.now().strftime("%-m/%-d (%a) %H:%M KST")
    mode = "모의" if config.KIS_ENV == "paper" else "실거래"

    lines = [
        f"환경: {mode}",
        f"미국 스윙 예산: ${swing_budget_usd:,.2f}",
        "",
    ]

    if positions:
        lines.append("[미국 스윙 포지션]")
        for sym, info in positions.items():
            lines.append(f"  {sym}: {info['qty']}주 @ ${info['avg_price']:,.2f}")
    else:
        lines.append("[미국 스윙 포지션] 없음 (대기)")
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

    msg = f"<b>🇺🇸 미국장 — {today}</b>\n\n" + "\n".join(lines)
    notify.send(msg)


def _to_int(v) -> int:
    try:
        return int(v or 0)
    except (ValueError, TypeError):
        return 0


def _to_float(v) -> float:
    try:
        return float(v or 0)
    except (ValueError, TypeError):
        return 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description="미국 일일 스윙")
    parser.add_argument("--execute", action="store_true", help="실제 주문")
    parser.add_argument("--refresh", action="store_true", help="시세 데이터 갱신")
    parser.add_argument("--yes", action="store_true", help="프롬프트 스킵 (자동용)")
    args = parser.parse_args()

    if args.execute and config.KIS_ENV != "paper":
        print("[차단] 실거래 모드. KIS_ENV=paper 확인.")
        return 1

    print("=" * 64)
    print(f"미국 일일 스윙 {'실행' if args.execute else '드라이런'}")
    print("=" * 64)
    print(f"유니버스: {SWING_UNIVERSE} | 배정: {SWING_ALLOCATION*100:.0f}%")

    if args.refresh:
        print("\n[준비] FDR 로 미국 데이터 갱신 중...")
        try:
            refresh_data()
        except Exception as e:
            print(f"  [갱신 실패] {e}")
            return 2

    # 지표 로드
    symbol_data = load_indicators_latest()
    if not symbol_data:
        print("[실패] 미국 지표 데이터 없음.")
        print("먼저: python -m src.daily_swing_us --refresh")
        return 3

    # KIS 연결
    try:
        config.validate()
        token = kis_auth.get_access_token()
    except Exception as e:
        print(f"[KIS 연결 실패] {e}")
        return 4

    # 한국 계좌 잔고에서 총 평가금액 (KRW) 가져오기
    # KRW 평가의 15% × 추정환율 = USD 예산
    # 간단화: 모의계좌 USD 잔고 직접 활용 + 환율 추정
    from . import check_balance
    try:
        kr_balance = check_balance.fetch_balance(token)
        output2_list = kr_balance.get("output2", [])
        output2 = output2_list[0] if output2_list else {}
        total_krw = _to_int(output2.get("tot_evlu_amt"))
    except kis_api.KISAPIError as e:
        print(f"[KR 잔고 실패] {e}")
        return 5

    # USD 환율 추정 (1USD ≈ 1400KRW, 변동 가능). 실제 매매에선 KIS 환율 적용됨.
    USD_KRW_ESTIMATE = 1400
    swing_budget_krw = total_krw * SWING_ALLOCATION
    swing_budget_usd = swing_budget_krw / USD_KRW_ESTIMATE
    position_size_usd = swing_budget_usd / MAX_POSITIONS

    print(f"\n총 평가 KRW    : {total_krw:>14,} 원")
    print(f"미국 예산 (15%) : ₩{swing_budget_krw:>14,.0f} ≈ ${swing_budget_usd:>10,.2f}")
    print(f"1포지션 목표    : ${position_size_usd:>10,.2f}")

    # 현재 미국 스윙 포지션
    swing_positions = get_swing_positions_from_db()
    print(f"\n현재 미국 스윙 포지션: {len(swing_positions)}개")
    for sym, info in swing_positions.items():
        print(f"  {sym}: {info['qty']}주 @ ${info['avg_price']:,.2f}")

    orders: list[tuple[str, str, int, str]] = []
    scan_results: dict[str, str] = {}

    # 1. 청산 체크
    print("\n[청산 시그널 체크]")
    current_date = pd.Timestamp(date.today())
    for sym, info in swing_positions.items():
        if sym not in symbol_data:
            print(f"  {sym}: 데이터 없음, 스킵")
            scan_results[sym] = "데이터 없음"
            continue
        df = symbol_data[sym]
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
            scan_results[sym] = f"보유 ({pnl_pct*100:+.2f}%)"

    # 2. 진입 체크
    print("\n[진입 시그널 체크]")
    projected = len(swing_positions) - sum(1 for o in orders if o[0] == "SELL")

    for sym in SWING_UNIVERSE:
        if sym in swing_positions:
            continue
        if projected >= MAX_POSITIONS:
            print(f"  {sym}: 최대 포지션 도달, 스킵")
            scan_results[sym] = "최대 포지션 도달"
            break
        if sym not in symbol_data:
            scan_results[sym] = "데이터 없음"
            continue
        df = symbol_data[sym]
        if len(df) < 2:
            continue
        prev_row = df.iloc[-2]
        entry_signal = strat.check_entry(prev_row)
        if entry_signal.valid:
            latest_close = float(df.iloc[-1]["close"])
            qty = int(position_size_usd // latest_close)
            if qty <= 0:
                print(f"  {sym}: 포지션 크기 부족 (현재가 ${latest_close:.2f})")
                scan_results[sym] = "포지션 크기 부족"
                continue
            print(f"  {sym}: 진입 — {', '.join(entry_signal.reasons)}")
            print(f"    목표 {qty}주 @ ~${latest_close:.2f} = ~${qty*latest_close:.2f}")
            orders.append(("BUY", sym, qty, "스윙 진입"))
            projected += 1
            scan_results[sym] = "진입 신호 ✓"
        else:
            reason = entry_signal.reasons[0] if entry_signal.reasons else "?"
            print(f"  {sym}: 시그널 없음 ({reason})")
            scan_results[sym] = f"대기 ({reason})"

    # 3. 주문 요약
    if not orders:
        print("\n변경 없음.")
        _send_morning_report(swing_budget_usd, swing_positions, scan_results, orders)
        return 0

    print(f"\n총 {len(orders)}개 주문:")
    for side, sym, qty, reason in orders:
        print(f"  [{side}] {sym} {qty}주  ({reason})")

    if not args.execute:
        print("\n※ DRY RUN")
        _send_morning_report(swing_budget_usd, swing_positions, scan_results, orders)
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
            result = place_overseas_order.place_market_like_order(
                sym, qty, side_lower, token, buffer=ORDER_PRICE_BUFFER
            )
            out = result.get("output", {})
            odno = out.get("ODNO", "?")
            msg = result.get("msg1", "")
            print(f"  성공. 주문번호 {odno}")

            # 체결가 추정 (현재가 사용)
            price_usd = 0.0
            try:
                p = check_overseas_price.fetch_price(sym, token)
                price_usd = _to_float(p.get("output", {}).get("last"))
            except Exception:
                pass

            log_trade(sym, side_lower, qty, price_usd, odno, msg)
            results.append({
                "status": "OK", "side": side, "symbol": sym,
                "qty": qty, "price": price_usd, "odno": odno, "msg": msg,
            })
        except (kis_api.KISAPIError, RuntimeError) as e:
            print(f"  실패: {e}")
            results.append({
                "status": "ERROR", "side": side, "symbol": sym,
                "qty": qty, "error": str(e),
            })

        if i < len(orders):
            time.sleep(0.6)

    ok = sum(1 for r in results if r["status"] == "OK")
    fail = len(results) - ok
    print(f"\n완료: 성공 {ok} / 실패 {fail}")

    _send_morning_report(
        swing_budget_usd, swing_positions, scan_results, orders, results
    )
    return 0 if fail == 0 else 6


if __name__ == "__main__":
    sys.exit(main())
