"""
미국 v3 자동 실행 — 체제별 어댑티브.

전략: swing_strategy_v3 (BULL/RANGE/BEAR 체제별 다른 룰)
유니버스: SPY, QQQ, AAPL, NVDA, TSLA
배정: 전체 자본의 15%
DB 태그: 'swing_v3'
스케줄: 평일 23:50 KST
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
from . import fear_greed
from . import indicators
from . import kis_api
from . import kis_auth
from . import load_candles
from . import market_regime as mr
from . import notify
from . import place_overseas_order
from . import swing_strategy_v3 as v3

SWING_UNIVERSE = ["AAPL", "NVDA", "TSLA"]  # SPY/QQQ 제거 (장기 BH 못 잡음)
SWING_ALLOCATION = 0.15
MAX_POSITIONS = 3
STRATEGY_TAG = "swing_v3"
USD_KRW_ESTIMATE = 1400
ORDER_PRICE_BUFFER = 0.005

# v4 F&G 극단 분할매매 룰 (relaxed)
FG_EXTREME_FEAR = 7
FG_EXTREME_GREED_SELL = 92
FG_SPLIT_BUY_RATIO = 0.25
FG_SPLIT_SELL_RATIO = 0.10
FG_SKIP_SELL_IN_BULL = True


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


def reconstruct_position(symbol: str, df: pd.DataFrame) -> v3.PositionV3 | None:
    """trades 테이블에서 v3 미국 포지션 재구성."""
    with db.connection() as conn:
        rows = conn.execute(
            """
            SELECT side, quantity, price, executed_at, notes
            FROM trades
            WHERE strategy = ? AND env = ? AND symbol = ?
            ORDER BY executed_at ASC
            """,
            (STRATEGY_TAG, config.KIS_ENV, symbol),
        ).fetchall()

    if not rows:
        return None

    cycles: list[list] = [[]]
    for r in rows:
        cycles[-1].append(r)
        net = sum(
            int(x["quantity"]) if x["side"] == "buy" else -int(x["quantity"])
            for x in cycles[-1]
        )
        if net <= 0 and len(cycles[-1]) > 1:
            cycles.append([])

    if not cycles[-1]:
        return None

    current_cycle = cycles[-1]
    total_qty = 0
    total_cost = 0.0
    initial_qty = 0
    entry_date: pd.Timestamp | None = None
    entry_regime = mr.REGIME_RANGE
    pf_t1_done = False
    pf_t2_done = False
    trailing_active = False
    dca_done = False

    buy_count = 0
    for r in current_cycle:
        side = r["side"]
        qty = int(r["quantity"])
        price = float(r["price"] or 0)
        notes = r["notes"] or ""

        if side == "buy":
            buy_count += 1
            if buy_count == 1:
                entry_date = pd.Timestamp(r["executed_at"])
                initial_qty = qty
                for regime in [mr.REGIME_BULL, mr.REGIME_BEAR, mr.REGIME_RANGE]:
                    if regime in notes:
                        entry_regime = regime
                        break
            else:
                dca_done = True
            total_qty += qty
            total_cost += qty * price
        elif side == "sell":
            total_qty -= qty
            if "1차" in notes or "+3%" in notes:
                pf_t1_done = True
            if "2차" in notes or "+7%" in notes or "+20%" in notes:
                pf_t2_done = True
            if "트레일" in notes:
                trailing_active = True

    if total_qty <= 0 or entry_date is None:
        return None

    total_buy_qty = sum(
        int(r["quantity"]) for r in current_cycle if r["side"] == "buy"
    )
    avg_price = total_cost / total_buy_qty if total_buy_qty > 0 else 0

    if entry_date in df.index or any(d >= entry_date for d in df.index):
        slice_df = df.loc[df.index >= entry_date]
        peak = float(slice_df["close"].max()) if not slice_df.empty else avg_price
    else:
        peak = avg_price

    pos = v3.PositionV3(
        qty=total_qty, avg_price=avg_price, entry_date=entry_date,
        initial_qty=initial_qty, peak_price=peak, entry_regime=entry_regime,
        pf_t1_done=pf_t1_done, pf_t2_done=pf_t2_done,
        trailing_active=trailing_active, dca_done=dca_done,
    )

    # B4 fix: position_states 에서 in-memory 상태 overlay
    state = _load_position_state(symbol)
    if state is not None:
        pos.trailing_active = state["trailing_active"] or pos.trailing_active
        pos.pf_t1_done = state["pf_t1_done"] or pos.pf_t1_done
        pos.pf_t2_done = state["pf_t2_done"] or pos.pf_t2_done
        pos.dca_done = state["dca_done"] or pos.dca_done
        pos.peak_price = max(pos.peak_price, state["peak_price"])
        if state["entry_regime"]:
            pos.entry_regime = state["entry_regime"]

    return pos


def save_position_state(symbol: str, position: v3.PositionV3) -> None:
    """[B4 fix] v3 in-memory 상태 영속화 (US)."""
    with db.connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO position_states
                (symbol, strategy, env, trailing_active, pf_t1_done,
                 pf_t2_done, dca_done, peak_price, entry_regime, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                symbol, STRATEGY_TAG, config.KIS_ENV,
                int(position.trailing_active),
                int(position.pf_t1_done),
                int(position.pf_t2_done),
                int(position.dca_done),
                float(position.peak_price),
                position.entry_regime,
            ),
        )


def clear_position_state(symbol: str) -> None:
    """[B4 fix] 청산된 포지션의 영속 상태 삭제."""
    with db.connection() as conn:
        conn.execute(
            """
            DELETE FROM position_states
            WHERE symbol = ? AND strategy = ? AND env = ?
            """,
            (symbol, STRATEGY_TAG, config.KIS_ENV),
        )


def _load_position_state(symbol: str) -> dict | None:
    """[B4 fix] position_states 에서 in-memory 상태 로드."""
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT trailing_active, pf_t1_done, pf_t2_done, dca_done,
                   peak_price, entry_regime
            FROM position_states
            WHERE symbol = ? AND strategy = ? AND env = ?
            """,
            (symbol, STRATEGY_TAG, config.KIS_ENV),
        ).fetchone()
    if row is None:
        return None
    return {
        "trailing_active": bool(row["trailing_active"]),
        "pf_t1_done": bool(row["pf_t1_done"]),
        "pf_t2_done": bool(row["pf_t2_done"]),
        "dca_done": bool(row["dca_done"]),
        "peak_price": float(row["peak_price"] or 0),
        "entry_regime": row["entry_regime"] or mr.REGIME_RANGE,
    }


