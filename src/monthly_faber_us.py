"""
월간 Faber Multi-Asset US 리밸런싱.

[Path A] 미국 시장 ETF 7-asset 분산 운영.
출처: Mebane Faber, "A Quantitative Approach to TAA" (2007)

룰 (놀라울 정도로 단순):
  매월 말 평가:
    - 자산 종가 > 10개월 SMA  →  보유 (활성)
    - 자산 종가 ≤ 10개월 SMA  →  현금
  활성 자산들에 동일 weight (1/N) 분배

Universe (7-asset, KIS 해외주식 API):
  - SPY  (US 대형주)
  - QQQ  (NASDAQ 100)
  - EFA  (선진국 ex-US)
  - EEM  (신흥국)
  - AGG  (US 종합 채권)
  - IEF  (7-10y Treasury)
  - TIP  (Treasury Inflation Protected)

월간 흐름:
  1. 매월 1~7일 09:10 KST 자동 실행 (VAA 와 5분 간격)
  2. 7-asset 의 10MA 평가
  3. 현재 보유 vs 타겟 weight 비교 → 매도/매수
  4. B2 fill check (잔고 차분)

자본 비중: 총 자본의 15% (NVDA v4 5% + Faber 15% = US 슬리브 20%, 30/70 비율)

실행:
  python -m src.monthly_faber_us              # 드라이런
  python -m src.monthly_faber_us --refresh    # 데이터 갱신 후 드라이런
  python -m src.monthly_faber_us --execute    # 실 주문 (프롬프트)
  python -m src.monthly_faber_us --execute --yes  # 자동 (스케줄용)
"""
from __future__ import annotations

import argparse
import sys
import time

import pandas as pd

from . import check_balance
from . import check_overseas_balance
from . import check_overseas_price
from . import config
from . import db
from . import dual_momentum as dm
from . import kis_api
from . import kis_auth
from . import load_candles
from . import notify
from . import place_overseas_order
from . import strategy_faber as faber

# Universe
UNIVERSE = ["SPY", "QQQ", "EFA", "EEM", "AGG", "IEF", "TIP"]

ASSET_NAMES = {
    "SPY": "SPDR S&P 500",
    "QQQ": "Invesco NASDAQ-100",
    "EFA": "iShares MSCI EAFE (선진국)",
    "EEM": "iShares MSCI Emerging Markets",
    "AGG": "iShares US Aggregate Bond",
    "IEF": "iShares 7-10y Treasury",
    "TIP": "iShares TIPS",
}

ALLOCATION = 0.15           # US 슬리브 = 총 자본의 15%
USD_KRW_ESTIMATE = 1410     # 단순 환율 가정 (정확 환율은 KIS API 별도)
STRATEGY_TAG = "faber_us"
# [#5 개선] 10MA → 12MA (Faber 본인 후속 추천 + 신호 안정성 ↑)
# 12MA 가 5.7년 짧은 데이터에서 노이즈 ↓ (이전 4/30 EFA+EEM → 5/1 SPY+QQQ+EFA 빠른 변경 완화)
MA_MONTHS = 12              # 12MA (이전: 10)
ORDER_PRICE_BUFFER = 0.005  # 시장가 호가 버퍼 (NVDA v3 와 동일)


def refresh_data() -> None:
    for sym in UNIVERSE:
        load_candles.load_symbol(sym, "US", years=2)


def compute_current_weights() -> tuple[dict[str, float], str]:
    """
    최신 Faber 신호 → {symbol: weight} dict.

    반환: (weights, signal_date_str)
      weights[sym] = 1/N (활성 자산) 또는 0 (현금)
    """
    prices = dm.load_multi_prices(UNIVERSE)
    if prices.empty:
        raise RuntimeError("DB 데이터 없음. --refresh 필요.")

    signal = faber.faber_signal(prices, ma_months=MA_MONTHS)
    if signal.empty:
        raise RuntimeError(f"시그널 없음 — {MA_MONTHS}개월 데이터 부족")

    last_signal = signal.iloc[-1]
    n_active = float(last_signal.sum())
    if n_active == 0:
        weights = {sym: 0.0 for sym in UNIVERSE}
    else:
        w = 1.0 / n_active
        weights = {sym: (w if last_signal.get(sym, 0) > 0 else 0.0) for sym in UNIVERSE}

    return weights, signal.index[-1].strftime("%Y-%m-%d")


def fetch_kr_total_equity(token: str) -> int:
    """총 평가 (KRW) 조회 — 자본 비중 계산용."""
    data = check_balance.fetch_balance(token)
    output2 = (data.get("output2") or [{}])[0]
    try:
        return int(output2.get("tot_evlu_amt") or 0)
    except (ValueError, TypeError):
        return 0


def fetch_us_holdings(token: str) -> dict[str, dict]:
    """KIS US 잔고 → {symbol: {qty, avg, exchange}}."""
    holdings = check_overseas_balance.fetch_all_us_holdings(token)
    result = {}
    for h in holdings:
        sym = h["symbol"]
        if sym in UNIVERSE:
            result[sym] = {
                "qty": h["qty"],
                "avg_price_usd": h["avg_price_usd"],
                "current_price_usd": h["current_price_usd"],
                "exchange": h["exchange"],
            }
    return result


