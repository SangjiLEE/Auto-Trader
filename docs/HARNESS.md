# 하네스 엔지니어링 (Harness Engineering)

이 시스템의 **재사용성·검증성·자동화의 핵심**은 명시적 하네스 설계.

> **하네스 (Harness)**: 코드를 동일한 패턴으로 실행·검증·재사용 가능하게 만드는 인프라.

8일간 5번의 큰 전략 재설계가 가능했던 이유 = 하네스가 잘 분리되어 있어서.

---

## 1. 백테스트 하네스 (5개 모듈)

전략을 직접 실거래 배포하지 않고, **각 전략별로 백테스트 모듈 분리**해 검증 게이트를 강제.

```
swing_backtest.py             # 슬로우 스윙 (200일 MA, Faber Timing)
swing_backtest_fast.py        # 빠른 스윙 (단타)
swing_backtest_v3.py          # v3 체제별 어댑티브 + 체제별 거래 분석
swing_backtest_v4.py          # v3 + F&G 극단 분할매매
backtest_rsi_reversion.py     # RSI 평균회귀
backtest_bb_reversion.py      # 볼린저 밴드 평균회귀
```

**공통 패턴:**
```python
@dataclass
class BacktestState:
    cash: float
    position: Position | None
    completed_trades: list[Trade]
    equity_curve: dict[date, float]

def run_backtest(df, ...) -> BacktestState: ...
def compute_metrics(state) -> dict: ...  # CAGR, Sharpe, MDD, win_rate
def regime_breakdown(state) -> dict: ...  # BULL/RANGE/BEAR 별 거래
```

**효과**:
- 새 전략 추가 시 같은 패턴 재사용
- 단타·평균회귀 등 4개 전략을 빠르게 검증 → 효과 미미 데이터 증거로 폐기
- 매번 같은 지표 (Sharpe, MDD, BH 비교) 자동 출력

---

## 2. 견고성 검증 하네스 (`robustness.py`)

**3가지 자동 검증** 한 번에:

```python
param_sensitivity()  # 룩백 3~24개월 그리드 → CAGR/Sharpe/MDD
oos_split()          # IS (전반부) vs OOS (후반부) 검증
yearly_analysis()    # 연도별 수익률 + 음수 연도 비율
```

각 검증마다 **자동 판정** ("안정적 / 불안정 / 일관됨 / 크게 저하"):

```
파라미터 민감도: CAGR 변동 6.2%p → 안정적
OOS 분할: OOS 가 IS 보다 좋음 → 우연 가능성, 또는 시장 환경 변화
연도별: 음수 4/11 → 정상 범위
```

DM 의 견고성을 정량으로 확인 후 배포 결정.

---

## 3. 전략 플러그인 아키텍처

전략을 별도 모듈로 분리. 공통 인터페이스:

```python
@dataclass
class Position: ...

@dataclass
class EntrySignal:
    valid: bool
    reasons: list[str]

@dataclass
class ExitSignal:
    should_exit: bool
    reason: str

# 각 전략 모듈이 구현
def check_entry(row: pd.Series) -> EntrySignal: ...
def check_exit(row, position, current_date) -> ExitSignal: ...
```

**모듈 구성:**
- `swing_strategy.py` — 슬로우 스윙 (6 AND 진입)
- `swing_strategy_fast.py` — 빠른 스윙 (4 AND, 짧은 보유)
- `swing_strategy_v3.py` — 체제 어댑티브 (체제별 다른 룰)
- `strategy_rsi_reversion.py` — RSI 평균회귀
- `strategy_bb_reversion.py` — BB 평균회귀

**효과:** 백테스트와 실거래 스크립트가 같은 strategy 모듈 import. **전략 수정 시 백테스트·실거래 동기화 보장**.

v3 는 한 단계 더 발전:
- `Action` 다단계 반환 (SELL_PARTIAL / SELL_ALL / BUY_DCA)
- 체제별 파라미터 분기 (`PARAMS_BULL`, `PARAMS_RANGE`, `PARAMS_BEAR`)

---

## 4. 체제 분류 하네스 (`market_regime.py`)

200MA + 60MA + ADX 조합으로 매일 BULL/RANGE/BEAR 분류:

```python
def detect_regime(row) -> str: ...
def regime_series(df) -> pd.Series: ...
def regime_distribution(df) -> dict: ...
```

전략 모듈 / 백테스트 / 실거래 스크립트 어디서든 같은 분류 룰. **기준 변경 시 한 곳만 수정**하면 모든 모듈에 반영.

---

## 5. 지표 하네스 (`indicators.py`)

모든 지표를 **단일 호출**로 부착:

```python
df = indicators.attach_all(df)
# → ma20, ma60, ma120, ma200,
#   rsi14,
#   tenkan, kijun, span_a, span_b, chikou, cloud_top, cloud_bottom,
#   vol_ma20,
#   bb_upper, bb_middle, bb_lower, bb_width, bb_pct,
#   atr14, adx14
```

새 지표 추가 시 `attach_all()` 한 줄 + 함수 정의만 추가 → 모든 백테스트/실거래에 자동 반영.

```python
# 새 지표 추가 예시
def new_indicator(df) -> pd.Series:
    return ...

def attach_all(df):
    # 기존 지표 ...
    result["new_ind"] = new_indicator(df)
    return result
```

---

## 6. 실거래 실행 하네스 (`daily_swing_v3_*.py`)

**드라이런 ↔ 실행 토글:**

