"""
한국 v3 자동 실행 — 체제별 어댑티브.

전략: swing_strategy_v3 (BULL/RANGE/BEAR 체제별 다른 룰)
유니버스: 069500, 005930, 000660, 035420
배정: 전체 자본의 15%
DB 태그: 'swing_v3'
스케줄: 평일 09:20 KST

상태 추적:
  - 현재 포지션은 trades 테이블에서 재구성
  - 부분익절·DCA는 각각 별도 trade 레코드로 기록
  - notes 필드에 "체제: BULL" 등 적어 두고 재구성 시 파싱
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
from . import fear_greed
from . import indicators
from . import kis_api
from . import kis_auth
from . import load_candles
from . import market_regime as mr
from . import notify
from . import place_order
from . import swing_strategy_v3 as v3

SWING_UNIVERSE = ["069500", "005930", "035420"]  # 000660 제거 (BH +4109% 못 잡음)
SWING_ALLOCATION = 0.15
MAX_POSITIONS = 3
STRATEGY_TAG = "swing_v3"

# v4 F&G 극단 분할매매 룰 (relaxed)
FG_EXTREME_FEAR = 7
FG_EXTREME_GREED_SELL = 92
FG_SPLIT_BUY_RATIO = 0.25      # 슬롯 cash 25% 매수
FG_SPLIT_SELL_RATIO = 0.10     # 보유 10% 매도
FG_SKIP_SELL_IN_BULL = True    # BULL 모드 매도 무시


def refresh_data() -> None:
    for sym in SWING_UNIVERSE:
        load_candles.load_symbol(sym, "KR", years=2)


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
    """trades 테이블에서 v3 포지션 상태 복원."""
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

    # 마지막 청산 이후 새 진입 추적
    cycles: list[list] = [[]]
    for r in rows:
        cycles[-1].append(r)
        # 만약 이 매도로 net qty 0 되면 새 사이클 시작
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

    # 평단가 (가중)
    total_buy_qty = sum(
        int(r["quantity"]) for r in current_cycle if r["side"] == "buy"
    )
    avg_price = total_cost / total_buy_qty if total_buy_qty > 0 else 0

    # peak_price: 진입 후 최고 종가
    if entry_date in df.index or any(d >= entry_date for d in df.index):
        slice_df = df.loc[df.index >= entry_date]
        peak = float(slice_df["close"].max()) if not slice_df.empty else avg_price
    else:
        peak = avg_price

    return v3.PositionV3(
        qty=total_qty, avg_price=avg_price, entry_date=entry_date,
        initial_qty=initial_qty, peak_price=peak, entry_regime=entry_regime,
        pf_t1_done=pf_t1_done, pf_t2_done=pf_t2_done,
        trailing_active=trailing_active, dca_done=dca_done,
    )


def get_v3_positions(symbol_data: dict[str, pd.DataFrame]) -> dict[str, v3.PositionV3]:
    positions = {}
    for sym in SWING_UNIVERSE:
        if sym not in symbol_data:
            continue
        pos = reconstruct_position(sym, symbol_data[sym])
        if pos is not None:
            positions[sym] = pos
    return positions


def log_trade(symbol, side, qty, price, order_id, msg, regime_note=""):
    notes = f"{regime_note} | {msg}" if regime_note else msg
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO trades
                (symbol, side, quantity, price, executed_at, env, strategy, order_id, notes)
            VALUES (?, ?, ?, ?, datetime('now'), ?, ?, ?, ?)
            """,
            (symbol, side, qty, price, config.KIS_ENV, STRATEGY_TAG, order_id, notes),
        )


