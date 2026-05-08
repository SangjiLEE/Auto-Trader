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
from . import market_hours
from . import safety
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


def save_position_state(symbol: str, position: v3.PositionV3) -> None:
    """[B4 fix] v3 in-memory 상태 영속화.

    trades 만으론 재구성 불가능한 상태 (trailing_active, peak_price)
    를 position_states 에 upsert. 매 실행 끝에 살아있는 포지션 모두 갱신.
    """
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


def reconstruct_position(symbol: str, df: pd.DataFrame) -> v3.PositionV3 | None:
    """trades 테이블에서 v3 포지션 상태 복원.

    [B4 fix] trades 기반 재구성 후 position_states 에서 overlay.
    trailing_active 처럼 trades 에 흔적 없는 상태를 정확히 복원.
    """
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

    pos = v3.PositionV3(
        qty=total_qty, avg_price=avg_price, entry_date=entry_date,
        initial_qty=initial_qty, peak_price=peak, entry_regime=entry_regime,
        pf_t1_done=pf_t1_done, pf_t2_done=pf_t2_done,
        trailing_active=trailing_active, dca_done=dca_done,
    )

    # B4 fix: position_states 에서 in-memory 상태 overlay
    # (trailing_active 처럼 trades 에 직접 흔적 없는 상태 복원)
    state = _load_position_state(symbol)
    if state is not None:
        pos.trailing_active = state["trailing_active"] or pos.trailing_active
        pos.pf_t1_done = state["pf_t1_done"] or pos.pf_t1_done
        pos.pf_t2_done = state["pf_t2_done"] or pos.pf_t2_done
        pos.dca_done = state["dca_done"] or pos.dca_done
        # peak_price 는 max (DB 값과 가격 히스토리 max 중 큰 쪽)
        pos.peak_price = max(pos.peak_price, state["peak_price"])
        if state["entry_regime"]:
            # 진입 시점 체제는 영속 상태가 더 정확 (notes 파싱 fallback 보다)
            pos.entry_regime = state["entry_regime"]

    return pos


def get_v3_positions(symbol_data: dict[str, pd.DataFrame]) -> dict[str, v3.PositionV3]:
    positions = {}
    for sym in SWING_UNIVERSE:
        if sym not in symbol_data:
            continue
        pos = reconstruct_position(sym, symbol_data[sym])
        if pos is not None:
            positions[sym] = pos
    return positions


def _query_kr_holdings(token: str) -> dict[str, int]:
    """KIS KR 잔고 → {symbol: qty}.

    [B2 fix] 주문 전후 잔고 차분으로 실제 체결량 확인하기 위한 helper.
    """
    data = check_balance.fetch_balance(token)
    output1 = data.get("output1", [])
    holdings: dict[str, int] = {}
    for h in output1:
        sym = (h.get("pdno") or "").strip()
        try:
            qty = int(h.get("hldg_qty") or 0)
        except (ValueError, TypeError):
            qty = 0
        if sym and qty > 0:
            holdings[sym] = qty
    return holdings