def fetch_us_prices(token: str) -> dict[str, float]:
    """Universe 종목 현재가 (USD)."""
    prices: dict[str, float] = {}
    for sym in UNIVERSE:
        try:
            data = check_overseas_price.fetch_price(sym, token)
            price = float(data.get("output", {}).get("last") or 0)
            if price > 0:
                prices[sym] = price
        except Exception as e:
            print(f"  [경고] {sym} 시세 조회 실패: {e}")
    return prices


def compute_plan(
    weights: dict[str, float],
    holdings: dict[str, dict],
    prices: dict[str, float],
    target_total_usd: float,
) -> list[tuple[str, str, int]]:
    """
    현재 보유 + 타겟 weight → 매매 계획.

    반환: [(SELL/BUY, symbol, qty), ...]
    """
    orders: list[tuple[str, str, int]] = []

    for sym in UNIVERSE:
        target_w = weights.get(sym, 0.0)
        target_value = target_total_usd * target_w
        price = prices.get(sym, 0.0)
        if price <= 0:
            continue
        target_qty = int(target_value / price) if target_w > 0 else 0
        current_qty = holdings.get(sym, {}).get("qty", 0)

        delta = target_qty - current_qty
        if delta > 0:
            orders.append(("BUY", sym, delta))
        elif delta < 0:
            orders.append(("SELL", sym, -delta))

    return orders


