"""
거래 비용 모델 — KR / US 분리, 모든 비용 항목 통합.

[Phase 1] Cross-model consensus (Codex MODIFY: timing fix 후 적용 / Claude 강력 추천)

기존: COMMISSION = 0.003 단일 상수 (왕복 0.3%)
실제 비용 분해:

KR 주식/ETF (069500, 005930, 035420 등):
  매수:
    - KIS 수수료: 0.015%
    - 스프레드: ~5-15bp (호가폭 단위)
    - 슬리피지: ~5-10bp (시초 갭 매매)
  매도:
    - KIS 수수료: 0.015%
    - 한국 거래세: 일반 주식 0.18%, ETF 면제 (2024 기준)
    - 스프레드 + 슬리피지: 동일
  → 왕복 ETF: ~0.30-0.40%
  → 왕복 일반 주식: ~0.50-0.60%

US 주식 (AAPL, NVDA, TSLA):
  매수:
    - KIS 수수료: 0.05% (한국 retail 기준 평균)
    - 스프레드: 1-3bp (대형주)
    - 슬리피지: 5-10bp (장 시작 갭)
  매도:
    - KIS 수수료: 0.05%
    - SEC fee + FINRA TAF: 0.005%
    - 스프레드 + 슬리피지: 동일
  KIS 환마진 (KRW→USD→KRW): ~0.30-0.50% 왕복
  → 왕복: ~0.55-0.85%

참고 자료:
  - KIS 공식 수수료표 (https://securities.koreainvestment.com)
  - 한국거래소 거래세 안내 (2024 기준 0.18%, 매년 인하)
  - FINRA Trading Activity Fee (TAF)
  - Almgren-Chriss "Optimal execution" (slippage 추정)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    """단방향 비용 분해. 왕복 = buy + sell."""

    name: str
    market: str               # "KR" | "US"
    commission_buy: float     # 비율 (0.0015 = 0.15%)
    commission_sell: float
    tax_sell: float           # 거래세, SEC fee 등 (매도시만)
    spread_one_side: float    # 매수/매도 각각 부담 (반호가)
    slippage_one_side: float  # 시초 갭 등
    fx_margin_rt: float       # 환마진 (왕복, US 만 해당)

    @property
    def buy_total(self) -> float:
        """매수 1회 비용 (가격에 곱하면 비용 금액)."""
        return self.commission_buy + self.spread_one_side + self.slippage_one_side

    @property
    def sell_total(self) -> float:
        """매도 1회 비용 (세금 포함)."""
        return (
            self.commission_sell + self.tax_sell
            + self.spread_one_side + self.slippage_one_side
        )

    @property
    def round_trip(self) -> float:
        """왕복 총 비용 (백테스트의 단일 cost 상수와 비교용)."""
        return self.buy_total + self.sell_total + self.fx_margin_rt

    def __repr__(self) -> str:
        return (
            f"CostModel({self.name}, market={self.market}, "
            f"RT={self.round_trip*100:.3f}%)"
        )


# ─── KR 모델 ─────────────────────────────────────────────────

KR_ETF_LARGE = CostModel(
    name="KR_ETF_LARGE",
    market="KR",
    commission_buy=0.00015,    # KIS 0.015%
    commission_sell=0.00015,
    tax_sell=0.0,              # ETF 거래세 면제 (2024)
    spread_one_side=0.0008,    # 8bp (KOSPI 200 ETF 호가)
    slippage_one_side=0.0010,  # 10bp 시초 갭
    fx_margin_rt=0.0,
)
# RT ≈ 0.36%

KR_STOCK_LARGE = CostModel(
    name="KR_STOCK_LARGE",
    market="KR",
    commission_buy=0.00015,
    commission_sell=0.00015,
    tax_sell=0.00180,          # 한국 일반주식 거래세 0.18%
    spread_one_side=0.0010,
    slippage_one_side=0.0010,
    fx_margin_rt=0.0,
)
# RT ≈ 0.56%

# ─── US 모델 ─────────────────────────────────────────────────

US_STOCK_LARGE = CostModel(
    name="US_STOCK_LARGE",
    market="US",
    commission_buy=0.00050,    # KIS 0.05%
    commission_sell=0.00050,
    tax_sell=0.00005,          # SEC fee + FINRA TAF
    spread_one_side=0.0003,    # 3bp 대형주
    slippage_one_side=0.0008,  # 8bp 갭
    fx_margin_rt=0.0040,       # 환마진 0.4% RT
)
# RT ≈ 0.66%

# ─── 호환용 (기존 COMMISSION = 0.003 으로 동작 유지하고 싶을 때) ─

LEGACY_FLAT = CostModel(
    name="LEGACY_FLAT_0.3pct",
    market="ANY",
    commission_buy=0.0015,     # 0.15% × 2 = 0.3% RT
    commission_sell=0.0015,
    tax_sell=0.0,
    spread_one_side=0.0,
    slippage_one_side=0.0,
    fx_margin_rt=0.0,
)
# RT ≈ 0.30% (기존 COMMISSION 상수 재현)


# ─── 자동 분류 ───────────────────────────────────────────────

# 알려진 ETF 종목 (확장 시 여기 추가)
_KR_ETFS = {"069500", "133690", "360750", "148070", "069660", "229200"}


def get_cost_model(symbol: str) -> CostModel:
    """심볼 → cost model 자동 매칭.

    한국 (6자리 숫자):
      - ETF 코드면 KR_ETF_LARGE
      - 그 외 KR_STOCK_LARGE
    그 외 (영문 ticker): US_STOCK_LARGE
    """
    sym = (symbol or "").strip().upper()
    if sym.isdigit() and len(sym) == 6:
        if sym in _KR_ETFS:
            return KR_ETF_LARGE
        return KR_STOCK_LARGE
    return US_STOCK_LARGE


def all_models() -> list[CostModel]:
    """비교 출력용 전체 모델 리스트."""
    return [KR_ETF_LARGE, KR_STOCK_LARGE, US_STOCK_LARGE, LEGACY_FLAT]


def main() -> int:
    """단독 실행 시 모든 모델 출력."""
    print("거래 비용 모델 비교")
    print("=" * 78)
    print(f"  {'name':<22} {'market':<7} {'buy':>9} {'sell':>9} {'RT':>9}")
    print("  " + "-" * 60)
    for m in all_models():
        print(
            f"  {m.name:<22} {m.market:<7} "
            f"{m.buy_total*100:>+8.3f}% {m.sell_total*100:>+8.3f}% "
            f"{m.round_trip*100:>+8.3f}%"
        )
    print()
    print("자동 분류 예시:")
    for sym in ["069500", "005930", "AAPL", "NVDA", "069660"]:
        m = get_cost_model(sym)
        print(f"  {sym} → {m.name} (RT {m.round_trip*100:.3f}%)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
