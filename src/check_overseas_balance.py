"""
해외주식 잔고 조회.

KIS 해외주식 잔고는 거래소별로 분리 조회 (NASD/NYSE/AMEX).
세 거래소 순회해서 통합 출력.

실행:
  python -m src.check_overseas_balance
"""
from __future__ import annotations

import sys

from . import config
from . import kis_api
from . import kis_auth
from . import kis_overseas

_ENDPOINT = "/uapi/overseas-stock/v1/trading/inquire-balance"


def fetch_balance(token: str, exchange: str = "NASD") -> dict:
    params = {
        "CANO": config.CANO,
        "ACNT_PRDT_CD": config.ACNT_PRDT_CD,
        "OVRS_EXCG_CD": exchange,
        "TR_CRCY_CD": "USD",
        "CTX_AREA_FK200": "",
        "CTX_AREA_NK200": "",
    }
    return kis_api.get(
        _ENDPOINT,
        tr_id=kis_overseas.balance_tr_id(),
        token=token,
        params=params,
    )


def fetch_all_us_holdings(token: str) -> list[dict]:
    """3개 거래소 순회 → 모든 미국주식 보유 통합 리스트."""
    holdings = []
    for exchange in kis_overseas.US_EXCHANGES:
        try:
            data = fetch_balance(token, exchange)
            output1 = data.get("output1", [])
            for h in output1:
                qty_str = h.get("ovrs_cblc_qty", "0") or "0"
                try:
                    qty = int(float(qty_str))
                except (ValueError, TypeError):
                    qty = 0
                if qty > 0:
                    holdings.append({
                        "symbol": (h.get("ovrs_pdno") or "").strip(),
                        "name": h.get("ovrs_item_name", ""),
                        "exchange": exchange,
                        "qty": qty,
                        "avg_price_usd": float(h.get("pchs_avg_pric") or 0),
                        "current_price_usd": float(h.get("now_pric2") or 0),
                        "eval_amount_usd": float(h.get("ovrs_stck_evlu_amt") or 0),
                        "pnl_usd": float(h.get("frcr_evlu_pfls_amt") or 0),
                    })
        except kis_api.KISAPIError as e:
            print(f"[{exchange}] 조회 실패: {e}")
    return holdings


def main() -> int:
    try:
        config.validate()
    except ValueError as e:
        print(f"[설정 오류] {e}")
        return 1

    try:
        token = kis_auth.get_access_token()
    except kis_auth.KISAuthError as e:
        print(f"[인증 실패] {e}")
        return 2

    mode = "모의" if config.KIS_ENV == "paper" else "실거래"
    print("=" * 60)
    print(f"해외주식 잔고 ({mode})")
    print("=" * 60)
    print(f"계좌: {config.CANO}-{config.ACNT_PRDT_CD}\n")

    holdings = fetch_all_us_holdings(token)

    if not holdings:
        print("보유 미국주식 없음.\n")
    else:
        total_usd = 0.0
        total_pnl = 0.0
        print(f"{'종목':<8} {'거래소':<6} {'수량':>6} "
              f"{'평단(USD)':>11} {'현재가':>10} {'평가':>13} {'손익':>13}")
        print("-" * 75)
        for h in holdings:
            print(
                f"{h['symbol']:<8} {h['exchange']:<6} {h['qty']:>6} "
                f"${h['avg_price_usd']:>10,.2f} "
                f"${h['current_price_usd']:>9,.2f} "
                f"${h['eval_amount_usd']:>12,.2f} "
                f"${h['pnl_usd']:>+12,.2f}"
            )
            total_usd += h["eval_amount_usd"]
            total_pnl += h["pnl_usd"]
        print("-" * 75)
        print(f"  합계: 평가 ${total_usd:,.2f}  손익 ${total_pnl:+,.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