```bash
python -m src.daily_swing_v3_kr           # 드라이런 (주문 X)
python -m src.daily_swing_v3_kr --execute # 실 주문
python -m src.daily_swing_v3_kr --execute --yes  # 자동 (스케줄용, 프롬프트 X)
```

**상태 재구성** (`reconstruct_position()`):

trades DB 에서 현재 포지션 복원 — 부분익절·DCA·체제 모두 trade notes 에서 파싱:

```python
# 매수 기록: notes = "v3 진입 (체제: BULL, F&G 33)"
# 매도 기록: notes = "+3% 1차 부분익절 (RANGE)"

def reconstruct_position(symbol, df) -> PositionV3 | None:
    # trades 테이블 쿼리
    # 매수 누적 → qty + avg_price
    # 매도 분석 → pf_t1_done, pf_t2_done, trailing_active
    # entry notes 파싱 → entry_regime
    # 가격 히스토리 → peak_price 계산
```

**효과**: 실거래 스크립트는 **stateless**. 모든 상태는 DB 에서. 재시작/재배포 시 안전.

---

## 7. 인증·캐시 하네스 (`kis_auth.py`)

OAuth2 토큰 + 파일 캐시 (24시간 유효):

```python
def get_access_token(force_refresh: bool = False) -> str:
    """캐시 우선, 5분 여유로 만료 전 갱신."""
    if not force_refresh:
        cached = _load_cached_token()
        if cached: return cached
    return _request_new_token()
```

KIS 분당 토큰 발급 제한 회피. 모든 스크립트가 공유.

---

## 8. 알림 하네스 (`notify.py`)

Telegram 통합 모듈, **체제·F&G 정보 표준화**:

```python
notify.notify_rebalance_success(env, ok, summary)
notify.notify_rebalance_failure(env, error)
notify.notify_order_result(env, side, sym, qty, price, success, detail)
notify.send(message, silent=False)
```

**알림 비활성 시 silent fail.** 메인 워크플로우 영향 X:
```python
def is_enabled() -> bool:
    token, chat_id = _creds()
    return bool(token) and bool(chat_id)

def send(message, silent=False) -> bool:
    if not is_enabled():
        return False  # 조용히 스킵
    ...
```

---

## 9. 스케줄 하네스 (macOS launchd)

5개 plist 파일로 **시간대별 작업 분산**:

```
deploy/
├── monthly.plist         # 매월 1~7일 09:05 (휴일 회피)
├── daily_swing_v3_kr     # 평일 09:20 (KR 장 시작)
├── daily_swing_v3_us     # 평일 23:50 (US 장 시작)
├── snapshot.plist        # 평일 15:45 (KR 마감)
└── us_closing_report     # Tue~Sat 06:15 (US 마감 다음 날)
```

각 plist 가 독립적. **한 작업 실패해도 다른 작업 영향 X**:

```xml
<key>StartCalendarInterval</key>
<array>
    <dict><key>Weekday</key><integer>1</integer>
          <key>Hour</key><integer>9</integer>
          <key>Minute</key><integer>20</integer></dict>
    <!-- ... 5일 반복 -->
</array>

<key>StandardOutPath</key>
<string>logs/daily_swing_v3_kr.log</string>
<key>StandardErrorPath</key>
<string>logs/daily_swing_v3_kr.err</string>
```

---

## 10. 데이터 레이어 (`db.py`)

SQLite 단일 파일 DB. 4개 테이블:

```sql
daily_candles       -- 일봉 시계열 (UPSERT)
trades              -- 모든 거래 로그 (strategy 태그로 분류)
daily_snapshots     -- 일일 계좌 상태 (KR + US 통합)
us_close_snapshots  -- 미국 장 마감 별도 추적
```

`with db.connection() as conn:` 컨텍스트 매니저로 자동 커밋.

---

## 11. 환경 분기 (`config.py`)

```env
KIS_ENV=paper  # 또는 real
```

한 줄 변경으로 모의/실거래 전환. 모든 스크립트가 `config.KIS_ENV` 참조.

`--execute` 플래그는 실거래 모드 차단:
```python
if args.execute and config.KIS_ENV != "paper":
    print("[차단] 실거래 모드. KIS_ENV=paper 확인.")
    return 1
```

이중 안전망. 실수로 실거래 모드에서 자동 매매하는 사고 방지.

---

## 12. 점진적 배포 + 롤백 가능성

각 단계가 독립 모듈이라 부분 배포 / 롤백 쉬움:

| 변경 | 영향 | 롤백 |
|-----|-----|-----|
| 단타 폐기 | fast 모듈 그대로, plist 만 unload | plist 다시 load 하면 부활 |
| v3 도입 | 기존 daily_swing 유지, v3 plist 추가 | v3 plist unload + daily_swing 활성화 |
| F&G 추가 | v3 코드에 분기만 삽입 | 분기 제거 또는 `FG_EXTREME_*` 임계값 0/100 |

**플러그인 아키텍처 + 백테스트 게이트 + 드라이런 토글 = 안전한 반복 실험.**

---

## 핵심 학습

1. **하네스 먼저, 전략 나중**: 백테스트 인프라 만들고 전략 끼워넣기
2. **공통 인터페이스 강제**: dataclass 로 시그널/포지션 표준화
3. **상태는 DB**: 메모리 상태 X, 모든 것은 trades 테이블에서 재구성
4. **드라이런 토글**: `--execute` 플래그로 안전·운영 분리
5. **데이터로 의사결정**: 직관 → 백테스트 → 견고성 검증 → 배포 (or 폐기)

이 패턴은 다른 도메인 (마케팅 자동화, 데이터 ETL, 모니터링 등) 에도 그대로 적용 가능.