def _query_us_holdings(token: str) -> dict[str, int]:
    """KIS US 잔고 → {symbol: qty}.

    [B2 fix] 주문 전후 잔고 차분으로 실제 체결량 확인.
    """
    holdings = check_overseas_balance.fetch_all_us_holdings(token)
    return {h["symbol"]: h["qty"] for h in holdings}


def _execute_with_fill_check_us(
    sym: str,
    side: str,
    requested_qty: int,
    token: str,
    wait_seconds: float = 6.0,  # US 는 KR 보다 약간 더 (해외 지연 고려)
) -> dict:
    """[B2 fix] US 주문 → 잔고 차분으로 실제 체결량 확인."""
    try:
        pre_holdings = _query_us_holdings(token)
    except Exception as e:
        print(f"  [B2] US 사전 잔고 조회 실패: {e} — fill check 스킵")
        pre_holdings = None

    pre_qty = pre_holdings.get(sym, 0) if pre_holdings else 0

    res = place_overseas_order.place_market_like_order(
        sym, requested_qty, side, token, buffer=ORDER_PRICE_BUFFER
    )
    out = res.get("output", {})
    odno = out.get("ODNO", "?")
    msg = res.get("msg1", "")

    time.sleep(wait_seconds)

    if pre_holdings is None:
        try:
            p = check_overseas_price.fetch_price(sym, token)
            price = float(p.get("output", {}).get("last") or 0)
        except Exception:
            price = 0.0
        return {
            "order_id": odno, "msg": msg,
            "requested_qty": requested_qty, "filled_qty": requested_qty,
            "price": price, "status": "FILLED",
        }

    try:
        post_holdings = _query_us_holdings(token)
    except Exception as e:
        print(f"  [B2] US 사후 잔고 조회 실패: {e} — fallback FILLED")
        try:
            p = check_overseas_price.fetch_price(sym, token)
            price = float(p.get("output", {}).get("last") or 0)
        except Exception:
            price = 0.0
        return {
            "order_id": odno, "msg": msg,
            "requested_qty": requested_qty, "filled_qty": requested_qty,
            "price": price, "status": "FILLED",
        }

    post_qty = post_holdings.get(sym, 0)
    delta = post_qty - pre_qty
    filled_qty = delta if side == "buy" else -delta
    if filled_qty < 0:
        filled_qty = 0

    try:
        p = check_overseas_price.fetch_price(sym, token)
        price = float(p.get("output", {}).get("last") or 0)
    except Exception:
        price = 0.0

    if filled_qty == 0:
        status = "REJECTED"
    elif filled_qty < requested_qty:
        status = "PARTIAL"
    else:
        status = "FILLED"

    return {
        "order_id": odno, "msg": msg,
        "requested_qty": requested_qty, "filled_qty": filled_qty,
        "price": price, "status": status,
    }


