"""
일간 Catalyst-driven 매매 (Phase B-Lite).

[Phase D 결과 + B-Lite 구현]
Phase D 검증: 8종목 catalyst 백테스트 종합 EV +0.187%, Win rate 62.1%
ORCL 폐기, ETF (SOXL/TSLL) 는 자체 실적 없음 → 제외

Universe (실적 있는 종목만):
  NVDA, TSLA, AAPL, IREN, BMNR

룰:
  매일 평가 (평일 23:50 KST):
    1. 보유 catalyst 종목 → +3% 익절 / -3% 손절 / 2일 max 청산
    2. 미보유 종목 중 catalyst window (실적 발표 ±N일) 활성 → 매수
       - days_before=1 (발표 직전)
       - days_after=2 (발표 후 PEAD 효과)
    3. 자본 비중: US 자본의 5% (NVDA v4 와 동일 슬리브 비중)

운영 흐름:
  1. earnings_calendar 캐시 조회 (1일 1회 갱신)
  2. universe 의 catalyst window 활성 종목 목록
  3. 현재 KIS US 보유 + 매수 시점 (entry_date 기록 from DB)
  4. 매도 룰 (보유 종목): +3%/-3%/2일 max
  5. 매수 룰 (미보유 + catalyst active): 동일 weight
  6. B2 fill check (잔고 차분)

실행:
  python -m src.daily_catalyst              # 드라이런
  python -m src.daily_catalyst --refresh    # 데이터 갱신
  python -m src.daily_catalyst --execute --yes  # 자동 실행
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date

import pandas as pd

from . import check_balance
from . import check_overseas_balance
from . import check_overseas_price
from . import config
from . import db
from . import earnings_calendar as ec
from . import kis_api
from . import kis_auth
from . import notify
from . import place_overseas_order

UNIVERSE = ["NVDA", "TSLA", "AAPL", "IREN", "BMNR"]

# Catalyst window: 실적 발표 ±N일
CATALYST_DAYS_BEFORE = 1
CATALYST_DAYS_AFTER = 2

# 매매 룰
PROFIT_PCT = 0.03    # +3% 익절
STOP_PCT = -0.03     # -3% 손절
MAX_HOLDING_DAYS = 2

# 자본 비중 (사용자 결정 — 25% 로 증액, 2026-04-30)
# 이전: 5% (실험적 도입)
# 신규: 25% (Phase D 백테스트 EV +0.187% 확인 후 비중 ↑)
ALLOCATION = 0.25    # KR 잔고 기준 25% (USD 환산 후 catalyst 슬리브)
USD_KRW_ESTIMATE = 1410
ORDER_PRICE_BUFFER = 0.005

STRATEGY_TAG = "catalyst"


def fetch_kr_total_equity(token: str) -> int:
    data = check_balance.fetch_balance(token)
    output2 = (data.get("output2") or [{}])[0]
    try:
        return int(output2.get("tot_evlu_amt") or 0)
    except (ValueError, TypeError):
        return 0


def fetch_us_holdings(token: str) -> dict[str, dict]:
    """KIS US 잔고에서 catalyst universe 만 추출."""
    holdings = check_overseas_balance.fetch_all_us_holdings(token)
    result = {}
    for h in holdings:
        sym = h["symbol"]
        if sym in UNIVERSE:
            result[sym] = h
    return result


def fetch_us_prices(token: str, symbols: list[str]) -> dict[str, float]:
    prices = {}
    for sym in symbols:
        try:
            data = check_overseas_price.fetch_price(sym, token)
            price = float(data.get("output", {}).get("last") or 0)
            if price > 0:
                prices[sym] = price
        except Exception:
            pass
    return prices


def fetch_catalyst_entry(symbol: str) -> tuple[date, float] | None:
    """trades 테이블에서 catalyst 전략의 마지막 매수 (entry) 조회.

    반환: (entry_date, entry_price) 또는 None.
    매도 후 다시 매수면 가장 최신 매수 사이클의 시작점.
    """
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT side, quantity, price, executed_at
            FROM trades
            WHERE strategy = ? AND env = ? AND symbol = ?
            ORDER BY executed_at ASC
            """,
            (STRATEGY_TAG, config.KIS_ENV, symbol),
        ).fetchall()
    if not rows:
        return None
    # 마지막 매도 이후 첫 매수 찾기
    cycle_buy_date = None
    cycle_buy_price = None
    cycle_qty = 0
    for r in rows:
        qty = int(r["quantity"])
        if r["side"] == "buy":
            if cycle_qty == 0:
                # 새 사이클 시작
                cycle_buy_date = r["executed_at"]
                cycle_buy_price = float(r["price"])
            cycle_qty += qty
        else:
            cycle_qty -= qty
            if cycle_qty <= 0:
                cycle_buy_date = None
                cycle_buy_price = None
                cycle_qty = 0
    if cycle_buy_date is None:
        return None
    try:
        d = date.fromisoformat(cycle_buy_date.split("T")[0].split(" ")[0])
        return d, cycle_buy_price
    except (ValueError, AttributeError):
        return None


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


