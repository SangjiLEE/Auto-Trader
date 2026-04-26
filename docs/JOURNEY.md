# 8일 진화 일지 (2026-04-19 ~ 04-26)

각 단계의 결정·학습·증거를 기록.

---

## Day 1 (4/19, 일) — 시작

**시작점:** "AI 구독료 (월 30만원) 자동매매로 충당하고 싶다."

**첫 진단 (사용자와 토론):**
- 100만원으로 월 30만원 = 월 30% 수익률 = 수학적으로 불가능
- "주당 10% / 최대 하락 5%" 목표는 메달리언 펀드의 50배
- 진짜 목표 재설계: **점진적 수익 파이프라인 + 손해 가능성 줄이기**

**결정:**
- KIS 한국투자증권 API 사용 (모의투자 시작)
- Python + FastAPI 백엔드 + (나중에) React 프론트
- 6~12개월 검증 후 자본 증액

**작업:**
- DESIGN_DOC.md 작성 (8개 트레이딩 스타일 비교)
- 단타 폐기 결정 (100만원에선 비용 드래그로 음수 EV)

**산출물:** [DESIGN_DOC.md](../DESIGN_DOC.md)

---

## Day 2 (4/20, 월) — KIS API 연결

**작업:**
- KIS 모의투자 계좌 + APP Key/Secret 발급
- `.env` 셋업, OAuth2 인증 (`kis_auth.py`)
- 토큰 캐시 (24시간 유효, 분당 발급 제한 회피)
- Hello World — 첫 KIS 호출 성공

**시행착오:**
- Python 3.9 사용 → 3.10+ 타입 힌트 (`X | None`) 호환 X
- → `from __future__ import annotations` 또는 3.12 venv 재생성
- 결국 3.12 venv 로 갈아탐

**산출물:** `kis_auth.py`, `kis_api.py`, `hello_world.py`

---

## Day 3 (4/21, 화) — DM 백테스트 + 견고성 검증

**작업:**
- FinanceDataReader 로 일봉 10년치 수집 (069500, 133690, SPY, QQQ 등)
- SQLite DB 스키마 (`db.py`)
- Dual Momentum 백테스트 (`dual_momentum.py`)
- 견고성 검증 3종 (`robustness.py`)

**검증:**
- 파라미터 민감도: CAGR 변동 6.2%p → 안정
- IS/OOS 분할: OOS (2021~) 가 IS 보다 좋음 → 과적합 반대
- 연도별: 음수 4/11 → 정상

**결정:**
- DM 70% 슬롯으로 운영
- 단순 BH 와 거의 동급 CAGR + MDD 절반 (Sharpe 1.10)

**산출물:** `dual_momentum.py`, `robustness.py`, `swing_backtest.py`

---

## Day 4 (4/22, 수) — 첫 모의 체결

**작업:**
- KIS 잔고 조회 (`check_balance.py`)
- 시세 조회 (`check_price.py`)
- 주문 (`place_order.py`)
- hashkey 생성 + 적용 (KIS 주문 API 필수)

**시행착오:**
- 첫 주문 시 HTTP 500 → hashkey 누락 발견
- KIS 주문 API 는 hashkey 헤더 필수 (조회 API 는 불필요)

**첫 체결 (10:58:33 KST):**
```
[1/1] 매수 005930 508주...
  성공. 주문번호 0000016351
```

069500 KODEX 200, 5천만원 모의 자금 거의 풀 투입.

**산출물:** `place_order.py`, `monthly_rebalance.py`, 첫 거래 기록 in DB

---

## Day 5 (4/23, 목) — Telegram + 자동화

**작업:**
- Telegram Bot 생성 (@BotFather)
- `notify.py` 모듈 (체결·실패·일일 보고)
- macOS launchd plist 4개 생성:
  - monthly (매월 1~3일 09:05)
  - daily_swing (평일 09:10)
  - daily_swing_us (평일 23:45)
  - snapshot (평일 15:45)
- 일일 스냅샷 자동화 (`daily_snapshot.py`)

**시행착오:**
- launchd `load` 시 "Operation not permitted" 에러
- 원인: 프로젝트가 `~/Desktop/` 에 있어 macOS TCC 가 차단
- 해결: `~/projects/auto-trading-bot/` 으로 이관

**보안 사고:**
- 사용자가 실거래 APP Secret 을 채팅창에 붙여넣음
- → 즉시 KIS 포털에서 재발급 안내
- `.env` 사용법 강조 + `.gitignore` 보안 가드 강화

**산출물:** `notify.py`, `daily_snapshot.py`, 5개 plist

---

## Day 6 (4/24, 금) — 미국 시장 통합

**작업:**
- KIS 해외주식 API 분석:
  - 엔드포인트: `/uapi/overseas-stock/`
  - tr_id 별도 (예: `VTTT1002U` 모의 미국 매수)
  - 거래소 코드 (NASD/NYSE/AMEX)
  - **시세 API 는 짧은 코드 (NAS/NYS/AMS)** 별도 매핑
- `kis_overseas.py` 어댑터
- `check_overseas_price.py`, `check_overseas_balance.py`, `place_overseas_order.py`
- 미국 종목 일봉 수집 (AAPL, NVDA, TSLA, SPY, QQQ)
- 미국 마감 보고 (`us_closing_report.py`)

**시행착오:**
- "ERROR INVALID [EXCD]=[NASD]" — 시세 API 가 NASD 거부
- → 별도 매핑 (NASD → NAS, NYSE → NYS, AMEX → AMS) 발견

**산출물:** 6개 모듈 + 1 plist 추가

---

## Day 7 (4/25, 토) — Enhanced Swing v3

