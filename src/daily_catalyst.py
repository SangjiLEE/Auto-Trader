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
from . import realized_pnl

# [#2 개선] universe 확장 (5 → 11종목, 거래 빈도 ↑)
UNIVERSE = [
    "NVDA", "TSLA", "AAPL",
    "IREN", "BMNR",                              # BTC 마이너
    "META", "AMZN", "GOOGL", "MSFT", "TSM",      # Big Tech
    "COIN",                                      # 암호화폐
]

# Catalyst window: 실적 발표 ±N일
CATALYST_DAYS_BEFORE = 1
CATALYST_DAYS_AFTER = 2

# [#3 개선] 동적 익절 (trailing stop) — 사용자 원래 룰 복원
# 이전: +3% 즉시 익절 (단순화)
# 신규: +3% 도달 → trailing 활성 → peak -1% 또는 +10% 도달 시 청산
PROFIT_TRIGGER = 0.03    # +3% 도달 시 trailing 활성화
TRAIL_DRAWDOWN = 0.01    # peak 부터 -1% drawdown 시 청산
PROFIT_CAP = 0.10        # +10% 도달 시 무조건 청산 (수익 확보)
STOP_PCT = -0.03         # -3% 손절 (trailing 비활성 상태)
MAX_HOLDING_DAYS = 2

# [#7 개선] 재진입 쿨다운
REENTRY_COOLDOWN_DAYS = 5

# 자본 비중
ALLOCATION = 0.25
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


