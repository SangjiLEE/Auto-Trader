# CLAUDE.md

이 파일은 **Claude Code (또는 다른 AI 페어 프로그래밍 도구) 가 이 프로젝트를 작업할 때** 참고할 컨텍스트.

---

## 프로젝트 개요

한국투자증권 KIS Open API 기반 개인용 자동매매 시스템.

- **언어**: Python 3.12+
- **DB**: SQLite (`data.db`)
- **시장**: KR (KIS 국내주식) + US (KIS 해외주식)
- **자동화**: macOS launchd (5개 plist)
- **알림**: Telegram Bot

자세한 내용은 [README.md](README.md) 참고.

---

## 핵심 원칙 (절대 변경 금지)

1. **`.env` 절대 커밋 금지.** `.gitignore` 에 있는지 항상 확인.
2. **API 키 / 토큰 / 계좌번호 절대 출력 / 로그 / 알림 금지.** 사용자에게도 보여주지 말 것.
3. **실거래 모드 (`KIS_ENV=real`) 가드 항상 유지.** `--execute` 플래그는 paper 모드에서만 작동.
4. **새 전략은 백테스트 게이트 통과 후 배포.** 견고성 검증 (파라미터/OOS/연도별) 필수.
5. **상태는 항상 DB.** 메모리 상태 X, 모든 것은 trades 테이블에서 재구성.

---

## 디렉토리 구조

```
src/
├── 인증/API
│   ├── config.py            # .env 로더
│   ├── kis_auth.py          # OAuth + 토큰 캐시
│   ├── kis_api.py           # REST 헬퍼 (hashkey 포함)
│   └── kis_overseas.py      # 해외 거래소·tr_id 매핑
│
├── 조회/주문
│   ├── check_balance.py / check_overseas_balance.py
│   ├── check_price.py / check_overseas_price.py
│   └── place_order.py / place_overseas_order.py
│
├── 데이터
│   ├── db.py                # SQLite 스키마
│   ├── load_candles.py      # FDR 시세 수집
│   └── show_candles.py
│
├── 분석 도구
│   ├── indicators.py        # MA/RSI/Ichimoku/BB/ATR/ADX
│   ├── market_regime.py     # BULL/RANGE/BEAR 분류
│   └── fear_greed.py        # F&G API
│
├── 전략 모듈
│   ├── swing_strategy_v3.py        # 운영 중 (체제 어댑티브)
│   ├── strategy_rsi_reversion.py   # 폐기 (참고용)
│   ├── strategy_bb_reversion.py    # 폐기 (참고용)
│   ├── swing_strategy.py           # 폐기 (슬로우 스윙)
│   └── swing_strategy_fast.py      # 폐기 (단타)
│
├── 백테스트
│   ├── dual_momentum.py
│   ├── swing_backtest.py / _fast.py / _v3.py / _v4.py
│   ├── backtest_rsi_reversion.py / backtest_bb_reversion.py
│   └── robustness.py
│
├── 실거래 실행
│   ├── monthly_rebalance.py        # DM 월간
│   ├── daily_swing_v3_kr.py        # KR 일간
│   ├── daily_swing_v3_us.py        # US 일간
│   ├── daily_snapshot.py           # 일일 스냅샷
│   └── us_closing_report.py        # US 마감
│
├── 유틸
│   ├── notify.py            # Telegram
│   ├── swing_screener.py    # 단타 적합도
│   └── hello_world.py       # 인증 검증
│
└── (기타)

deploy/                       # launchd plist 5개
docs/                         # 상세 문서 (분리)
logs/                         # 자동 실행 로그 (.gitignore)
```

---

## 코드 패턴 (반드시 지켜야 할 것)

### 1. 전략 모듈 인터페이스

새 전략 추가 시:

```python
from dataclasses import dataclass

@dataclass
class Position:
    qty: int
    avg_price: float
    entry_date: pd.Timestamp
    # 전략별 추가 필드 (peak_price, breakeven_triggered 등)

@dataclass
class EntrySignal:
    valid: bool
    reasons: list[str]

@dataclass
class ExitSignal:
    should_exit: bool
    reason: str

def check_entry(row: pd.Series) -> EntrySignal:
    """진입 시그널. 4-6 AND 조건."""
    ...

def check_exit(row, position, current_date) -> ExitSignal:
    """청산 시그널. 5-6 OR 조건."""
    ...
```

### 2. 백테스트 모듈 패턴

```python
@dataclass
class BacktestState:
    cash: float
    position: Position | None
    completed_trades: list[dict]
    equity_curve: dict[pd.Timestamp, float]

def run_backtest(df, initial_capital) -> BacktestState: ...
def compute_metrics(state, initial) -> dict: ...
def print_summary(symbol, state, df, initial) -> None: ...
```

### 3. 실거래 스크립트 패턴

```bash
# 드라이런 (주문 X)
python -m src.daily_swing_v3_kr

# 실 주문 (확인 프롬프트)
python -m src.daily_swing_v3_kr --execute

# 자동 실행 (스케줄용, 프롬프트 X)
python -m src.daily_swing_v3_kr --execute --yes

# 데이터 갱신 후 실행
python -m src.daily_swing_v3_kr --refresh --execute --yes
```

