"""
미국 빠른 스윙 (1~2일 보유) 자동 실행.

  - 유니버스: SPY, QQQ, AAPL, NVDA, TSLA
  - 자본 슬롯: 전체의 5%
  - 전략: swing_strategy_fast (4 AND 진입, 5 OR + ATR 손절 청산)
  - DB 태그: 'day_us'
  - 스케줄: 평일 23:30 KST (DST 기준 장 시작 후 1시간, 표준시 5분 전)

실행:
  python -m src.daily_swing_fast_us           # 드라이런
  python -m src.daily_swing_fast_us --refresh
  python -m src.daily_swing_fast_us --execute
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date

import pandas as pd

from . import check_balance
from . import check_overseas_price
from . import config
from . import db
from . import indicators
from . import kis_api
from . import kis_auth
from . import load_candles
from . import notify
from . import place_overseas_order
from . import swing_strategy_fast as fast

SWING_UNIVERSE = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA"]
SWING_ALLOCATION = 0.05
MAX_POSITIONS = 1
STRATEGY_TAG = "day_us"
USD_KRW_ESTIMATE = 1400
ORDER_PRICE_BUFFER = 0.005


def refresh_data() -> None:
    for sym in SWING_UNIVERSE:
        load_candles.load_symbol(sym, "US", years=2)


def load_indicators_latest() -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    with db.connection() as conn:
        for sym in SWING_UNIVERSE:
            df = pd.read_sql_query(
                "SELECT date, open, high, low, close, volume FROM daily_candles "
                "WHERE symbol = ? ORDER BY date ASC",
                conn, params=(sym,), parse_dates=["date"], index_col="date",
            )
            if not df.empty:
                result[sym] = indicators.attach_all(df)
    return result


def get_positions_from_db() -> dict[str, dict]:
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT symbol,
                   SUM(CASE WHEN side='buy' THEN quantity ELSE -quantity END) AS net_qty,
                   MAX(CASE WHEN side='buy' THEN executed_at END) AS last_buy,
                   AVG(CASE WHEN side='buy' THEN price END) AS avg_buy_price
            FROM trades
            WHERE strategy = ? AND env = ?
            GROUP BY symbol
            HAVING net_qty > 0
            """,
            (STRATEGY_TAG, config.KIS_ENV),
        ).fetchall()
    positions = {}
    for r in rows:
        positions[r["symbol"]] = {
            "qty": int(r["net_qty"]),
            "avg_price": float(r["avg_buy_price"] or 0),
            "entry_date": (
                pd.Timestamp(r["last_buy"]) if r["last_buy"] else pd.Timestamp.now()
            ),
        }
    return positions


def get_initial_atr(symbol_data: pd.DataFrame, entry_date: pd.Timestamp) -> float:
    try:
        if entry_date in symbol_data.index:
            v = symbol_data.loc[entry_date, "atr14"]
            return float(v) if pd.notna(v) else 0.0
        before = symbol_data.loc[symbol_data.index <= entry_date]
        if len(before) > 0:
            v = before.iloc[-1]["atr14"]
            return float(v) if pd.notna(v) else 0.0
    except Exception:
        pass
    return 0.0