def fetch_catalyst_entry(symbol: str) -> dict | None:
    """[#6 개선] catalyst 자기 매수의 net 보유 정보.

    반환: {entry_date, avg_price, qty, peak_price} 또는 None.
    - 다른 strategy (swing_v3) 의 NVDA 보유와 분리
    - 청산 매도 수량 = 자기 net qty (KIS 잔고 합산 평단 무관)
    - peak_price = 매수 후 최고 가격 (trailing 계산용, position_states 에서 별도 추적)
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

    cycle_qty = 0
    cycle_total_buy_value = 0.0
    cycle_total_buy_qty = 0
    cycle_buy_date = None

    for r in rows:
        qty = int(r["quantity"])
        if r["side"] == "buy":
            if cycle_qty == 0:
                cycle_buy_date = r["executed_at"]
                cycle_total_buy_value = 0.0
                cycle_total_buy_qty = 0
            cycle_qty += qty
            cycle_total_buy_qty += qty
            cycle_total_buy_value += qty * float(r["price"])
        else:
            cycle_qty -= qty
            if cycle_qty <= 0:
                cycle_qty = 0
                cycle_total_buy_qty = 0
                cycle_total_buy_value = 0.0
                cycle_buy_date = None

    if cycle_qty <= 0 or cycle_buy_date is None:
        return None

    avg_price = (
        cycle_total_buy_value / cycle_total_buy_qty
        if cycle_total_buy_qty > 0 else 0
    )
    try:
        d = date.fromisoformat(cycle_buy_date.split("T")[0].split(" ")[0])
    except (ValueError, AttributeError):
        return None

    return {
        "entry_date": d,
        "avg_price": avg_price,
        "qty": cycle_qty,
    }


def fetch_last_sell_date(symbol: str) -> date | None:
    """[#7 개선] 재진입 쿨다운 — catalyst 마지막 매도일."""
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT executed_at FROM trades
            WHERE strategy = ? AND env = ? AND symbol = ? AND side = 'sell'
            ORDER BY id DESC LIMIT 1
            """,
            (STRATEGY_TAG, config.KIS_ENV, symbol),
        ).fetchone()
    if not row:
        return None
    try:
        return date.fromisoformat(row["executed_at"].split("T")[0].split(" ")[0])
    except (ValueError, AttributeError):
        return None


def can_reenter(symbol: str, today: date | None = None) -> bool:
    """[#7 개선] 재진입 가능 여부 — 마지막 매도 후 N거래일 경과."""
    if today is None:
        today = date.today()
    last = fetch_last_sell_date(symbol)
    if last is None:
        return True
    return (today - last).days >= REENTRY_COOLDOWN_DAYS


def load_catalyst_state(symbol: str) -> dict:
    """[#3 개선] position_states 에서 catalyst trailing 상태 로드."""
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT trailing_active, peak_price
            FROM position_states
            WHERE symbol = ? AND strategy = ? AND env = ?
            """,
            (symbol, STRATEGY_TAG, config.KIS_ENV),
        ).fetchone()
    if row is None:
        return {"trailing_active": False, "peak_price": 0.0}
    return {
        "trailing_active": bool(row["trailing_active"]),
        "peak_price": float(row["peak_price"] or 0),
    }


def save_catalyst_state(symbol: str, trailing_active: bool, peak_price: float) -> None:
    """[#3 개선] catalyst trailing 상태 영속화."""
    with db.connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO position_states
                (symbol, strategy, env, trailing_active, peak_price,
                 pf_t1_done, pf_t2_done, dca_done, entry_regime, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, 0, 0, 'CATALYST', datetime('now'))
            """,
            (
                symbol, STRATEGY_TAG, config.KIS_ENV,
                int(trailing_active), float(peak_price),
            ),
        )


def clear_catalyst_state(symbol: str) -> None:
    """[#3 개선] 청산 시 state 삭제."""
    with db.connection() as conn:
        conn.execute(
            """
            DELETE FROM position_states
            WHERE symbol = ? AND strategy = ? AND env = ?
            """,
            (symbol, STRATEGY_TAG, config.KIS_ENV),
        )


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


def _send_report(actions, holdings, calendar, results=None, sleeve_usd=0.0, prices=None):
    if not notify.is_enabled():
        return
    mode = "모의" if config.KIS_ENV == "paper" else "실"
    today = date.today()

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

    n_signals = len(actions)
    lines = [
        f"【실적 모멘텀 (Catalyst) — {today.isoformat()} [{mode}]】",
        f"　스캔: {len(UNIVERSE)}종목 / 신호 {n_signals}건",
        "",
    ]

    # Catalyst window 활성
    lines.append("◾️Catalyst Active")
    if active_syms:
        for s in active_syms:
            lines.append(f"　{s}")
    else:
        lines.append("　window 없음")
    lines.append("")

    # 보유 포지션
    lines.append("◾️보유 catalyst 포지션")
    if holdings:
        for sym, h in holdings.items():
            lines.append(f"　{sym}: {h['qty']:,}주 @ ${h['avg_price_usd']:,.2f}")
    else:
        lines.append("　없음")
    lines.append("")

    # 액션 / 실행 결과
    if results:
        lines.append("◾️실행 결과")
        for r in results:
            emoji = "✅" if r.get("status") == "OK" else "❌"
            fill = r.get("fill_status", "")
            lines.append(
                f"　{emoji} {r['side']} {r['symbol']} {r.get('filled_qty', '?')}주 [{fill}] — {r.get('reason', '')}"
            )
    elif actions:
        lines.append("◾️계획 (드라이런)")
        for a in actions:
            lines.append(f"　{a['side']} {a['symbol']} {a['qty']}주 — {a['reason']}")
    else:
        lines.append("◾️실행 결과")
        lines.append("　거래 없음")

    # 전체 실현수익률 + 미실현
    realized, _cur = realized_pnl.realized_for_strategy(STRATEGY_TAG)
    pct = realized_pnl.pct(realized, sleeve_usd) if sleeve_usd > 0 else 0.0

    unrealized = 0.0
    if holdings and prices:
        for sym, h in holdings.items():
            cur = prices.get(sym, h.get("current_price_usd", 0))
            avg = h.get("catalyst_avg") or h.get("avg_price_usd", 0)
            qty = h.get("catalyst_qty") or h.get("qty", 0)
            if cur > 0 and avg > 0:
                unrealized += (cur - avg) * qty
    unr_pct = realized_pnl.pct(unrealized, sleeve_usd) if sleeve_usd > 0 else 0.0

    lines.append("")
    lines.append("◾️전체 실현수익률")
    lines.append(f"　Catalyst 누적 실현: ${realized:+,.2f} ({pct:+.2f}%)")
    lines.append(f"　미실현: ${unrealized:+,.2f} ({unr_pct:+.2f}%)")
    lines.append(f"　(US 슬리브 ≈ ${sleeve_usd:,.0f} 대비)")

    notify.send("\n".join(lines), channel=notify.CHANNEL_US_REALTIME)


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

    raw_holdings = fetch_us_holdings(token)

    # [#6 개선] catalyst 자기 매수만 보유로 인식 (다른 strategy 충돌 방지)
    catalyst_holdings = {}
    for sym, h in raw_holdings.items():
        entry_info = fetch_catalyst_entry(sym)
        if entry_info is not None:
            catalyst_holdings[sym] = {
                **h,
                "catalyst_qty": entry_info["qty"],
                "catalyst_avg": entry_info["avg_price"],
                "catalyst_entry_date": entry_info["entry_date"],
            }

    print(f"  현재 catalyst 포지션: {len(catalyst_holdings)}개 (자기 매수만)")
    for sym, h in catalyst_holdings.items():
        print(f"    {sym}: catalyst {h['catalyst_qty']}주 @ ${h['catalyst_avg']:.2f} "
              f"(KIS 잔고 합산 {h['qty']}주 / 평단 ${h['avg_price_usd']:.2f})")
    if len(raw_holdings) > len(catalyst_holdings):
        other_syms = [s for s in raw_holdings if s not in catalyst_holdings]
        print(f"  다른 strategy 보유 (catalyst 무관): {', '.join(other_syms)}")

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
    syms_to_price = list(set(active_syms + list(catalyst_holdings.keys())))
    prices = fetch_us_prices(token, syms_to_price) if syms_to_price else {}

    actions = []

    # 5. [#3 개선] 청산 룰 (trailing stop + +10% cap + -3% 손절 + 2일 max)
    print("\n[4] 청산 평가 (동적 익절 룰)...")
    for sym, h in catalyst_holdings.items():
        current = prices.get(sym, h.get("current_price_usd", 0))
        avg = h["catalyst_avg"]
        catalyst_qty = h["catalyst_qty"]
        entry_d = h["catalyst_entry_date"]
        if avg <= 0 or current <= 0:
            continue
        pnl_pct = (current - avg) / avg
        days_held = (today - entry_d).days

        # [#3] trailing 상태 로드
        state = load_catalyst_state(sym)
        trailing_active = state["trailing_active"]
        peak = max(state["peak_price"], current, avg)

        # [#3] +3% 도달 시 trailing 활성
        if not trailing_active and pnl_pct >= PROFIT_TRIGGER:
            trailing_active = True
            print(f"  {sym}: 🎯 trailing 활성 (+{pnl_pct*100:.2f}%)")

        # peak 갱신
        if current > peak:
            peak = current

        # 영속화 (현 상태)
        save_catalyst_state(sym, trailing_active, peak)

        reason = None
        # +10% cap (수익 확보)
        if pnl_pct >= PROFIT_CAP:
            reason = f"+{pnl_pct*100:.2f}% 최대 익절 (cap)"
        # trailing 발동 (peak 부터 -1% drawdown)
        elif trailing_active:
            trail_threshold = peak * (1 - TRAIL_DRAWDOWN)
            if current <= trail_threshold:
                peak_pct = (peak - avg) / avg * 100
                reason = (
                    f"trailing 청산 (peak +{peak_pct:.2f}% → 현재 +{pnl_pct*100:.2f}%, "
                    f"drawdown -{TRAIL_DRAWDOWN*100:.0f}%)"
                )
        # -3% 손절 (trailing 비활성 상태에서만)
        if reason is None and not trailing_active and pnl_pct <= STOP_PCT:
            reason = f"{pnl_pct*100:.2f}% 손절"
        # 2일 max 시간만료
        if reason is None and days_held >= MAX_HOLDING_DAYS:
            reason = f"{days_held}일 max 보유 시간만료 ({pnl_pct*100:+.2f}%)"

        if reason:
            actions.append({
                "side": "SELL", "symbol": sym, "qty": catalyst_qty,
                "reason": reason,
            })
            print(f"  {sym}: SELL {catalyst_qty}주 — {reason}")
        else:
            trail_flag = "🎯 trailing" if trailing_active else "일반"
            print(f"  {sym}: 유지 ({pnl_pct*100:+.2f}%, peak +{(peak-avg)/avg*100:.2f}%, "
                  f"{days_held}일, {trail_flag})")

    # 6. [#7 개선] 매수 룰 — 쿨다운 + entry-aware
    print(f"\n[5] 매수 평가 (재진입 쿨다운 {REENTRY_COOLDOWN_DAYS}거래일)...")
    new_buys = []
    for sym in active_syms:
        # catalyst 가 이미 보유 중이면 skip
        if sym in catalyst_holdings:
            print(f"  {sym}: catalyst 이미 보유 → skip")
            continue
        # 재진입 쿨다운 체크
        if not can_reenter(sym, today):
            last_sell = fetch_last_sell_date(sym)
            days_since = (today - last_sell).days if last_sell else 0
            print(f"  {sym}: 쿨다운 중 (마지막 매도 {days_since}일 전, "
                  f"{REENTRY_COOLDOWN_DAYS}일 필요)")
            continue
        new_buys.append(sym)

    if new_buys and prices:
        capital_per_pos = sleeve_usd / len(new_buys)
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
    elif not active_syms:
        print("  catalyst window 활성 종목 없음")
    else:
        print("  매수 가능 종목 없음 (모두 보유 중 또는 쿨다운)")

    # 7. 실행
    if not actions:
        print("\n변경 없음.")
        _send_report(actions, catalyst_holdings, calendar,
                     sleeve_usd=sleeve_usd, prices=prices)
        return 0

    print(f"\n총 {len(actions)}개 액션:")
    for a in actions:
        print(f"  [{a['side']}] {a['symbol']} {a['qty']}주 — {a['reason']}")

    if not args.execute:
        print("\n※ DRY RUN")
        _send_report(actions, catalyst_holdings, calendar,
                     sleeve_usd=sleeve_usd, prices=prices)
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
                # [#3 개선] 매수 후 state init / 매도 후 state clear
                if side_lower == "buy" and filled > 0:
                    save_catalyst_state(sym, False, price)  # trailing 비활성, peak=매수가
                elif side_lower == "sell" and filled >= qty:
                    clear_catalyst_state(sym)  # 청산 → state 삭제
                results.append({**a, "status": "OK", "filled_qty": filled,
                                "fill_status": status, "price": price})
        except (kis_api.KISAPIError, RuntimeError) as e:
            print(f"  ❌ API 오류: {e}")
            results.append({**a, "status": "ERROR", "error": str(e)})
        if i < len(actions):
            time.sleep(0.5)

    ok = sum(1 for r in results if r["status"] == "OK")
    print(f"\n완료: 성공 {ok} / 실패 {len(results)-ok}")
    _send_report(actions, catalyst_holdings, calendar, results,
                 sleeve_usd=sleeve_usd, prices=prices)
    return 0


if __name__ == "__main__":
    sys.exit(main())
