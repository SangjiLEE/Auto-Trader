"""
월간 VAA 리밸런싱 (Vigilant Asset Allocation, Keller-Keuning 2017).

[Path C 결과 기반 신규 라이브 모듈]
- VAA Sharpe 1.54 / Calmar 1.35 / MDD -17.79% (DM 의 Sharpe 1.10 압도)
- 카나리 신호: 모든 offensive 자산 양수 score → best offensive 매수
                하나라도 음수 → best defensive 도피

월간 흐름:
  1. 매월 1~7일 09:05 KST 자동 실행
  2. 최신 13612U momentum score 계산
  3. VAA 룰로 타겟 자산 결정 (1개)
  4. 현재 포지션 vs 타겟 비교 → SELL 현재 / BUY 타겟
  5. B2 fill check 적용 (잔고 차분 reconciliation)

Phase 1 (드라이런): 주문 plan 만 출력
Phase 2 (실제 실행): --execute + 확인 프롬프트 + 자동 일괄 (--yes)

모의투자 (KIS_ENV=paper) 에서만 --execute 허용.

실행:
  python -m src.monthly_vaa                # 드라이런
  python -m src.monthly_vaa --refresh      # 데이터 갱신 후 드라이런
  python -m src.monthly_vaa --execute      # 실 주문 (프롬프트)
  python -m src.monthly_vaa --execute --yes  # 자동 (스케줄용)
"""
from __future__ import annotations

import argparse
import sys
import time

from . import check_balance
from . import check_price
from . import config
from . import db
from . import safety
from . import dual_momentum as dm
from . import kis_api
from . import kis_auth
from . import load_candles
from . import notify
from . import place_order
from . import realized_pnl
from . import strategy_vaa as vaa

KR_SEED_KRW = 50_000_000  # 실현수익률 분모

# VAA universe (3 offensive + 1 defensive)
OFFENSIVE_SYMBOLS = ["069500", "133690", "360750"]
DEFENSIVE_SYMBOLS = ["148070"]
UNIVERSE_SYMBOLS = OFFENSIVE_SYMBOLS + DEFENSIVE_SYMBOLS

ASSET_NAMES = {
    "069500": "KODEX 200 (한국 주식)",
    "133690": "TIGER 미국나스닥100",
    "360750": "TIGER 미국S&P500",
    "148070": "KOSEF 국고채10년 (채권)",
    "CASH": "현금",
}

STRATEGY_TAG = "vaa"


def refresh_data() -> None:
    for sym in UNIVERSE_SYMBOLS:
        load_candles.load_symbol(sym, "KR", years=2)


def compute_current_signal() -> tuple[str, str]:
    """최신 VAA 신호. 반환: (target_asset, signal_date_str)."""
    prices = dm.load_multi_prices(UNIVERSE_SYMBOLS)
    if prices.empty:
        raise RuntimeError("DB 데이터 없음. --refresh 또는 load_candles 먼저.")

    signal = vaa.vaa_signal(prices, OFFENSIVE_SYMBOLS, DEFENSIVE_SYMBOLS)
    if signal.empty:
        raise RuntimeError("시그널 없음 — 데이터 부족 (12개월 lookback 필요)")

    last_date = signal.index[-1]
    target = str(signal.iloc[-1])
    return target, last_date.strftime("%Y-%m-%d")


def fetch_positions(token: str) -> tuple[dict[str, int], int, int]:
    """KIS KR 잔고 → (종목별 수량, 예수금, 총평가)."""
    data = check_balance.fetch_balance(token)
    output1 = data.get("output1", [])
    output2 = (data.get("output2") or [{}])[0]

    positions: dict[str, int] = {}
    for h in output1:
        sym = (h.get("pdno") or "").strip()
        try:
            qty = int(h.get("hldg_qty") or 0)
        except (ValueError, TypeError):
            qty = 0
        if sym and qty > 0:
            positions[sym] = qty

    try:
        cash = int(output2.get("dnca_tot_amt") or 0)
    except (ValueError, TypeError):
        cash = 0
    try:
        total = int(output2.get("tot_evlu_amt") or 0)
    except (ValueError, TypeError):
        total = 0
    return positions, cash, total