def execute_with_fill_check(
    sym: str, side: str, qty: int, token: str, wait_seconds: float = 6.0,
) -> dict:
    """[B2] 미국 ETF 주문 + 잔고 차분 reconciliation."""
    def query():
        h = check_overseas_balance.fetch_all_us_holdings(token)
        return {x["symbol"]: x["qty"] for x in h}

    try:
        pre = query()
    except Exception:
        pre = None
    pre_qty = pre.get(sym, 0) if pre else 0

    res = place_overseas_order.place_market_like_order(
        sym, qty, side, token, buffer=ORDER_PRICE_BUFFER
    )
    out = res.get("output", {})
    odno = out.get("ODNO", "?")
    msg = res.get("msg1", "")

    time.sleep(wait_seconds)

    filled_qty = qty
    fill_status = "FILLED"
    if pre is not None:
        try:
            post = query()
            delta = post.get(sym, 0) - pre_qty
            actual = delta if side == "buy" else -delta
            filled_qty = max(0, actual)
            if filled_qty == 0:
                fill_status = "REJECTED"
            elif filled_qty < qty:
                fill_status = "PARTIAL"
        except Exception:
            pass

    return {
        "order_id": odno, "msg": msg,
        "requested_qty": qty, "filled_qty": filled_qty,
        "status": fill_status,
    }