def confirm_execution() -> bool:
    print("\n" + "!" * 64)
    print("Faber Multi-Asset US 주문 실제 전송. y/yes 외엔 취소.")
    print("!" * 64)
    try:
        return input("[y/N]: ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


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


def execute_orders(
    orders: list[tuple[str, str, int]],
    prices: dict[str, float],
    token: str,
) -> list[dict]:
    """[B2 fill check 통합] 미국 ETF 매매 + 잔고 차분 reconciliation."""
    if config.KIS_ENV != "paper":
        raise RuntimeError("실거래 모드 차단. KIS_ENV=paper 확인.")

    results: list[dict] = []
    print("\n" + "=" * 64)
    print("Faber US 주문 전송 중... (B2: fill check via US 잔고 차분)")
    print("=" * 64)

    def query() -> dict[str, int]:
        h = check_overseas_balance.fetch_all_us_holdings(token)
        return {x["symbol"]: x["qty"] for x in h}

    for i, (side, sym, qty) in enumerate(orders, 1):
        side_ko = "매도" if side == "SELL" else "매수"
        side_param = side.lower()
        price = prices.get(sym, 0.0)

        print(f"\n[{i}/{len(orders)}] {side_ko} {sym} ({ASSET_NAMES.get(sym, '?')}) {qty}주...")

        try:
            pre = query()
        except Exception as e:
            print(f"  [B2] 사전 잔고 실패: {e}")
            pre = None
        pre_qty = pre.get(sym, 0) if pre else 0

        try:
            res = place_overseas_order.place_market_like_order(
                sym, qty, side_param, token, buffer=ORDER_PRICE_BUFFER
            )
            out = res.get("output", {})
            odno = out.get("ODNO", "?")
            msg = res.get("msg1", "")

            time.sleep(6.0)
            filled_qty = qty
            fill_status = "FILLED"

            if pre is not None:
                try:
                    post = query()
                    delta = post.get(sym, 0) - pre_qty
                    actual = delta if side_param == "buy" else -delta
                    filled_qty = max(0, actual)
                    if filled_qty == 0:
                        fill_status = "REJECTED"
                    elif filled_qty < qty:
                        fill_status = "PARTIAL"
                except Exception as e:
                    print(f"  [B2] 사후 잔고 실패: {e}")

            if fill_status == "REJECTED":
                print(f"  ❌ REJECTED {odno} ({msg})")
                results.append({
                    "status": "ERROR", "side": side, "symbol": sym,
                    "qty": qty, "filled_qty": 0, "error": f"REJECTED: {msg}",
                })
            elif fill_status == "PARTIAL":
                print(f"  ⚠️ PARTIAL {filled_qty}/{qty}주 @ ${price:.2f} ({odno})")
                log_trade(sym, side_param, filled_qty, price, odno,
                          f"Faber PARTIAL {filled_qty}/{qty} | {msg}")
                results.append({
                    "status": "OK", "side": side, "symbol": sym,
                    "qty": filled_qty, "price": price, "fill_status": "PARTIAL",
                })
            else:
                print(f"  ✅ FILLED {filled_qty}주 @ ${price:.2f} ({odno})")
                log_trade(sym, side_param, filled_qty, price, odno, f"Faber | {msg}")
                results.append({
                    "status": "OK", "side": side, "symbol": sym,
                    "qty": filled_qty, "price": price, "fill_status": "FILLED",
                })
        except (kis_api.KISAPIError, RuntimeError) as e:
            print(f"  ❌ API 오류: {e}")
            results.append({"status": "ERROR", "side": side, "symbol": sym,
                            "qty": qty, "error": str(e)})

        if i < len(orders):
            time.sleep(0.5)

    return results


def print_report(weights, signal_date, holdings, total_usd, prices, orders) -> None:
    mode = "모의투자" if config.KIS_ENV == "paper" else "실거래"

    print("\n" + "=" * 64)
    print(f"Faber Multi-Asset US [{mode}]")
    print("=" * 64)
    print(f"  최신 신호 날짜: {signal_date}")
    print(f"  US 슬리브 자본: ${total_usd:,.2f}")

    n_active = sum(1 for w in weights.values() if w > 0)
    print(f"  활성 자산: {n_active}개 / {len(UNIVERSE)} (각 {1/max(n_active,1)*100:.1f}% weight)")

    print("\n[현재 보유]")
    if not holdings:
        print("  (없음)")
    for sym, h in holdings.items():
        print(f"  {sym}: {h['qty']}주 @ ${h['avg_price_usd']:.2f}")

    print("\n[Faber 신호 (10MA)]")
    for sym in UNIVERSE:
        w = weights.get(sym, 0)
        flag = "✅ 활성" if w > 0 else "❌ 현금"
        price = prices.get(sym, 0)
        print(f"  {sym} {ASSET_NAMES.get(sym, ''):<35} {flag}  현재 ${price:.2f}")

    print("\n[주문 계획]")
    if not orders:
        print("  변경 없음 (현재 = 타겟)")
    for side, sym, qty in orders:
        side_ko = "매도" if side == "SELL" else "매수"
        price = prices.get(sym, 0)
        print(f"  {side_ko} {sym} {qty}주 @ ~${price:.2f} = ~${qty*price:.2f}")


def _send_report(weights, signal_date, holdings, total_usd, orders, results=None):
    if not notify.is_enabled():
        return
    mode = "모의" if config.KIS_ENV == "paper" else "실"
    n_active = sum(1 for w in weights.values() if w > 0)
    active_syms = [s for s, w in weights.items() if w > 0]

    lines = [
        f"📊 추세 추종 분산 (Faber US) — 월간 [{mode}]",
        f"신호 날짜: {signal_date}",
        f"활성 자산 ({n_active}/7): {', '.join(active_syms) if active_syms else '없음 (현금)'}",
        f"US 슬롯: ${total_usd:,.2f}",
        "",
    ]
    if not orders:
        lines.append("변경 없음 (타겟 = 현재)")
    elif results is None:
        lines.append("[Plan]")
        for side, sym, qty in orders:
            lines.append(f"  {side} {sym} {qty}주")
    else:
        lines.append("[실행 결과]")
        for r in results:
            status_emoji = "✅" if r.get("status") == "OK" else "❌"
            fill = r.get("fill_status", "")
            lines.append(
                f"  {status_emoji} {r['side']} {r['symbol']} "
                f"{r.get('qty', '?')}주 [{fill}]"
            )
    notify.send("\n".join(lines), channel=notify.CHANNEL_US_REALTIME)


def main() -> int:
    parser = argparse.ArgumentParser(description="Faber Multi-Asset US 월간")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    if args.execute and config.KIS_ENV != "paper":
        print(f"[차단] KIS_ENV={config.KIS_ENV} — 실거래 차단.")
        return 3

    if args.refresh:
        print("[데이터 갱신]")
        refresh_data()
        print("  완료.\n")

    weights, signal_date = compute_current_weights()

    try:
        token = kis_auth.get_access_token()
    except Exception as e:
        print(f"[KIS 인증 실패] {e}")
        return 4

    # KR 총평가 (USD 환산용)
    total_krw = fetch_kr_total_equity(token)
    sleeve_krw = total_krw * ALLOCATION
    sleeve_usd = sleeve_krw / USD_KRW_ESTIMATE
    print(f"\n총평가 (KR 기준): ₩{total_krw:,} | Faber US 슬롯: ${sleeve_usd:,.2f}")

    # 현재 US 보유
    holdings = fetch_us_holdings(token)
    prices = fetch_us_prices(token)

    orders = compute_plan(weights, holdings, prices, sleeve_usd)
    print_report(weights, signal_date, holdings, sleeve_usd, prices, orders)

    if not orders:
        _send_report(weights, signal_date, holdings, sleeve_usd, orders)
        return 0

    if not args.execute:
        print("\n※ DRY RUN — 실제 주문 X")
        _send_report(weights, signal_date, holdings, sleeve_usd, orders)
        return 0

    if not args.yes and not confirm_execution():
        print("취소됨.")
        return 0

    results = execute_orders(orders, prices, token)
    ok = sum(1 for r in results if r["status"] == "OK")
    print(f"\n완료: 성공 {ok} / 실패 {len(results)-ok}")
    _send_report(weights, signal_date, holdings, sleeve_usd, orders, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