def log_trade(symbol, side, qty, price_usd, order_id, msg):
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO trades
                (symbol, side, quantity, price, executed_at, env, strategy, order_id, notes)
            VALUES (?, ?, ?, ?, datetime('now'), ?, ?, ?, ?)
            """,
            (symbol, side, qty, price_usd, config.KIS_ENV, STRATEGY_TAG, order_id, msg),
        )


def confirm_execution() -> bool:
    print("\n" + "!" * 64)
    print("US 빠른 스윙 주문 실제 전송. 진행: y")
    print("!" * 64)
    try:
        return input("[y/N]: ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _send_report(
    swing_budget_usd, positions, scan_results, orders, results=None
):
    if not notify.is_enabled():
        return
    today = pd.Timestamp.now().strftime("%-m/%-d (%a) %H:%M KST")
    mode = "모의" if config.KIS_ENV == "paper" else "실거래"
    lines = [
        f"환경: {mode}",
        f"슬롯 예산(5%): ${swing_budget_usd:,.2f}",
        "",
    ]
    if positions:
        lines.append("[US 단타 포지션]")
        for sym, info in positions.items():
            lines.append(f"  {sym}: {info['qty']}주 @ ${info['avg_price']:,.2f}")
    else:
        lines.append("[US 단타] 보유 없음 (대기)")
    lines.append("")
    lines.append("[시그널 스캔]")
    for sym, status in scan_results.items():
        lines.append(f"  {sym}: {status}")
    lines.append("")
    if orders and results is not None:
        lines.append("[거래 결과]")
        for r in results:
            if r["status"] == "OK":
                lines.append(f"  ✓ {r['side']} {r['symbol']} {r['qty']}주")
            else:
                lines.append(f"  ✗ {r['side']} {r['symbol']}: 실패")
    elif orders:
        lines.append("[계획(드라이런)]")
        for s, sym, q, _ in orders:
            lines.append(f"  {s} {sym} {q}주")
    else:
        lines.append("거래 없음")
    notify.send(f"<b>⚡ US 단타 — {today}</b>\n\n" + "\n".join(lines))


def _to_int(v):
    try:
        return int(v or 0)
    except (ValueError, TypeError):
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="US 빠른 스윙")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    if args.execute and config.KIS_ENV != "paper":
        print("[차단] 실거래 모드. KIS_ENV=paper 확인.")
        return 1

    print("=" * 64)
    print(f"US 빠른 스윙 {'실행' if args.execute else '드라이런'}")
    print("=" * 64)
    print(f"유니버스: {SWING_UNIVERSE} | 배정 5%")

    if args.refresh:
        print("\n[준비] 데이터 갱신...")
        refresh_data()

    symbol_data = load_indicators_latest()
    if not symbol_data:
        print("[실패] 지표 데이터 없음. --refresh 사용.")
        return 3

    try:
        config.validate()
        token = kis_auth.get_access_token()
    except Exception as e:
        print(f"[KIS 실패] {e}")
        return 4

    try:
        balance = check_balance.fetch_balance(token)
    except kis_api.KISAPIError as e:
        print(f"[잔고 실패] {e}")
        return 5

    output2 = (balance.get("output2") or [{}])[0]
    total_krw = _to_int(output2.get("tot_evlu_amt"))
    swing_budget_krw = total_krw * SWING_ALLOCATION
    swing_budget_usd = swing_budget_krw / USD_KRW_ESTIMATE
    print(f"\n총평가 KRW: {total_krw:,}원, 슬롯 예산: ${swing_budget_usd:,.2f}")

    positions = get_positions_from_db()
    print(f"현재 US 단타 포지션: {len(positions)}개")
    for sym, info in positions.items():
        print(f"  {sym}: {info['qty']}주 @ ${info['avg_price']:,.2f}")

    orders = []
    scan_results = {}
    current_date = pd.Timestamp(date.today())

    print("\n[청산 체크]")
    for sym, info in positions.items():
        if sym not in symbol_data:
            scan_results[sym] = "데이터 없음"
            continue
        df = symbol_data[sym]
        latest = df.iloc[-1]
        atr_init = get_initial_atr(df, info["entry_date"])
        pos = fast.Position(
            entry_date=info["entry_date"], entry_price=info["avg_price"],
            qty=info["qty"], initial_atr=atr_init,
        )
        ex = fast.check_exit(latest, pos, current_date)
        if ex.should_exit:
            print(f"  {sym}: 청산 — {ex.reason}")
            orders.append(("SELL", sym, info["qty"], ex.reason))
            scan_results[sym] = f"청산 — {ex.reason}"
        else:
            pnl = (latest["close"] - info["avg_price"]) / info["avg_price"]
            print(f"  {sym}: 유지 ({pnl*100:+.2f}%)")
            scan_results[sym] = f"보유 ({pnl*100:+.2f}%)"

    print("\n[진입 체크]")
    projected = len(positions) - sum(1 for o in orders if o[0] == "SELL")
    for sym in SWING_UNIVERSE:
        if sym in positions:
            continue
        if projected >= MAX_POSITIONS:
            print(f"  {sym}: 최대 포지션 도달, 스킵")
            scan_results[sym] = "최대 포지션"
            break
        if sym not in symbol_data:
            scan_results[sym] = "데이터 없음"
            continue
        df = symbol_data[sym]
        if len(df) < 2:
            continue
        sig = fast.check_entry(df.iloc[-2])
        if sig.valid:
            latest_close = float(df.iloc[-1]["close"])
            qty = int(swing_budget_usd // latest_close)
            if qty <= 0:
                print(f"  {sym}: 크기 부족 (현재가 ${latest_close:.2f})")
                scan_results[sym] = "크기 부족"
                continue
            print(f"  {sym}: 진입 — {', '.join(sig.reasons)}")
            print(f"    {qty}주 @ ${latest_close:.2f} = ${qty*latest_close:.2f}")
            orders.append(("BUY", sym, qty, "단타 진입"))
            projected += 1
            scan_results[sym] = "진입 신호 ✓"
        else:
            r = sig.reasons[0] if sig.reasons else "?"
            print(f"  {sym}: 시그널 없음 ({r})")
            scan_results[sym] = f"대기 ({r})"

    if not orders:
        print("\n변경 없음.")
        _send_report(swing_budget_usd, positions, scan_results, orders)
        return 0

    print(f"\n총 {len(orders)}개 주문:")
    for s, sym, q, r in orders:
        print(f"  [{s}] {sym} {q}주 ({r})")

    if not args.execute:
        print("\n※ DRY RUN")
        _send_report(swing_budget_usd, positions, scan_results, orders)
        return 0

    if not args.yes and not confirm_execution():
        print("취소됨.")
        return 0

    print("\n주문 전송 중...")
    results = []
    for i, (side, sym, qty, _) in enumerate(orders, 1):
        side_lower = side.lower()
        print(f"\n[{i}/{len(orders)}] {side} {sym} {qty}주...")
        try:
            res = place_overseas_order.place_market_like_order(
                sym, qty, side_lower, token, buffer=ORDER_PRICE_BUFFER
            )
            out = res.get("output", {})
            odno = out.get("ODNO", "?")
            msg = res.get("msg1", "")
            print(f"  성공. {odno}")
            price_usd = 0.0
            try:
                p = check_overseas_price.fetch_price(sym, token)
                price_usd = float(p.get("output", {}).get("last") or 0)
            except Exception:
                pass
            log_trade(sym, side_lower, qty, price_usd, odno, msg)
            results.append({
                "status": "OK", "side": side, "symbol": sym, "qty": qty, "price": price_usd
            })
        except (kis_api.KISAPIError, RuntimeError) as e:
            print(f"  실패: {e}")
            results.append({
                "status": "ERROR", "side": side, "symbol": sym, "error": str(e)
            })
        if i < len(orders):
            time.sleep(0.5)

    ok = sum(1 for r in results if r["status"] == "OK")
    print(f"\n완료: 성공 {ok} / 실패 {len(results)-ok}")
    _send_report(swing_budget_usd, positions, scan_results, orders, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