def _persist_position_states(
    positions: dict[str, v3.PositionV3],
    results: list[dict] | None = None,
) -> None:
    """[B4 fix] 매 실행 끝에 살아있는 포지션 state 영속화."""
    cleared: set[str] = set()
    if results is not None:
        for r in results:
            if r.get("status") != "OK":
                continue
            sym = r.get("symbol")
            qty = r.get("qty", 0)
            if r.get("action") == "SELL" and sym in positions:
                if qty >= positions[sym].qty:
                    clear_position_state(sym)
                    cleared.add(sym)
    for sym, pos in positions.items():
        if sym not in cleared:
            save_position_state(sym, pos)


def get_v3_positions(symbol_data: dict[str, pd.DataFrame]) -> dict[str, v3.PositionV3]:
    positions = {}
    for sym in SWING_UNIVERSE:
        if sym not in symbol_data:
            continue
        pos = reconstruct_position(sym, symbol_data[sym])
        if pos is not None:
            positions[sym] = pos
    return positions


def log_trade(symbol, side, qty, price_usd, order_id, msg, regime_note=""):
    notes = f"{regime_note} | {msg}" if regime_note else msg
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO trades
                (symbol, side, quantity, price, executed_at, env, strategy, order_id, notes)
            VALUES (?, ?, ?, ?, datetime('now'), ?, ?, ?, ?)
            """,
            (symbol, side, qty, price_usd, config.KIS_ENV, STRATEGY_TAG, order_id, notes),
        )


def confirm_execution() -> bool:
    print("\n" + "!" * 64)
    print("v3 US 주문 실제 전송. y / yes 외엔 취소.")
    print("!" * 64)
    try:
        return input("[y/N]: ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _send_report(swing_budget_usd, positions, scan_results, actions, results=None):
    if not notify.is_enabled():
        return
    today = pd.Timestamp.now().strftime("%-m/%-d (%a) %H:%M KST")
    mode = "모의" if config.KIS_ENV == "paper" else "실거래"
    lines = [
        f"환경: {mode}",
        f"v3 US 슬롯: ${swing_budget_usd:,.2f}",
        fear_greed.status_text(),
        "",
    ]

    if positions:
        lines.append("[v3 US 포지션]")
        for sym, pos in positions.items():
            lines.append(f"  {sym}({pos.entry_regime}): {pos.qty}주 @ ${pos.avg_price:.2f}")
    else:
        lines.append("[v3 US 포지션] 없음")
    lines.append("")

    lines.append("[시그널 스캔]")
    for sym, status in scan_results.items():
        lines.append(f"  {sym}: {status}")
    lines.append("")

    if actions and results is not None:
        lines.append("[실행 결과]")
        for a in results:
            if a["status"] == "OK":
                lines.append(f"  ✓ {a['action']} {a['symbol']} {a['qty']}주 — {a['reason']}")
            else:
                lines.append(f"  ✗ {a['action']} {a['symbol']}: 실패")
    elif actions:
        lines.append("[계획(드라이런)]")
        for a in actions:
            lines.append(f"  {a['action']} {a['symbol']} {a['qty']}주 — {a['reason']}")
    else:
        lines.append("거래 없음 (조건 미충족)")

    notify.send(f"<b>🎯 v3 US — {today}</b>\n\n" + "\n".join(lines))


def _to_int(v):
    try:
        return int(v or 0)
    except (ValueError, TypeError):
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="US v3 자동 실행")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    if args.execute and config.KIS_ENV != "paper":
        print("[차단] 실거래 모드. KIS_ENV=paper 확인.")
        return 1

    print("=" * 64)
    print(f"v3 US (체제별 어댑티브) {'실행' if args.execute else '드라이런'}")
    print("=" * 64)
    print(f"유니버스: {SWING_UNIVERSE} | 배정 15%")

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
    print(f"\n총평가: ₩{total_krw:,} | v3 US 슬롯: ${swing_budget_usd:,.2f}")

    # 공포탐욕지수
    fg_info = fear_greed.fetch_index()
    fg_value = fg_info.get("value", 50)
    fg_class = fg_info.get("classification", "?")
    fg_modifier = fear_greed.position_size_modifier()
    fg_block = fear_greed.should_block_entry()
    print(f"공포탐욕지수: {fg_value} ({fg_class}) | 사이즈 배율 {fg_modifier:.2f}x"
          f" | 진입차단: {fg_block}")

    positions = get_v3_positions(symbol_data)
    print(f"\n현재 v3 US 포지션: {len(positions)}개")
    for sym, pos in positions.items():
        print(f"  {sym}({pos.entry_regime}): {pos.qty}주 @ ${pos.avg_price:.2f}")

    actions: list[dict] = []
    scan_results: dict[str, str] = {}
    current_date = pd.Timestamp(date.today())

    print("\n[청산/익절 체크]")
    for sym, pos in list(positions.items()):
        df = symbol_data[sym]
        latest = df.iloc[-1]
        sell_actions = v3.check_exit_v3(latest, pos, current_date)
        if not sell_actions:
            dca = v3.check_dca(latest, pos)
            if dca is not None:
                actions.append({
                    "action": "BUY", "symbol": sym, "qty": dca.qty, "reason": dca.reason
                })
                scan_results[sym] = f"DCA — {dca.reason}"
                continue
        for a in sell_actions:
            actions.append({
                "action": "SELL", "symbol": sym, "qty": a.qty, "reason": a.reason
            })
        if sell_actions:
            scan_results[sym] = f"청산: {sell_actions[0].reason}"
            print(f"  {sym}({pos.entry_regime}): {sell_actions[0].reason}")
        else:
            pnl = (float(latest["close"]) - pos.avg_price) / pos.avg_price
            scan_results[sym] = f"보유 ({pnl*100:+.2f}%)"
            print(f"  {sym}({pos.entry_regime}): 유지 ({pnl*100:+.2f}%)")

    # 1.5. F&G 극단 분할매매 (v4 relaxed)
    fg_targeted: set[str] = set()
    if fg_value <= FG_EXTREME_FEAR:
        # 분할매수: 첫 universe 종목 (AAPL), slot 25%
        target_sym = SWING_UNIVERSE[0]
        target_df = symbol_data.get(target_sym)
        if target_df is not None and not target_df.empty:
            target_price = float(target_df.iloc[-1]["close"])
            cash_to_use_usd = swing_budget_usd * FG_SPLIT_BUY_RATIO
            qty = int(cash_to_use_usd / target_price)
            if qty > 0:
                print(f"\n[F&G 극공포 {fg_value}] 분할매수: {target_sym} {qty}주")
                actions.append({
                    "action": "BUY", "symbol": target_sym, "qty": qty,
                    "reason": f"F&G {fg_value} 극공포 분할매수",
                })
                fg_targeted.add(target_sym)

    elif fg_value >= FG_EXTREME_GREED_SELL:
        for sym, pos in positions.items():
            if FG_SKIP_SELL_IN_BULL and pos.entry_regime == mr.REGIME_BULL:
                print(f"  [F&G 극탐 {fg_value}] {sym}: BULL 모드 → 매도 건너뜀")
                continue
            if any(a["symbol"] == sym and a["action"] == "SELL" for a in actions):
                continue
            sell_qty = max(int(pos.qty * FG_SPLIT_SELL_RATIO), 1)
            if 0 < sell_qty <= pos.qty:
                print(f"\n[F&G 극탐 {fg_value}] 분할매도: {sym} {sell_qty}주 (체제 {pos.entry_regime})")
                actions.append({
                    "action": "SELL", "symbol": sym, "qty": sell_qty,
                    "reason": f"F&G {fg_value} 극탐 분할매도",
                })
                fg_targeted.add(sym)

    print("\n[진입 체크]")

    # [B3 fix] MAX_POSITIONS 정확 카운팅:
    # - 부분익절 / F&G 분할매도 / DCA 매수 등은 포지션 수 변화 X
    # - 전량 청산 (qty >= position.qty) 만 -1
    # - 새 종목 BUY 만 +1
    def _count_projected_positions(positions, actions):
        count = len(positions)
        for a in actions:
            sym = a["symbol"]
            if a["action"] == "SELL":
                if sym in positions and a["qty"] >= positions[sym].qty:
                    count -= 1
            elif a["action"] == "BUY":
                if sym not in positions:
                    count += 1
        return count

    projected = _count_projected_positions(positions, actions)

    for sym in SWING_UNIVERSE:
        if sym in positions:
            continue
        if sym in fg_targeted:
            print(f"  {sym}: F&G 극단으로 이미 처리됨")
            scan_results[sym] = "F&G 극단 처리됨"
            continue
        if projected >= MAX_POSITIONS:
            scan_results[sym] = "최대 포지션"
            continue
        df = symbol_data[sym]
        if len(df) < 2:
            continue
        regime_today = mr.detect_regime(df.iloc[-1])
        params = v3.get_params(regime_today)

        if params.get("block_entry"):
            print(f"  {sym}: BEAR 차단 ({regime_today})")
            scan_results[sym] = f"{regime_today} 차단"
            continue

        # F&G 진입 차단 체크
        if fg_block:
            print(f"  {sym}: F&G {fg_value} 과열 → 진입 차단")
            scan_results[sym] = f"F&G {fg_value} 과열 차단"
            continue

        prev_row = df.iloc[-2]
        sig = v3.check_entry(prev_row, regime_today)
        if sig.valid:
            latest_close = float(df.iloc[-1]["close"])
            ratio = params["initial_buy_ratio"] * fg_modifier  # F&G 배율
            slot_per_pos = swing_budget_usd / MAX_POSITIONS
            target_alloc = slot_per_pos * ratio
            qty = int(target_alloc // latest_close)
            if qty <= 0:
                scan_results[sym] = "크기 부족"
                continue
            print(f"  {sym}: 진입 ({regime_today}) — {', '.join(sig.reasons)}")
            print(f"    {qty}주 @ ~${latest_close:.2f} = ~${qty*latest_close:.2f}")
            actions.append({
                "action": "BUY", "symbol": sym, "qty": qty,
                "reason": f"v3 진입 (체제: {regime_today}, F&G {fg_value})",
            })
            projected += 1
            scan_results[sym] = f"진입 신호 ✓ ({regime_today})"
        else:
            r = sig.reasons[0] if sig.reasons else "?"
            print(f"  {sym}: 시그널 없음 ({r}, 체제 {regime_today})")
            scan_results[sym] = f"대기 ({r}) [{regime_today}]"

    if not actions:
        print("\n변경 없음.")
        _persist_position_states(positions)  # B4: 변경 없어도 mutated state 보존
        _send_report(swing_budget_usd, positions, scan_results, actions)
        return 0

    print(f"\n총 {len(actions)}개 액션:")
    for a in actions:
        print(f"  [{a['action']}] {a['symbol']} {a['qty']}주 — {a['reason']}")

    if not args.execute:
        print("\n※ DRY RUN")
        _persist_position_states(positions)  # B4: 드라이런도 state 보존
        _send_report(swing_budget_usd, positions, scan_results, actions)
        return 0

    if not args.yes and not confirm_execution():
        print("취소됨.")
        _persist_position_states(positions)  # B4: 취소도 state 보존
        return 0

    print("\n주문 전송 중... (B2: fill check via US 잔고 차분)")
    results = []
    for i, a in enumerate(actions, 1):
        side_lower = a["action"].lower()
        sym = a["symbol"]
        requested_qty = a["qty"]
        print(f"\n[{i}/{len(actions)}] {a['action']} {sym} {requested_qty}주...")
        try:
            fill = _execute_with_fill_check_us(
                sym, side_lower, requested_qty, token, wait_seconds=6.0,
            )
            odno = fill["order_id"]
            filled = fill["filled_qty"]
            price = fill["price"]
            status = fill["status"]
            msg = fill["msg"]

            if status == "REJECTED":
                print(f"  ❌ REJECTED 주문번호 {odno} — 미체결 (msg: {msg})")
                results.append({
                    **a, "status": "ERROR", "filled_qty": 0,
                    "fill_status": "REJECTED", "error": f"미체결: {msg}",
                })
            elif status == "PARTIAL":
                print(f"  ⚠️ PARTIAL {filled}/{requested_qty}주 @ ${price:.2f} (주문 {odno})")
                log_trade(sym, side_lower, filled, price, odno,
                          f"PARTIAL {filled}/{requested_qty} | {msg}", a["reason"])
                results.append({
                    **a, "status": "OK", "qty": filled, "filled_qty": filled,
                    "price": price, "fill_status": "PARTIAL",
                })
            else:
                print(f"  ✅ FILLED {filled}주 @ ${price:.2f} (주문 {odno})")
                log_trade(sym, side_lower, filled, price, odno, msg, a["reason"])
                results.append({
                    **a, "status": "OK", "qty": filled, "filled_qty": filled,
                    "price": price, "fill_status": "FILLED",
                })
        except (kis_api.KISAPIError, RuntimeError) as e:
            print(f"  ❌ API 오류: {e}")
            results.append({**a, "status": "ERROR", "error": str(e)})
        if i < len(actions):
            time.sleep(0.5)

    ok = sum(1 for r in results if r["status"] == "OK")
    print(f"\n완료: 성공 {ok} / 실패 {len(results)-ok}")
    # B4: 실 주문 결과 반영 — 청산된 sym clear, 나머지 save
    _persist_position_states(positions, results)
    _send_report(swing_budget_usd, positions, scan_results, actions, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
