"""
월간 리밸런싱 (Dual Momentum).

Phase 1 (드라이런): 주문 plan 출력만
Phase 2 (실제 실행): --execute 플래그 + 확인 프롬프트 후 주문 전송

모의투자 환경(KIS_ENV=paper)에서만 --execute 허용.
모든 주문은 DB trades 테이블에 로그.

실행:
  python -m src.monthly_rebalance              # 드라이런
  python -m src.monthly_rebalance --refresh    # 데이터 갱신 후 드라이런
  python -m src.monthly_rebalance --execute    # 실제 주문 (확인 프롬프트 있음)
  python -m src.monthly_rebalance --refresh --execute  # 전부 자동화 단일 명령
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

# 매매 가능 유니버스 (KIS 국내주식 API로 거래되는 ETF)
UNIVERSE_SYMBOLS = ["069500", "133690", "360750", "148070"]

ASSET_NAMES = {
    "069500": "KODEX 200 (한국 주식)",
    "133690": "TIGER 미국나스닥100",
    "360750": "TIGER 미국S&P500",
    "148070": "KOSEF 국고채10년 (채권)",
    "CASH": "현금",
}


# ── 데이터 갱신 ───────────────────────────────────────

def refresh_data() -> None:
    """DB에 최신 일봉 수집 (최근 2년)."""
    for sym in UNIVERSE_SYMBOLS:
        load_candles.load_symbol(sym, "KR", years=2)


# ── 시그널 ────────────────────────────────────────────

def compute_current_signal(lookback: int = 12) -> tuple[str, str]:
    """최신 DM 시그널. 반환: (target_asset, signal_date_str).

    [B1 fix] dual_momentum_signal(complete_only=True) 사용으로 부분
    현재월 bucket 자동 제외. 추가 검증:
      - 신호 날짜가 직전 완료 월말인지 확인
      - 60일 이상 stale 이면 경고 (데이터 갱신 누락 의심)
    """
    prices = dm.load_multi_prices(UNIVERSE_SYMBOLS)
    if prices.empty:
        raise RuntimeError("DB 데이터 없음. --refresh 옵션 또는 load_candles 실행 필요")

    signal = dm.dual_momentum_signal(prices, lookback, complete_only=True)
    if signal.empty:
        raise RuntimeError(
            "시그널 없음 — 완료된 월 데이터 부족 (lookback 12개월 + 현재월 제외 후)"
        )

    last_date = signal.index[-1]
    target = str(signal.iloc[-1])

    # 신호 staleness 검증
    today = pd.Timestamp.today().normalize()
    days_old = (today - last_date).days
    if days_old > 60:
        print(
            f"⚠️  [B1] 신호가 오래됨 ({days_old}일 전, {last_date.date()}). "
            "데이터 갱신 필요 가능성. --refresh 검토."
        )
    elif days_old < 1:
        # complete_only=True 면 발생 안 해야 함. 안전장치.
        raise RuntimeError(
            f"신호 날짜가 오늘 이후 ({last_date.date()}) — 미완료 bucket 의심"
        )

    return target, last_date.strftime("%Y-%m-%d")


# ── 현재 포지션 ───────────────────────────────────────

def fetch_positions(token: str) -> tuple[dict[str, int], int, int]:
    """KIS 국내주식 잔고 → (종목별 수량, 예수금, 총평가금액)."""
    data = check_balance.fetch_balance(token)
    output1 = data.get("output1", [])
    output2_list = data.get("output2", [])
    output2 = output2_list[0] if output2_list else {}

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
    """종목 현재가 (원). 장외시간엔 직전 종가."""
    data = check_price.fetch_price(symbol, token)
    price = data.get("output", {}).get("stck_prpr") or "0"
    try:
        return int(price)
    except (ValueError, TypeError):
        return 0


# ── 주문 계획 ─────────────────────────────────────────

def compute_plan(
    target_asset: str,
    positions: dict[str, int],
    total_equity: int,
    prices: dict[str, int],
    allocation: float = 0.70,
) -> tuple[list[tuple[str, str, int]], list[str]]:
    """
    타겟 자산 + 현재 상태 → 주문 plan.

    규칙:
      1. 유니버스 내 타겟 아닌 종목은 전량 매도
      2. 총 평가금액(total_equity) × 98% 를 예산으로 타겟 보유량 계산
         (dnca_tot_amt 는 T+2 미정산 포함이라 이중계산 위험)
      3. 유니버스 밖 종목은 건드리지 않음 (경고)

    반환: (orders, non_universe_warnings)
    """
    orders: list[tuple[str, str, int]] = []
    non_universe = [s for s in positions if s not in UNIVERSE_SYMBOLS]

    for sym in UNIVERSE_SYMBOLS:
        qty = positions.get(sym, 0)
        if qty == 0:
            continue
        if sym != target_asset or target_asset == "CASH":
            orders.append(("SELL", sym, qty))

    if target_asset == "CASH":
        return orders, non_universe

    target_price = prices.get(target_asset, 0)
    if target_price <= 0:
        return orders, non_universe

    # 전체 평가금액 × allocation (DM 배정 비율) × 98% (수수료/슬리피지 버퍼)
    # allocation 0.70 = DM 70% (나머지 30%는 Swing slot 용)
    # allocation 1.00 = 순수 DM 단독
    usable = int(total_equity * allocation * 0.98)
    target_qty = usable // target_price
    current_qty = positions.get(target_asset, 0)
    diff = target_qty - current_qty

    if diff > 0:
        orders.append(("BUY", target_asset, diff))
    elif diff < 0:
        orders.append(("SELL", target_asset, -diff))

    return orders, non_universe


# ── 실행 (Phase 2) ────────────────────────────────────

def confirm_execution() -> bool:
    """Y/N 프롬프트. 기본 N."""
    print("\n" + "!" * 64)
    print("주문을 실제로 KIS 모의투자 계좌에 전송하려 합니다.")
    print("취소하려면 Enter 또는 n. 진행하려면 y 또는 yes.")
    print("!" * 64)
    try:
        response = input("진행할까요? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return response in ("y", "yes")


def log_trade(
    symbol: str, side: str, qty: int, price: int, order_id: str, msg: str
) -> None:
    """trades 테이블에 로그."""
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO trades
                (symbol, side, quantity, price, executed_at, env, strategy, order_id, notes)
            VALUES (?, ?, ?, ?, datetime('now'), ?, ?, ?, ?)
            """,
            (symbol, side, qty, price, config.KIS_ENV, "dual_momentum", order_id, msg),
        )