def _execute_with_fill_check(
    sym: str,
    side: str,
    requested_qty: int,
    token: str,
    wait_seconds: float = 4.0,
) -> dict:
    """[B2 fix] 주문 → 대기 → 잔고 재조회 → 실제 체결량 추정.

    문제: 기존 코드는 주문 수락(API 200) = 체결 가정. 실제로는 KIS 가
    수락만 하고 부분/지연/거부 가능. 거기에 fresh quote 를 trade price
    로 로깅 → DB 가 broker 와 동기 깨짐.

    해결: 주문 전후 잔고 스냅샷 차이 = 실제 체결량 (broker 가 source of truth).
    가격은 quote (근사) — 정확한 fill price 추출은 KIS inquire-order API
    필요하지만 환경별 tr_id 차이 등 복잡성 있어 추후 별도 작업.

    반환:
      {
        "order_id": ODNO,
        "msg": KIS 응답 메시지,
        "requested_qty": 요청 수량,
        "filled_qty": 실제 체결량 (잔고 차분 기반),
        "price": 체결가 추정 (현재 quote),
        "status": "FILLED" | "PARTIAL" | "REJECTED",
      }
    """
    # 1. 주문 전 잔고 스냅샷
    try:
        pre_holdings = _query_kr_holdings(token)
    except Exception as e:
        print(f"  [B2] 사전 잔고 조회 실패: {e} — fill check 스킵, legacy 동작")
        pre_holdings = None

    pre_qty = pre_holdings.get(sym, 0) if pre_holdings else 0

    # 2. 주문 실행 (예외는 caller 가 처리)
    res = place_order.place_market_order(sym, requested_qty, side, token)
    out = res.get("output", {})
    odno = out.get("ODNO", "?")
    msg = res.get("msg1", "")

    # 3. 체결 대기
    time.sleep(wait_seconds)

    # 4. 주문 후 잔고 스냅샷
    if pre_holdings is None:
        # 사전 조회 실패 시 fill check 무의미 — 요청량 그대로 가정
        try:
            p_data = check_price.fetch_price(sym, token)
            price = int(p_data.get("output", {}).get("stck_prpr") or 0)
        except Exception:
            price = 0
        return {
            "order_id": odno, "msg": msg,
            "requested_qty": requested_qty, "filled_qty": requested_qty,
            "price": price, "status": "FILLED",
        }

    try:
        post_holdings = _query_kr_holdings(token)
    except Exception as e:
        print(f"  [B2] 사후 잔고 조회 실패: {e} — 요청량 가정 (fallback)")
        try:
            p_data = check_price.fetch_price(sym, token)
            price = int(p_data.get("output", {}).get("stck_prpr") or 0)
        except Exception:
            price = 0
        return {
            "order_id": odno, "msg": msg,
            "requested_qty": requested_qty, "filled_qty": requested_qty,
            "price": price, "status": "FILLED",
        }

    post_qty = post_holdings.get(sym, 0)

    # 5. 차분 → 실제 체결량
    delta = post_qty - pre_qty
    filled_qty = delta if side == "buy" else -delta

    # 음수 방지 (다른 outflow 있을 가능성 — 보수적으로 0)
    if filled_qty < 0:
        filled_qty = 0

    # 6. 가격 (현재 quote — 근사)
    try:
        p_data = check_price.fetch_price(sym, token)
        price = int(p_data.get("output", {}).get("stck_prpr") or 0)
    except Exception:
        price = 0

    # 7. 상태 판정
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
    """[B4 fix] 매 실행 끝에 살아있는 포지션의 v3 in-memory state 영속화.

    드라이런 (results=None): 모든 살아있는 pos save
       (v3.check_exit_v3() 가 mutate 한 peak / trailing_active 등 보존)
    실 주문 (results=list): 실제 전량 청산된 sym 은 clear,
       나머지 살아있는 pos 는 save
    """
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

    notify.send(
        f"<b>🎯 v3 KR — {today}</b>\n\n" + "\n".join(lines),
        channel=notify.CHANNEL_KR_REALTIME,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="KR v3 자동 실행")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    if safety.block_execute_if_real(args.execute):
        return 1

    # 실 주문 시 시간 / 휴장 가드 (드라이런은 시간 무관)
    if args.execute:
        try:
            market_hours.assert_kis_paper_market_open()
        except RuntimeError as e:
            print(f"[차단] {e}")
            return 4

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
    # [B4] 청산 액션 + state 변화는 v3.check_exit_v3() 호출로 발생.
    # 그 호출이 pos 의 trailing_active / peak_price / pf_t*_done 을 mutate 함.
    # 이 mutated state 를 매 실행 끝에 position_states 로 영속화.
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
    # [Phase 2] KR 슬리브 시장-와이드 체제 (069500 기반) — BEAR 시 신규 진입 차단
    sleeve_regime = mr.detect_market_regime("KR")
    print(f"  KR 슬리브 시장 체제 (069500 기반): {sleeve_regime}")
    sleeve_block_entry = sleeve_regime == mr.REGIME_BEAR

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

        # [Phase 2] 슬리브 시장-와이드 체제 BEAR 시 신규 진입 차단
        if sleeve_block_entry:
            print(f"  {sym}: KR 슬리브 BEAR ({sleeve_regime}) → 진입 차단")
            scan_results[sym] = f"슬리브 {sleeve_regime} 차단"
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
        _persist_position_states(positions)  # B4: 변경 없어도 mutated state 보존
        _send_report(swing_budget, positions, scan_results, actions)
        return 0

    print(f"\n총 {len(actions)}개 액션:")
    for a in actions:
        print(f"  [{a['action']}] {a['symbol']} {a['qty']}주 — {a['reason']}")

    if not args.execute:
        print("\n※ DRY RUN")
        _persist_position_states(positions)  # B4: 드라이런도 mutated state 보존
        _send_report(swing_budget, positions, scan_results, actions)
        return 0

    if not args.yes and not confirm_execution():
        print("취소됨.")
        return 0

    print("\n주문 전송 중... (B2: fill check via 잔고 차분)")
    results = []
    for i, a in enumerate(actions, 1):
        side_lower = a["action"].lower()
        sym = a["symbol"]
        requested_qty = a["qty"]
        print(f"\n[{i}/{len(actions)}] {a['action']} {sym} {requested_qty}주...")
        try:
            fill = _execute_with_fill_check(
                sym, side_lower, requested_qty, token, wait_seconds=4.0,
            )
            odno = fill["order_id"]
            filled = fill["filled_qty"]
            price = fill["price"]
            status = fill["status"]
            msg = fill["msg"]

            if status == "REJECTED":
                # 미체결 → DB 에 trade 기록 X. 알림.
                print(f"  ❌ REJECTED 주문번호 {odno} — 미체결 (msg: {msg})")
                results.append({
                    **a, "status": "ERROR", "filled_qty": 0,
                    "fill_status": "REJECTED", "error": f"미체결: {msg}",
                })
            elif status == "PARTIAL":
                # 부분 체결 → 실제 체결량만 DB 기록
                print(f"  ⚠️ PARTIAL {filled}/{requested_qty}주 @ {price:,} (주문 {odno})")
                log_trade(sym, side_lower, filled, price, odno,
                          f"PARTIAL {filled}/{requested_qty} | {msg}", a["reason"])
                results.append({
                    **a, "status": "OK", "qty": filled, "filled_qty": filled,
                    "price": price, "fill_status": "PARTIAL",
                })
            else:
                # FILLED — 정상 체결
                print(f"  ✅ FILLED {filled}주 @ {price:,} (주문 {odno})")
                log_trade(sym, side_lower, filled, price, odno, msg, a["reason"])
                results.append({
                    **a, "status": "OK", "qty": filled, "filled_qty": filled,
                    "price": price, "fill_status": "FILLED",
                })
        except kis_api.KISAPIError as e:
            print(f"  ❌ API 오류: {e}")
            results.append({**a, "status": "ERROR", "error": str(e)})
        if i < len(actions):
            time.sleep(0.5)

    ok = sum(1 for r in results if r["status"] == "OK")
    print(f"\n완료: 성공 {ok} / 실패 {len(results)-ok}")
    # B4: 실 주문 결과 반영하여 state 갱신 (청산된 sym clear, 나머지 save)
    _persist_position_states(positions, results)
    _send_report(swing_budget, positions, scan_results, actions, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