def confirm_execution() -> bool:
    print("\n" + "!" * 64)
    print("Catalyst 주문 실제 전송. y/yes 외엔 취소.")
    print("!" * 64)
    try:
        return input("[y/N]: ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _send_report(actions, holdings, calendar, results=None):
    if not notify.is_enabled():
        return
    mode = "모의" if config.KIS_ENV == "paper" else "실"
    today = date.today()
    lines = [
        f"🎯 Catalyst Daily [{mode}]",
        f"날짜: {today.isoformat()}",
        "",
    ]

    # Catalyst window 활성 종목
    active_syms = []
    for sym in UNIVERSE:
        dates = calendar.get(sym, [])
        in_win, near, delta = ec.is_in_catalyst_window(
            dates, today, CATALYST_DAYS_BEFORE, CATALYST_DAYS_AFTER,
        )
        if in_win:
            d_label = "오늘" if delta == 0 else f"{delta:+}일"
            active_syms.append(f"{sym} ({d_label})")

    if active_syms:
        lines.append("[Catalyst Active]")
        lines.extend(f"  {s}" for s in active_syms)
    else:
        lines.append("Catalyst window 없음")

    if holdings:
        lines.append("")
        lines.append("[보유 catalyst 포지션]")
        for sym, h in holdings.items():
            lines.append(f"  {sym}: {h['qty']}주 @ ${h['avg_price_usd']:.2f}")

    if not actions:
        lines.append("")
        lines.append("거래 없음")
    else:
        lines.append("")
        lines.append("[액션]")
        for a in actions:
            lines.append(f"  {a['side']} {a['symbol']} {a['qty']}주 — {a['reason']}")

    if results:
        lines.append("")
        lines.append("[실행 결과]")
        for r in results:
            emoji = "✅" if r.get("status") == "OK" else "❌"
            fill = r.get("fill_status", "")
            lines.append(f"  {emoji} {r['side']} {r['symbol']} {r.get('filled_qty', '?')}주 [{fill}]")

    notify.send("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description="Catalyst 일간 매매")
    parser.add_argument("--refresh", action="store_true", help="실적 캘린더 갱신")
    parser.add_argument("--execute", action="store_true", help="실제 주문")
    parser.add_argument("--yes", action="store_true", help="자동 yes")
    args = parser.parse_args()

    if args.execute and config.KIS_ENV != "paper":
        print(f"[차단] KIS_ENV={config.KIS_ENV} — 실거래 차단.")
        return 3

    today = date.today()
    print("=" * 70)
    print(f"Catalyst Daily — {today.isoformat()}")
    print("=" * 70)

    # 1. 실적 캘린더
    print("\n[1] 실적 캘린더 조회...")
    calendar = ec.fetch_earnings_dates(UNIVERSE, force=args.refresh)
    print(f"  {sum(len(d) for d in calendar.values())}개 일정 확보")

    # 2. KIS 인증 + 잔고
    print("\n[2] KIS 잔고 조회...")
    try:
        token = kis_auth.get_access_token()
    except Exception as e:
        print(f"[KIS 실패] {e}")
        return 4

    total_krw = fetch_kr_total_equity(token)
    sleeve_usd = (total_krw * ALLOCATION) / USD_KRW_ESTIMATE
    print(f"  US 슬리브 자본: ${sleeve_usd:,.2f} (총 {total_krw:,}원의 {ALLOCATION*100:.0f}%)")

    holdings = fetch_us_holdings(token)
    print(f"  현재 catalyst 포지션: {len(holdings)}개")
    for sym, h in holdings.items():
        print(f"    {sym}: {h['qty']}주 @ ${h['avg_price_usd']:.2f} (현재 ${h['current_price_usd']:.2f})")

    # 3. Catalyst window 활성 종목
    print(f"\n[3] Catalyst window (실적 발표 ±{CATALYST_DAYS_BEFORE}/{CATALYST_DAYS_AFTER}일)...")
    active_syms = []
    for sym in UNIVERSE:
        dates = calendar.get(sym, [])
        in_win, near, delta = ec.is_in_catalyst_window(
            dates, today, CATALYST_DAYS_BEFORE, CATALYST_DAYS_AFTER,
        )
        flag = "✅ ACTIVE" if in_win else ""
        if near:
            d_label = "오늘" if delta == 0 else f"{delta:+}일"
            print(f"  {sym}: {near} ({d_label}) {flag}")
        else:
            print(f"  {sym}: 일정 없음")
        if in_win:
            active_syms.append(sym)

    # 4. 가격 조회
    syms_to_price = list(set(active_syms + list(holdings.keys())))
    prices = fetch_us_prices(token, syms_to_price) if syms_to_price else {}

    actions = []

    # 5. 청산 룰 (보유 종목)
    print("\n[4] 청산 평가...")
    for sym, h in holdings.items():
        current = prices.get(sym, h.get("current_price_usd", 0))
        avg = h.get("avg_price_usd", 0)
        if avg <= 0 or current <= 0:
            continue
        pnl_pct = (current - avg) / avg

        # entry_date 조회 → catalyst 가 매수한 포지션인지 확인
        entry_info = fetch_catalyst_entry(sym)
        if entry_info is None:
            # 다른 strategy (예: swing_v3) 의 포지션 → catalyst 가 건드리지 않음
            print(f"  {sym}: 다른 strategy 포지션 (catalyst 매수 X) → 청산 스킵")
            continue
        entry_date, _ = entry_info
        days_held = (today - entry_date).days

        reason = None
        if pnl_pct >= PROFIT_PCT:
            reason = f"+{pnl_pct*100:.2f}% 익절"
        elif pnl_pct <= STOP_PCT:
            reason = f"{pnl_pct*100:.2f}% 손절"
        elif days_held >= MAX_HOLDING_DAYS:
            reason = f"{days_held}일 max 보유 시간만료 ({pnl_pct*100:+.2f}%)"

        if reason:
            actions.append({
                "side": "SELL", "symbol": sym, "qty": h["qty"], "reason": reason,
            })
            print(f"  {sym}: SELL {h['qty']}주 — {reason}")
        else:
            print(f"  {sym}: 유지 ({pnl_pct*100:+.2f}%, {days_held}일 보유)")

    # 6. 매수 룰 (catalyst active + 미보유)
    print("\n[5] 매수 평가...")
    new_buys = [s for s in active_syms if s not in holdings]
    if new_buys and prices:
        capital_per_pos = sleeve_usd / len(new_buys)  # 동일 weight
        for sym in new_buys:
            price = prices.get(sym, 0)
            if price <= 0:
                continue
            qty = int(capital_per_pos / price)
            if qty <= 0:
                print(f"  {sym}: 자본 부족 (${capital_per_pos:.2f} / ${price:.2f} = 0주)")
                continue
            actions.append({
                "side": "BUY", "symbol": sym, "qty": qty,
                "reason": f"catalyst window 진입 (~${qty*price:.2f})",
            })
            print(f"  {sym}: BUY {qty}주 @ ~${price:.2f} = ~${qty*price:.2f}")
    else:
        print("  catalyst window 활성 + 미보유 종목 없음")

    # 7. 실행
    if not actions:
        print("\n변경 없음.")
        _send_report(actions, holdings, calendar)
        return 0

    print(f"\n총 {len(actions)}개 액션:")
    for a in actions:
        print(f"  [{a['side']}] {a['symbol']} {a['qty']}주 — {a['reason']}")

    if not args.execute:
        print("\n※ DRY RUN")
        _send_report(actions, holdings, calendar)
        return 0

    if not args.yes and not confirm_execution():
        print("취소됨.")
        return 0

    print("\n주문 전송 중... (B2 fill check)")
    results = []
    for i, a in enumerate(actions, 1):
        side_lower = a["side"].lower()
        sym = a["symbol"]
        qty = a["qty"]
        print(f"\n[{i}/{len(actions)}] {a['side']} {sym} {qty}주...")
        try:
            fill = execute_with_fill_check(sym, side_lower, qty, token)
            odno = fill["order_id"]
            filled = fill["filled_qty"]
            status = fill["status"]
            msg = fill["msg"]
            price = prices.get(sym, 0)
            if status == "REJECTED":
                print(f"  ❌ REJECTED {odno}")
                results.append({**a, "status": "ERROR", "filled_qty": 0,
                                "fill_status": "REJECTED", "error": msg})
            else:
                print(f"  {'⚠️' if status == 'PARTIAL' else '✅'} {status} {filled}/{qty}주 @ ${price:.2f} ({odno})")
                log_trade(sym, side_lower, filled, price, odno,
                          f"catalyst {status} | {a['reason']} | {msg}")
                results.append({**a, "status": "OK", "filled_qty": filled,
                                "fill_status": status, "price": price})
        except (kis_api.KISAPIError, RuntimeError) as e:
            print(f"  ❌ API 오류: {e}")
            results.append({**a, "status": "ERROR", "error": str(e)})
        if i < len(actions):
            time.sleep(0.5)

    ok = sum(1 for r in results if r["status"] == "OK")
    print(f"\n완료: 성공 {ok} / 실패 {len(results)-ok}")
    _send_report(actions, holdings, calendar, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