def _query_holdings(token: str) -> dict[str, int]:
    """[B2 fix] KR 잔고 → {symbol: qty}."""
    holdings, _, _ = fetch_positions(token)
    return holdings


def execute_orders(
    orders: list[tuple[str, str, int]],
    prices: dict[str, int],
    token: str,
) -> list[dict]:
    """주문 실제 전송. paper 모드에서만.

    [B2 fix] 주문 전후 잔고 차분으로 실제 체결량 검증.
    부분 체결 / 거부 시 정확한 qty 만 DB 기록 → DB-broker 동기 유지.
    """
    safety.assert_paper(label="DM 월간 리밸런스")

    results: list[dict] = []
    print("\n" + "=" * 64)
    print("주문 전송 중... (B2: fill check via 잔고 차분)")
    print("=" * 64)

    for i, (side, symbol, qty) in enumerate(orders, 1):
        side_ko = "매도" if side == "SELL" else "매수"
        side_param = side.lower()  # place_order expects 'buy'/'sell'
        price = prices.get(symbol, 0)

        print(f"\n[{i}/{len(orders)}] {side_ko} {symbol} {qty}주...")

        # B2: 사전 잔고
        try:
            pre_holdings = _query_holdings(token)
        except Exception as e:
            print(f"  [B2] 사전 잔고 조회 실패: {e} — fill check 스킵")
            pre_holdings = None
        pre_qty = pre_holdings.get(symbol, 0) if pre_holdings else 0

        try:
            result = place_order.place_market_order(
                symbol, qty, side_param, token
            )
            output = result.get("output", {})
            odno = output.get("ODNO", "?")
            msg = result.get("msg1", "")

            # B2: 체결 대기 + 사후 잔고
            time.sleep(4.0)
            filled_qty = qty  # 기본값 (잔고 조회 실패 시 fallback)
            fill_status = "FILLED"

            if pre_holdings is not None:
                try:
                    post_holdings = _query_holdings(token)
                    post_qty = post_holdings.get(symbol, 0)
                    delta = post_qty - pre_qty
                    actual = delta if side_param == "buy" else -delta
                    if actual < 0:
                        actual = 0
                    filled_qty = actual
                    if filled_qty == 0:
                        fill_status = "REJECTED"
                    elif filled_qty < qty:
                        fill_status = "PARTIAL"
                    else:
                        fill_status = "FILLED"
                except Exception as e:
                    print(f"  [B2] 사후 잔고 조회 실패: {e} — 요청량 가정")

            if fill_status == "REJECTED":
                print(f"  ❌ REJECTED 주문번호 {odno} — 미체결 (msg: {msg})")
                results.append({
                    "status": "ERROR", "side": side, "symbol": symbol,
                    "qty": qty, "filled_qty": 0,
                    "error": f"REJECTED: {msg}", "odno": odno,
                })
            elif fill_status == "PARTIAL":
                print(f"  ⚠️ PARTIAL {filled_qty}/{qty}주 @ {price:,} (주문 {odno})")
                log_trade(symbol, side_param, filled_qty, price, odno,
                          f"PARTIAL {filled_qty}/{qty} | {msg}")
                results.append({
                    "status": "OK", "side": side, "symbol": symbol,
                    "qty": filled_qty, "filled_qty": filled_qty,
                    "price": price, "odno": odno, "msg": msg,
                    "fill_status": "PARTIAL",
                })
            else:
                print(f"  ✅ FILLED {filled_qty}주 @ {price:,} (주문 {odno})")
                log_trade(symbol, side_param, filled_qty, price, odno, msg)
                results.append({
                    "status": "OK", "side": side, "symbol": symbol,
                    "qty": filled_qty, "filled_qty": filled_qty,
                    "price": price, "odno": odno, "msg": msg,
                    "fill_status": "FILLED",
                })

        except kis_api.KISAPIError as e:
            print(f"  ❌ API 오류: {e}")
            results.append({
                "status": "ERROR", "side": side, "symbol": symbol,
                "qty": qty, "error": str(e),
            })

        # 레이트리밋 여유 (KIS는 주문 API 초당 몇 건 제한)
        if i < len(orders):
            time.sleep(0.5)

    return results


