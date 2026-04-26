"""
Sprint 1 Week 1 (2): 계좌 잔고 조회.

KIS Open API로 국내주식 잔고를 조회해서 예수금과 보유 종목을 출력.
인증된 GET 요청이 정상 동작하는지 검증하는 단계.

실행:
  python -m src.check_balance
"""
from __future__ import annotations

import sys

from . import config
from . import kis_api
from . import kis_auth

_ENDPOINT = "/uapi/domestic-stock/v1/trading/inquire-balance"


def _tr_id() -> str:
    # 모의투자는 V로 시작, 실거래는 T로 시작
    return "VTTC8434R" if config.KIS_ENV == "paper" else "TTTC8434R"


def fetch_balance(token: str) -> dict:
    params = {
        "CANO": config.CANO,
        "ACNT_PRDT_CD": config.ACNT_PRDT_CD,
        "AFHR_FLPR_YN": "N",          # 시간외단일가 여부
        "OFL_YN": "",                  # 공란
        "INQR_DVSN": "01",             # 01: 대출일별, 02: 종목별
        "UNPR_DVSN": "01",             # 단가구분 (01 고정)
        "FUND_STTL_ICLD_YN": "N",      # 펀드결제분 포함 여부
        "FNCG_AMT_AUTO_RDPT_YN": "N",  # 융자금액자동상환 여부
        "PRCS_DVSN": "01",             # 처리구분
        "CTX_AREA_FK100": "",          # 연속조회 (최초 공란)
        "CTX_AREA_NK100": "",
    }
    return kis_api.get(_ENDPOINT, tr_id=_tr_id(), token=token, params=params)


def _fmt_won(value: str | int | None) -> str:
    """'1234567' → '1,234,567'."""
    try:
        return f"{int(value or 0):,}"
    except (ValueError, TypeError):
        return str(value)


def main() -> int:
    print("=" * 60)
    print("계좌 잔고 조회")
    print("=" * 60)

    try:
        config.validate()
    except ValueError as e:
        print(f"[설정 오류] {e}")
        return 1

    mode = "모의투자" if config.KIS_ENV == "paper" else "실거래"
    print(f"환경: {config.KIS_ENV} ({mode})")
    print(f"계좌: {config.CANO}-{config.ACNT_PRDT_CD}")
    print()

    print("토큰 준비 중... (캐시 재사용 시 즉시)")
    try:
        token = kis_auth.get_access_token()
    except kis_auth.KISAuthError as e:
        print(f"[인증 실패] {e}")
        return 2
    print("토큰 OK")
    print()

    print("잔고 조회 중...")
    try:
        data = fetch_balance(token)
    except kis_api.KISAPIError as e:
        print(f"[조회 실패] {e}")
        return 3
    print("조회 완료")
    print()

    # output2: 계좌 요약 (리스트로 반환, 보통 원소 1개)
    summary_list = data.get("output2", [])
    summary = summary_list[0] if summary_list else {}

    print("-" * 60)
    print("계좌 요약")
    print("-" * 60)
    print(f"  예수금 총금액       : {_fmt_won(summary.get('dnca_tot_amt')):>18} 원")
    print(f"  익일 정산 예수금    : {_fmt_won(summary.get('nxdy_excc_amt')):>18} 원")
    print(f"  주식 평가금액       : {_fmt_won(summary.get('scts_evlu_amt')):>18} 원")
    print(f"  매입금액 합계       : {_fmt_won(summary.get('pchs_amt_smtl_amt')):>18} 원")
    print(f"  평가손익 합계       : {_fmt_won(summary.get('evlu_pfls_smtl_amt')):>18} 원")
    print(f"  총 평가금액         : {_fmt_won(summary.get('tot_evlu_amt')):>18} 원")
    print()

    # output1: 보유 종목 리스트
    holdings = data.get("output1", [])
    print("-" * 60)
    print(f"보유 종목 ({len(holdings)}개)")
    print("-" * 60)
    if not holdings:
        print("  (보유 종목 없음)")
    else:
        print(f"  {'종목명':<16} {'수량':>8} {'평단':>10} {'현재가':>10} {'손익':>12}")
        for h in holdings:
            name = (h.get("prdt_name") or "")[:16]
            qty = h.get("hldg_qty", "0")
            avg = _fmt_won(h.get("pchs_avg_pric"))
            cur = _fmt_won(h.get("prpr"))
            pnl = _fmt_won(h.get("evlu_pfls_amt"))
            print(f"  {name:<16} {qty:>8} {avg:>10} {cur:>10} {pnl:>12}")
    print()

    print("잔고 조회 완료. 다음 단계: 시세 조회 + 주문 테스트.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