**작업:**
- 빠른 스윙 (단타) 백테스트 → 모든 종목 BH 못 이김 → **폐기**
- 시장 체제 분류기 (`market_regime.py`) — BULL/RANGE/BEAR
- Enhanced Swing v3 (`swing_strategy_v3.py`):
  - 체제별 다른 룰 (BULL: 큰 익절, RANGE: 회전, BEAR: 차단)
  - 부분 익절 + DCA + 트레일링
  - Position 다단계 상태 (pf_t1_done, pf_t2_done, trailing_active)

**시행착오:**
- 백테스트 거래 수 너무 적음 (2-3회/년)
- 원인: 재진입 룰 너무 엄격 (직전 청산가 -2% 이상 하락 + N일 쿨다운)
- → 쿨다운 N일의 2.5배 지나면 가격 무관 재진입 허용

**검증:**
- NVDA: +64% / 5년, MDD -17% (BH MDD 추정 -66%)
- TSLA: +55% / 5년 (BH +85% 거의 따라감)
- 069500: +19% / 10년

**결정:**
- v3 KR/US 각 15% 슬롯
- DM 70% + v3 30% = 100%

**산출물:** `swing_strategy_v3.py`, `swing_backtest_v3.py`, `daily_swing_v3_*.py`

---

## Day 8 (4/26, 일) — F&G 통합 + 정리

**작업:**
- 평균회귀 시도:
  - RSI < 30 + 200MA 위: 거래 1-3회/년 (의미 없음) → 폐기
  - BB 하단 터치: 거래 빈도 OK 지만 모든 종목 BH 못 이김 → 폐기
- 공포탐욕지수 통합 (`fear_greed.py`):
  - Alternative.me API
  - 사이즈 배율 (Extreme Fear 1.30x, Extreme Greed 0.0x)
- v4 (v3 + F&G 극단 분할매매):
  - F&G ≤ 7: 분할매수 25%
  - F&G ≥ 92: 분할매도 10% (BULL 제외)

**시행착오:**
- v4 strict (≥88, 25%): 강세장에서 너무 자주 매도 → v3 보다 나쁨
- v4 relaxed (≥92, 10%, BULL skip): v3 동급 또는 우수
- 069500: v3 +19.1% → v4 relaxed **+28.85%** (+9.7%p)

**최종 정리:**
- README + 6개 docs 분리
- CLAUDE.md (AI 워크플로우 컨텍스트)
- GitHub 푸시 준비

**산출물:** `fear_greed.py`, `swing_backtest_v4.py`, v3 KR/US 에 F&G 통합

---

## 코드량 + 시간

- **30+ Python 모듈, ~7,000줄 코드**
- **5개 launchd 작업**
- **5개 전략 모듈** (3개 운영 + 2개 폐기 보존)
- **5개 백테스트 모듈**
- **8일 = 약 40~60시간**

---

## 의사결정의 패턴

매 단계 같은 사이클:
```
1. 가설 (사용자 직관 또는 학술/업계 룰)
2. 백테스트 모듈 작성
3. 데이터 검증 (CAGR/Sharpe/MDD/거래수/체제별)
4. 견고성 검증 (파라미터, OOS, 연도별)
5. 드라이런 실행 → 실거래 (or 폐기)
```

이 패턴이 8일에 5번 반복:
1. 슬로우 스윙 (Faber) → 일봉 whipsaw 로 BH 못 이김 → 보관 후 v3 로 발전
2. 단타 → 비용 드래그 → 폐기
3. v3 (체제 어댑티브) → 작동 → 배포
4. RSI/BB 평균회귀 → 효과 미미 → 폐기
5. v4 (v3 + F&G) → 일부 종목 v3 보다 우수 → 배포

**데이터로 의사결정.** 직관 좋아도 백테스트 안 통과하면 폐기.

---

## 인사이트 (그동안 배운 것)

1. **모든 전략이 BH 못 이긴다** (장기 강추세 시장).  하지만 **MDD 절반**은 가능.
2. **체제 어댑티브 > 단일 룰**. 같은 종목도 시장 상태에 따라 다른 룰.
3. **F&G 분할매수 효과 미미, 분할매도 BULL skip 시 효과적**.
4. **유니버스 선별 > 알고리즘 튜닝**. 강추세 종목 (000660, SPY 30년) 은 어떤 룰로도 BH 못 이김.
5. **백테스트 게이트 + 드라이런 토글**이 빠른 반복의 핵심.

---

## 사용자 의사결정 흐름

| 시점 | 사용자가 한 말 | 결과 |
|-----|---------------|-----|
| 4/19 | "주당 10% 가능?" | 수학적 불가능 설명, 목표 재설정 |
| 4/22 | "단타도 가능?" | 백테스트로 음수 EV 증명, 폐기 |
| 4/24 | "미국주식도?" | 해외주식 API 통합 |
| 4/25 | "체제 어댑티브 룰?" | v3 빌드 |
| 4/25 | "유저 직관 (오르면 팔고 떨어지면 사기)?" | RANGE 모드로 이미 구현됨 |
| 4/26 | "RSI 평균회귀 추가?" | 백테스트로 효과 미미 증명, 폐기 |
| 4/26 | "BB 평균회귀?" | 동일 결과, 폐기 |
| 4/26 | "공포탐욕지수?" | v4 통합, 검증 |
| 4/26 | "F&G ≤7 분할매수, ≥88 분할매도" | strict 시도 → relaxed (BULL skip) 로 개선 |

각 결정마다 백테스트 또는 데이터 증거로 판단.

---

## 앞으로

5/4 첫 본격 자동 사이클 → 5월 한 달 데이터 축적 → 6월 초 분석 → 6~9월 검증 → 10월~ 실거래 전환 검토.

자세한 계획: [README.md](../README.md#앞으로의-계획) 참고.