# ── 출력 ──────────────────────────────────────────────

def _fmt_won(x: int) -> str:
    return f"{x:,}"


def print_report(
    target: str,
    signal_date: str,
    positions: dict[str, int],
    cash: int,
    total: int,
    prices: dict[str, int],
    orders: list[tuple[str, str, int]],
    non_universe: list[str],
) -> None:
    mode = "모의투자" if config.KIS_ENV == "paper" else "실거래"
    target_label = ASSET_NAMES.get(target, target)

    print("\n" + "=" * 64)
    print("리밸런싱 계획")
    print("=" * 64)
    print(f"환경           : {config.KIS_ENV} ({mode})")
    print(f"시그널 기준일  : {signal_date}")
    print(f"이번 달 타겟   : {target}  ({target_label})")

    print("\n─ 현재 포지션 ─")
    print(f"  예수금      : {_fmt_won(cash):>18} 원")
    print(f"  총 평가금액 : {_fmt_won(total):>18} 원")
    if positions:
        for sym, qty in positions.items():
            price = prices.get(sym, 0)
            value = qty * price
            marker = " (유니버스 외)" if sym not in UNIVERSE_SYMBOLS else ""
            print(
                f"  {sym:<8} {qty:>6}주 x {_fmt_won(price):>8}원 "
                f"= {_fmt_won(value):>13}원{marker}"
            )
    else:
        print("  (보유 종목 없음)")

    print("\n─ 주문 Plan ─")
    if not orders:
        print("  (변경 불필요 - 이미 타겟 상태)")
    else:
        for side, sym, qty in orders:
            price = prices.get(sym, 0)
            amount = qty * price
            label = "매도" if side == "SELL" else "매수"
            name = ASSET_NAMES.get(sym, sym)
            print(
                f"  [{label}] {sym:<8} {qty:>6}주 @ {_fmt_won(price):>8}원 "
                f"= {_fmt_won(amount):>13}원  ({name})"
            )

    if non_universe:
        print("\n[경고] 유니버스 밖 보유 종목 (건드리지 않음):")
        for sym in non_universe:
            print(f"  - {sym}: {positions.get(sym, 0)}주")