def fetch_current_price(symbol: str, token: str) -> int:
    data = check_price.fetch_price(symbol, token)
    price = data.get("output", {}).get("stck_prpr") or "0"
    try:
        return int(price)
    except (ValueError, TypeError):
        return 0


def compute_plan(
    target_asset: str,
    positions: dict[str, int],
    total_equity: int,
    prices: dict[str, int],
    allocation: float = 0.5,  # [2026-04-30 변경] KR 자본 50% (US 슬리브용 50% 확보)
                               # 이전: 1.0 (VAA winner-take-all). 변경 사유:
                               # US Catalyst 비중 5% → 25% 증액으로 자본 분배 재설계
                               # KR 50 + US (NVDA 5 + Faber 15 + Catalyst 25) = 95% + buffer 5%
) -> tuple[list[tuple[str, str, int]], list[str]]:
    """
    현재 포지션 → 타겟 자산으로 변경.

    반환:
      - orders: [(SELL/BUY, symbol, qty), ...]
      - non_universe_warnings: VAA universe 외 종목 경고
    """
    orders: list[tuple[str, str, int]] = []
    non_universe: list[str] = []

    # 1. universe 외 종목 경고 (VAA 가 관리하지 않음)
    for sym in positions:
        if sym not in UNIVERSE_SYMBOLS:
            non_universe.append(sym)

    # 2. CASH 면 모든 universe 종목 청산
    if target_asset == vaa.CASH_LABEL:
        for sym in UNIVERSE_SYMBOLS:
            if positions.get(sym, 0) > 0:
                orders.append(("SELL", sym, positions[sym]))
        return orders, non_universe

    # 3. 타겟 자산 외의 universe 종목 청산
    for sym in UNIVERSE_SYMBOLS:
        if sym != target_asset and positions.get(sym, 0) > 0:
            orders.append(("SELL", sym, positions[sym]))

    # 4. 타겟 자산 비중 조정 (allocation 대비 over/under)
    target_price = prices.get(target_asset, 0)
    if target_price <= 0:
        return orders, non_universe + [f"{target_asset} 시세 조회 실패"]

    target_value = total_equity * allocation
    current_target_value = positions.get(target_asset, 0) * target_price
    delta_value = target_value - current_target_value

    if delta_value > 0:
        # under-allocated: 추가 매수
        buy_qty = int(delta_value / target_price)
        if buy_qty > 0:
            orders.append(("BUY", target_asset, buy_qty))
    elif delta_value < 0:
        # [2026-04-30 신규] over-allocated: 일부 매도 (allocation 50% 변경 대응)
        # 비중 50% 초과 시 초과분 매도 → US 슬리브용 자본 free
        sell_qty = int((-delta_value) / target_price)
        current_qty = positions.get(target_asset, 0)
        sell_qty = min(sell_qty, current_qty)
        # 5% 이내 미세 차이는 무시 (반복 매매 방지)
        threshold_qty = max(int(current_qty * 0.05), 1)
        if sell_qty > threshold_qty:
            orders.append(("SELL", target_asset, sell_qty))

    return orders, non_universe