def confirm_execution() -> bool:
    print("\n" + "!" * 64)
    print("v3 KR 주문 실제 전송. y / yes 외엔 취소.")
    print("!" * 64)
    try:
        return input("[y/N]: ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _send_report(swing_budget, positions, scan_results, actions, results=None):
    if not notify.is_enabled():
        return
    today = pd.Timestamp.now().strftime("%-m/%-d (%a) %H:%M KST")
    mode = "모의" if config.KIS_ENV == "paper" else "실거래"
    lines = [
        f"환경: {mode}",
        f"v3 KR 슬롯: {swing_budget:,}원",
        fear_greed.status_text(),
        "",
    ]

    if positions:
        lines.append("[v3 포지션]")
        for sym, pos in positions.items():
            lines.append(f"  {sym}({pos.entry_regime}): {pos.qty}주 @ {int(pos.avg_price):,}")
    else:
        lines.append("[v3 포지션] 없음")
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

    notify.send(f"<b>🎯 v3 KR — {today}</b>\n\n" + "\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description="KR v3 자동 실행")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    if args.execute and config.KIS_ENV != "paper":
        print("[차단] 실거래 모드. KIS_ENV=paper 확인.")
        return 1

    print("=" * 64)
    print(f"v3 KR (체제별 어댑티브) {'실행' if args.execute else '드라이런'}")
    print("=" * 64)
    print(f"유니버스: {SWING_UNIVERSE} | 배정 15%")

    if args.refresh:
        print("\n[준비] 데이터 갱신...")
        refresh_data()

    symbol_data = load_indicators_latest()
    if not symbol_data:
        print("[실패] 지표 데이터 없음.")
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
    try:
        total_equity = int(output2.get("tot_evlu_amt") or 0)
    except (ValueError, TypeError):
        total_equity = 0

    swing_budget = int(total_equity * SWING_ALLOCATION)
    print(f"\n총평가: {total_equity:,}원 | v3 KR 슬롯: {swing_budget:,}원")

    # 공포탐욕지수 체크
    fg_info = fear_greed.fetch_index()
    fg_value = fg_info.get("value", 50)
    fg_class = fg_info.get("classification", "?")
    fg_modifier = fear_greed.position_size_modifier()
    fg_block = fear_greed.should_block_entry()
    print(f"공포탐욕지수: {fg_value} ({fg_class}) | 사이즈 배율 {fg_modifier:.2f}x"
          f" | 진입차단: {fg_block}")

    positions = get_v3_positions(symbol_data)
    print(f"\n현재 v3 KR 포지션: {len(positions)}개")
    for sym, pos in positions.items():
        print(f"  {sym}({pos.entry_regime}): {pos.qty}주 @ {int(pos.avg_price):,} "
              f"(peak {int(pos.peak_price):,}, DCA {pos.dca_done})")

    actions: list[dict] = []  # {"action", "symbol", "qty", "reason"}
    scan_results: dict[str, str] = {}
    current_date = pd.Timestamp(date.today())

    # 1. 청산 / 부분익절 / 트레일링 체크
    print("\n[청산/익절 체크]")
    for sym, pos in list(positions.items()):
        df = symbol_data[sym]
        latest = df.iloc[-1]
        sell_actions = v3.check_exit_v3(latest, pos, current_date)
        # DCA 체크 (청산 액션 없을 때만)
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
        # 분할매수: 슬롯 cash 의 25%
        target_sym = SWING_UNIVERSE[0]  # 첫 종목 (069500 KODEX 200)
        target_df = symbol_data.get(target_sym)
        if target_df is not None and not target_df.empty:
            target_price = float(target_df.iloc[-1]["close"])
            cash_to_use = swing_budget * FG_SPLIT_BUY_RATIO
            qty = int(cash_to_use / target_price)
            if qty > 0:
                print(f"\n[F&G 극공포 {fg_value}] 분할매수: {target_sym} {qty}주")
                actions.append({
                    "action": "BUY", "symbol": target_sym, "qty": qty,
                    "reason": f"F&G {fg_value} 극공포 분할매수",
                })
                fg_targeted.add(target_sym)

    elif fg_value >= FG_EXTREME_GREED_SELL:
        # 분할매도: 보유 포지션 10% (BULL 제외)
        for sym, pos in positions.items():
            if FG_SKIP_SELL_IN_BULL and pos.entry_regime == mr.REGIME_BULL:
                print(f"  [F&G 극탐 {fg_value}] {sym}: BULL 모드 → 매도 건너뜀")
                continue
            # 이미 v3 청산 액션 있으면 스킵
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

    # 2. 진입 체크 (체제별 룰)
    print("\n[진입 체크]")

    # [B3 fix] MAX_POSITIONS 정확 카운팅:
    # - 부분익절 / F&G 분할매도 / DCA 매수 등은 포지션 수 변화 X
    # - 전량 청산 (qty >= position.qty) 만 -1
    # - 새 종목 BUY 만 +1
    # 이전에는 모든 SELL 을 -1 처리 → 부분익절 시 새 종목 진입 가능 → MAX 초과
    def _count_projected_positions(positions, actions):
        count = len(positions)
        for a in actions:
            sym = a["symbol"]
            if a["action"] == "SELL":
                # 전량 청산만 -1 (부분익절은 무시)
                if sym in positions and a["qty"] >= positions[sym].qty:
                    count -= 1
            elif a["action"] == "BUY":
                # 새 종목 진입만 +1 (DCA/F&G 분할매수는 무시)
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
        # 오늘 시점 체제 (실시간) + 어제 종가로 시그널
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
            ratio = params["initial_buy_ratio"] * fg_modifier  # F&G 배율 적용
            slot_per_pos = swing_budget // MAX_POSITIONS
            target_alloc = slot_per_pos * ratio
            qty = int(target_alloc // latest_close)
            if qty <= 0:
                scan_results[sym] = "크기 부족"
                continue
            print(f"  {sym}: 진입 ({regime_today}) — {', '.join(sig.reasons)}")
            print(f"    {qty}주 @ ~{int(latest_close):,} = ~{qty*int(latest_close):,}원"
                  f" (F&G {fg_modifier:.2f}x)")
            actions.append({
                "action": "BUY", "symbol": sym, "qty": qty,
                "reason": f"v3 진입 (체제: {regime_today}, F&G {fg_value})",
            })
            projected += 1  # 새 종목 진입 → +1
            scan_results[sym] = f"진입 신호 ✓ ({regime_today})"
        else:
            r = sig.reasons[0] if sig.reasons else "?"
            print(f"  {sym}: 시그널 없음 ({r}, 체제 {regime_today})")
            scan_results[sym] = f"대기 ({r}) [{regime_today}]"

    if not actions:
        print("\n변경 없음.")
        _send_report(swing_budget, positions, scan_results, actions)
        return 0

    print(f"\n총 {len(actions)}개 액션:")
    for a in actions:
        print(f"  [{a['action']}] {a['symbol']} {a['qty']}주 — {a['reason']}")

    if not args.execute:
        print("\n※ DRY RUN")
        _send_report(swing_budget, positions, scan_results, actions)
        return 0

    if not args.yes and not confirm_execution():
        print("취소됨.")
        return 0

    print("\n주문 전송 중...")
    results = []
    for i, a in enumerate(actions, 1):
        side_lower = a["action"].lower()
        sym = a["symbol"]
        qty = a["qty"]
        print(f"\n[{i}/{len(actions)}] {a['action']} {sym} {qty}주...")
        try:
            res = place_order.place_market_order(sym, qty, side_lower, token)
            out = res.get("output", {})
            odno = out.get("ODNO", "?")
            msg = res.get("msg1", "")
            print(f"  성공. {odno}")
            try:
                p_data = check_price.fetch_price(sym, token)
                price = int(p_data.get("output", {}).get("stck_prpr") or 0)
            except Exception:
                price = 0
            log_trade(sym, side_lower, qty, price, odno, msg, a["reason"])
            results.append({**a, "status": "OK", "price": price})
        except kis_api.KISAPIError as e:
            print(f"  실패: {e}")
            results.append({**a, "status": "ERROR", "error": str(e)})
        if i < len(actions):
            time.sleep(0.5)

    ok = sum(1 for r in results if r["status"] == "OK")
    print(f"\n완료: 성공 {ok} / 실패 {len(results)-ok}")
    _send_report(swing_budget, positions, scan_results, actions, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