# ── 메인 ──────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="월간 리밸런싱")
    parser.add_argument(
        "--refresh", action="store_true", help="DB 최신 갱신 후 실행"
    )
    parser.add_argument(
        "--lookback", type=int, default=12, help="DM 룩백 개월 (기본 12)"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="실제 주문 전송 (모의투자만, 확인 프롬프트 있음)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="확인 프롬프트 건너뛰기 (자동 스케줄용). 수동 실행 시 쓰지 말 것.",
    )
    parser.add_argument(
        "--allocation",
        type=float,
        default=0.70,
        help="DM 자본 배정 비율. 0.70=v3 멀티 전략 (기본, v3 KR+US 30% 남김), 1.00=순수 DM.",
    )
    args = parser.parse_args()

    print("=" * 64)
    mode_label = "실제 실행" if args.execute else "드라이런"
    print(f"월간 리밸런싱 ({mode_label})")
    print("=" * 64)

    if args.refresh:
        print("\n[준비] 데이터 갱신 중...")
        try:
            refresh_data()
        except Exception as e:
            print(f"[갱신 실패] {e}")
            return 1

    # 1. 시그널
    print("\n[1/4] DM 시그널 계산...")
    try:
        target, signal_date = compute_current_signal(args.lookback)
    except RuntimeError as e:
        print(f"  [실패] {e}")
        return 2
    print(f"  → {target}  (기준일 {signal_date})")

    # 2. KIS 인증 + 잔고
    print("\n[2/4] KIS 인증 + 잔고 조회...")
    try:
        config.validate()
    except ValueError as e:
        print(f"  [설정 오류] {e}")
        return 3
    try:
        token = kis_auth.get_access_token()
    except kis_auth.KISAuthError as e:
        print(f"  [인증 실패] {e}")
        return 4
    try:
        positions, cash, total = fetch_positions(token)
    except kis_api.KISAPIError as e:
        print(f"  [잔고 조회 실패] {e}")
        return 5
    print(f"  → 예수금 {_fmt_won(cash)}원, {len(positions)}개 종목")

    # 3. 시세
    print("\n[3/4] 시세 조회...")
    symbols_to_price = set()
    if target != "CASH":
        symbols_to_price.add(target)
    for sym in positions:
        if sym in UNIVERSE_SYMBOLS:
            symbols_to_price.add(sym)

    prices: dict[str, int] = {}
    price_failures: list[str] = []
    for sym in symbols_to_price:
        try:
            prices[sym] = fetch_current_price(sym, token)
            print(f"  {sym}: {_fmt_won(prices[sym])}원")
        except kis_api.KISAPIError as e:
            print(f"  [시세 실패] {sym}: {e}")
            prices[sym] = 0
            price_failures.append(sym)

    # 타겟 자산 시세를 못 받았으면 plan 생성 불가. 조기 중단.
    if target != "CASH" and prices.get(target, 0) <= 0:
        print(
            f"\n[중단] 타겟 자산 {target} 시세 조회 실패 — plan 생성 불가."
        )
        print("1~2분 뒤 다시 실행하세요: python -m src.monthly_rebalance --execute")
        return 7

    # 4. 계획
    print(f"\n[4/4] 주문 계획 생성 (DM 할당 {args.allocation*100:.0f}%)...")
    orders, non_universe = compute_plan(
        target, positions, total, prices, allocation=args.allocation
    )
    print_report(
        target, signal_date, positions, cash, total, prices, orders, non_universe
    )

    # 5. 실행 (--execute 시)
    if args.execute:
        if not orders:
            print("\n변경 없음. 실행 생략.")
            return 0
        if safety.block_execute_if_real(args.execute):
            return 6
        if args.yes:
            print("\n[--yes] 확인 프롬프트 건너뜀. 자동 진행.")
        elif not confirm_execution():
            print("\n취소됨.")
            return 0

        try:
            results = execute_orders(orders, prices, token)
        except RuntimeError as e:
            print(f"\n[실행 실패] {e}")
            return 7

        ok = sum(1 for r in results if r["status"] == "OK")
        fail = len(results) - ok
        print("\n" + "=" * 64)
        print(f"실행 완료: 성공 {ok} / 실패 {fail}")
        print("=" * 64)
        print("\n체결 확인: python -m src.check_balance")
        print("주문 로그: sqlite3 data.db 'SELECT * FROM trades ORDER BY id DESC LIMIT 5'")

        # Telegram 알림 (설정 시)
        if notify.is_enabled():
            summary_lines = []
            for r in results:
                if r["status"] == "OK":
                    summary_lines.append(
                        f"{r['side']} {r['symbol']} {r['qty']}주 "
                        f"@ {r['price']:,}원  (주문 {r['odno']})"
                    )
                else:
                    summary_lines.append(
                        f"FAIL {r['side']} {r['symbol']} {r['qty']}주: "
                        f"{r.get('error', '')}"
                    )
            summary = "\n".join(summary_lines)
            if fail == 0:
                notify.notify_rebalance_success(
                    config.KIS_ENV, ok, f"타겟: {target}\n\n{summary}"
                )
            else:
                notify.notify_rebalance_failure(
                    config.KIS_ENV,
                    f"성공 {ok} / 실패 {fail}\n\n{summary}",
                )

        return 0 if fail == 0 else 8

    # 드라이런 종료
    print("\n" + "=" * 64)
    print("※ DRY RUN - 실제 주문 전송 안 됨")
    print("※ 실제 실행: python -m src.monthly_rebalance --execute")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