`--execute` 시 `config.KIS_ENV != "paper"` 면 자동 차단.

### 4. 상태 재구성

실거래 스크립트는 stateless. 상태는 DB 에서:

```python
def reconstruct_position(symbol, df) -> Position | None:
    """trades 테이블에서 현재 포지션 재구성."""
    # 매수 누적 → qty + avg_price
    # 매도 분석 → 부분익절 플래그
    # entry notes 파싱 → entry_regime
    # 가격 히스토리 → peak_price 계산
```

`notes` 필드에 모든 컨텍스트 저장:
- 매수: `"v3 진입 (체제: BULL, F&G 33)"`
- 매도: `"+3% 1차 부분익절 (RANGE)"`

### 5. Telegram 알림

```python
from . import notify

if notify.is_enabled():
    notify.send(message)  # 비활성 시 silent skip
```

메인 워크플로우 영향 X.

---

## 자주 사용하는 명령

### 검증 / 테스트
```bash
python -m src.hello_world             # KIS 인증
python -m src.check_balance           # 잔고
python -m src.notify "테스트"          # Telegram
python -m src.fear_greed              # F&G 현재값
```

### 백테스트
```bash
python -m src.dual_momentum           # DM
python -m src.swing_backtest_v3       # v3 (4종목)
python -m src.swing_backtest_v4       # v4 (v3+F&G)
python -m src.robustness              # 견고성 검증
```

### 드라이런
```bash
python -m src.monthly_rebalance       # DM
python -m src.daily_swing_v3_kr       # KR
python -m src.daily_swing_v3_us       # US
```

### 실 주문 (모의 계좌)
```bash
python -m src.monthly_rebalance --execute
python -m src.daily_swing_v3_kr --execute
python -m src.daily_swing_v3_us --execute
```

### 데이터
```bash
python -m src.load_candles                    # 기본 유니버스
python -m src.load_candles AAPL --market US
python -m src.show_candles                    # 저장 데이터 확인
```

### launchd 관리
```bash
launchctl list | grep autotrading             # 등록 확인
launchctl unload ~/Library/LaunchAgents/com.sangjisair.autotrading.<name>.plist
launchctl load ~/Library/LaunchAgents/com.sangjisair.autotrading.<name>.plist
```

---

## 새 전략 추가 워크플로우

```
1. src/strategy_<name>.py            # 전략 룰 모듈
2. src/backtest_<name>.py            # 백테스트 모듈
3. python -m src.backtest_<name>     # 검증
   - CAGR, Sharpe, MDD, win_rate
   - 체제별 거래 분석
   - BH 비교
4. 결과 좋으면:
   src/daily_<name>.py               # 실거래 모듈
   deploy/com.sangjisair.autotrading.<name>.plist
5. 드라이런 → --execute 검증 → launchd 설치
```

결과 안 좋으면 폐기. 모듈은 보존 (참고용).

---

## 절대 하지 말아야 할 것

1. **`.env` 또는 API 키를 코드에 하드코딩 X**
2. **`KIS_ENV=real` 로 무인 자동 실행 X** (확인 프롬프트 + 사용자 승인 필요)
3. **백테스트 안 한 전략을 자동 스케줄에 배포 X**
4. **`logs/`, `data.db`, `.token_cache.json`, `.fear_greed_cache.json` 깃 커밋 X**
5. **단일 백테스트 결과로 결론 X** — 견고성 검증 (파라미터, OOS, 연도별) 필수
6. **장기 강추세 종목 (NVDA 5년+ 같은) 단타 적용 X** — BH 압도적

---

## 운영 중 시스템 (현재)

- DM 70% (069500/133690/360750/148070 중 1택)
- v3 KR 15% (069500/005930/035420)
- v3 US 15% (AAPL/NVDA/TSLA)
- F&G 통합 (사이즈 배율 + 극단 분할매매)
- 5개 launchd 작업 (monthly, daily_swing_v3_kr, daily_swing_v3_us, snapshot, us_closing_report)

자본 합계: **100%**.

---

## 개발 환경

- macOS (Apple Silicon, M3 Air)
- Python 3.12 (Homebrew, `/opt/homebrew/bin/python3.12`)
- 가상환경: `.venv/`
- 한국투자증권 모의투자 (KIS_ENV=paper)
- Telegram Bot 알림 활성

---

## 참고 문서

- [README.md](README.md) — 프로젝트 전체 개요
- [docs/STRATEGIES.md](docs/STRATEGIES.md) — 전략 룰 상세
- [docs/HARNESS.md](docs/HARNESS.md) — 하네스 엔지니어링
- [docs/SETUP.md](docs/SETUP.md) — 설치 가이드
- [docs/SCHEDULES.md](docs/SCHEDULES.md) — 자동 스케줄
- [docs/BACKTESTS.md](docs/BACKTESTS.md) — 백테스트 결과
- [docs/JOURNEY.md](docs/JOURNEY.md) — 8일 진화 일지
- [DESIGN_DOC.md](DESIGN_DOC.md) — 초기 설계