def confirm_execution() -> bool:
    print("\n" + "!" * 64)
    print("VAA 월간 리밸런싱 주문 실제 전송. y/yes 외엔 취소.")
    print("!" * 64)
    try:
        return input("[y/N]: ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def log_trade(symbol, side, qty, price, order_id, msg):
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO trades
                (symbol, side, quantity, price, executed_at, env, strategy, order_id, notes)
            VALUES (?, ?, ?, ?, datetime('now'), ?, ?, ?, ?)
            """,
            (symbol, side, qty, price, config.KIS_ENV, STRATEGY_TAG, order_id, msg),
        )


def execute_orders(
    orders: list[tuple[str, str, int]],
    prices: dict[str, int],
    token: str,
) -> list[dict]:
    """[B2 fill check 통합] 주문 전후 잔고 차분으로 실제 체결량 확인."""
    safety.assert_paper(label="VAA 매매")

    results: list[dict] = []
    print("\n" + "=" * 64)
    print("VAA 주문 전송 중... (B2: fill check via 잔고 차분)")
    print("=" * 64)

    def query_holdings() -> dict[str, int]:
        h, _, _ = fetch_positions(token)
        return h

    for i, (side, symbol, qty) in enumerate(orders, 1):
        side_ko = "매도" if side == "SELL" else "매수"
        side_param = side.lower()
        price = prices.get(symbol, 0)

        print(f"\n[{i}/{len(orders)}] {side_ko} {symbol} ({ASSET_NAMES.get(symbol, '?')}) {qty}주...")

        try:
            pre = query_holdings()
        except Exception as e:
            print(f"  [B2] 사전 잔고 조회 실패: {e}")
            pre = None
        pre_qty = pre.get(symbol, 0) if pre else 0

        try:
            res = place_order.place_market_order(symbol, qty, side_param, token)
            out = res.get("output", {})
            odno = out.get("ODNO", "?")
            msg = res.get("msg1", "")

            time.sleep(4.0)
            filled_qty = qty
            fill_status = "FILLED"

            if pre is not None:
                try:
                    post = query_holdings()
                    delta = post.get(symbol, 0) - pre_qty
                    actual = delta if side_param == "buy" else -delta
                    filled_qty = max(0, actual)
                    if filled_qty == 0:
                        fill_status = "REJECTED"
                    elif filled_qty < qty:
                        fill_status = "PARTIAL"
                except Exception as e:
                    print(f"  [B2] 사후 잔고 실패: {e} — 요청량 가정")

            if fill_status == "REJECTED":
                print(f"  ❌ REJECTED {odno} ({msg})")
                results.append({
                    "status": "ERROR", "side": side, "symbol": symbol,
                    "qty": qty, "filled_qty": 0, "error": f"REJECTED: {msg}",
                })
            elif fill_status == "PARTIAL":
                print(f"  ⚠️ PARTIAL {filled_qty}/{qty}주 @ {price:,} ({odno})")
                log_trade(symbol, side_param, filled_qty, price, odno,
                          f"VAA PARTIAL {filled_qty}/{qty} | {msg}")
                results.append({
                    "status": "OK", "side": side, "symbol": symbol,
                    "qty": filled_qty, "filled_qty": filled_qty,
                    "price": price, "odno": odno, "msg": msg, "fill_status": "PARTIAL",
                })
            else:
                print(f"  ✅ FILLED {filled_qty}주 @ {price:,} ({odno})")
                log_trade(symbol, side_param, filled_qty, price, odno, f"VAA | {msg}")
                results.append({
                    "status": "OK", "side": side, "symbol": symbol,
                    "qty": filled_qty, "filled_qty": filled_qty,
                    "price": price, "odno": odno, "msg": msg, "fill_status": "FILLED",
                })
        except kis_api.KISAPIError as e:
            print(f"  ❌ API 오류: {e}")
            results.append({
                "status": "ERROR", "side": side, "symbol": symbol,
                "qty": qty, "error": str(e),
            })

        if i < len(orders):
            time.sleep(0.5)

    return results


def print_report(target, signal_date, positions, cash, total, prices, orders, non_universe) -> None:
    mode = "모의투자" if config.KIS_ENV == "paper" else "실거래"

    print("\n" + "=" * 64)
    print(f"VAA 월간 리밸런싱 [{mode}]")
    print("=" * 64)
    print(f"  최신 신호 날짜: {signal_date}")
    print(f"  타겟 자산:      {target} ({ASSET_NAMES.get(target, '?')})")
    print(f"  총 평가금액:    {total:,}원")
    print(f"  예수금:         {cash:,}원")

    print(f"\n[현재 포지션]")
    if not positions:
        print("  (없음)")
    for sym, qty in positions.items():
        if sym in UNIVERSE_SYMBOLS:
            value = qty * prices.get(sym, 0)
            print(f"  {sym} ({ASSET_NAMES.get(sym, '?')}): {qty}주 (≈{value:,}원)")
        else:
            print(f"  {sym}: {qty}주 ⚠️ VAA universe 외")

    if non_universe:
        print(f"\n⚠️ VAA universe 외 종목: {', '.join(non_universe)}")
        print("  (이 종목들은 VAA 가 관리 X. 다른 전략 또는 수동 처리)")

    print(f"\n[주문 계획]")
    if not orders:
        print("  변경 없음 (현재 = 타겟)")
    for side, sym, qty in orders:
        side_ko = "매도" if side == "SELL" else "매수"
        price = prices.get(sym, 0)
        print(f"  {side_ko} {sym} {qty}주 @ ~{price:,}원 (≈{qty*price:,}원)")


def _send_report(target, signal_date, positions, total, orders, results=None):
    if not notify.is_enabled():
        return
    mode = "모의" if config.KIS_ENV == "paper" else "실"
    lines = [
        f"*【경계형 자산배분 (VAA) — 월간 [{mode}]】*",
        f"　신호 날짜: {signal_date}",
        f"　타겟: {target} ({ASSET_NAMES.get(target, '?')})",
        f"　총평가: {total:,}원",
        "",
    ]

    if not orders:
        lines.append("*◾️실행 결과*")
        lines.append("　변경 없음 (타겟 = 현재)")
    elif results is None:
        lines.append("*◾️계획 (드라이런)*")
        for side, sym, qty in orders:
            lines.append(f"　{side} {sym} {qty}주")
    else:
        lines.append("*◾️실행 결과*")
        for r in results:
            status_emoji = "✅" if r.get("status") == "OK" else "❌"
            fill = r.get("fill_status", "")
            lines.append(
                f"　{status_emoji} {r['side']} {r['symbol']} "
                f"{r.get('filled_qty', r.get('qty', '?'))}주 [{fill}]"
            )

    # 전체 실현수익률
    realized, _cur = realized_pnl.realized_for_strategy(STRATEGY_TAG)
    pct = realized_pnl.pct(realized, KR_SEED_KRW)
    lines.append("")
    lines.append("*◾️전체 실현수익률*")
    lines.append(f"　VAA 누적 실현: ₩{int(realized):+,} ({pct:+.2f}%)")
    lines.append(f"　(초기 KR 시드 ₩{KR_SEED_KRW:,} 대비)")

    notify.send("\n".join(lines), channel=notify.CHANNEL_KR_REALTIME)


def main() -> int:
    parser = argparse.ArgumentParser(description="VAA 월간 리밸런싱")
    parser.add_argument("--refresh", action="store_true",
                        help="실행 전 데이터 갱신")
    parser.add_argument("--execute", action="store_true",
                        help="실제 주문 전송 (없으면 드라이런)")
    parser.add_argument("--yes", action="store_true",
                        help="확인 프롬프트 자동 yes (스케줄용)")
    args = parser.parse_args()

    if safety.block_execute_if_real(args.execute):
        return 3

    if args.refresh:
        print("[데이터 갱신]")
        refresh_data()
        print("  완료.\n")

    # 1. 최신 VAA 신호
    target, signal_date = compute_current_signal()
    print(f"VAA 신호: {target} (날짜 {signal_date})")

    # 2. KIS 인증 + 잔고
    try:
        token = kis_auth.get_access_token()
    except Exception as e:
        print(f"[KIS 인증 실패] {e}")
        return 4

    positions, cash, total = fetch_positions(token)

    # 3. universe 종목 가격 + 현재 보유 종목 가격
    prices = {}
    for sym in UNIVERSE_SYMBOLS:
        prices[sym] = fetch_current_price(sym, token)
    for sym in positions:
        if sym not in prices:
            prices[sym] = fetch_current_price(sym, token)

    # 4. 주문 plan
    orders, non_universe = compute_plan(target, positions, total, prices)
    print_report(target, signal_date, positions, cash, total, prices, orders, non_universe)

    if not orders:
        _send_report(target, signal_date, positions, total, orders)
        return 0

    if not args.execute:
        print("\n※ DRY RUN — 실제 주문 X")
        _send_report(target, signal_date, positions, total, orders)
        return 0

    if not args.yes and not confirm_execution():
        print("취소됨.")
        return 0

    results = execute_orders(orders, prices, token)
    ok = sum(1 for r in results if r["status"] == "OK")
    print(f"\n완료: 성공 {ok} / 실패 {len(results)-ok}")
    _send_report(target, signal_date, positions, total, orders, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
