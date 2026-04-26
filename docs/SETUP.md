# 설치 가이드

처음 시작부터 자동 운영까지.

---

## 사전 준비

- macOS (Apple Silicon 또는 Intel)
- Python 3.12+
- 한국투자증권 계좌
- (선택) Telegram 계정

---

## 1. Python 환경

```bash
# Python 3.12 확인
python3.12 --version
# 없으면: brew install python@3.12

# 프로젝트 클론
git clone https://github.com/SangjiLEE/Auto-Trader.git
cd Auto-Trader

# 가상환경
python3.12 -m venv .venv
source .venv/bin/activate

# 패키지
pip install -r requirements.txt
```

---

## 2. KIS Open API 발급

### 2-1. 계좌 개설
1. **한국투자증권 모바일 앱** 다운로드
2. 비대면 계좌 개설 (국내주식 + 해외주식 동시)
3. 모의투자 계좌도 별도 신청 (시드 5천만원 자동 지급)

### 2-2. API 키 발급
1. https://apiportal.koreainvestment.com 접속 (계좌 로그인)
2. **나의 앱 → 앱 등록**
3. APP Key + APP Secret 발급
4. **모의투자 계좌**도 별도 앱 등록 (별도 키)

> ⚠️ **APP Secret 은 발급 시 한 번만 노출됨.** 즉시 안전한 곳에 저장.

---

## 3. Telegram 봇 (선택, 강력 추천)

### 3-1. 봇 생성
1. Telegram 에서 `@BotFather` 검색
2. `/newbot` 입력
3. 봇 이름 (예: `My Trading Bot`)
4. 봇 유저네임 (끝에 `bot`, 예: `sangjisair_trade_bot`)
5. **토큰 발급** (예: `7234567890:AAH...`)

### 3-2. Chat ID 확인
1. 본인 봇과 대화 시작 (아무 메시지 전송)
2. 터미널:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
   ```
3. 결과의 `"chat": {"id": ...}` 가 chat_id

---

## 4. `.env` 작성

```bash
cp .env.example .env
nano .env
```

```env
# 모의투자 (먼저 시작!)
KIS_PAPER_APP_KEY=PSxxxxxxxxx
KIS_PAPER_APP_SECRET=xxxxxxxx...
KIS_PAPER_ACCOUNT_NO=12345678-01
KIS_PAPER_BASE_URL=https://openapivts.koreainvestment.com:29443

# 실거래 (나중에)
KIS_REAL_APP_KEY=...
KIS_REAL_APP_SECRET=...
KIS_REAL_ACCOUNT_NO=...
KIS_REAL_BASE_URL=https://openapi.koreainvestment.com:9443

# 환경 토글
KIS_ENV=paper  # ⚠️ 처음엔 무조건 paper

# Telegram
TELEGRAM_BOT_TOKEN=7234567890:AAH...
TELEGRAM_CHAT_ID=123456789
```

---

## 5. 검증

```bash
# 인증 테스트
python -m src.hello_world
# → "Hello World 완료" 면 OK

# Telegram 테스트
python -m src.notify "설치 테스트"
# → 핸드폰에 메시지 와야 함

# 잔고 조회
python -m src.check_balance
```

---

## 6. 데이터 수집

```bash
# 기본 유니버스 (10년치)
python -m src.load_candles

# 추가 종목
python -m src.load_candles 035420                    # NAVER
python -m src.load_candles AAPL --market US --years 5
python -m src.load_candles NVDA --market US --years 5
python -m src.load_candles TSLA --market US --years 5

# 확인
python -m src.show_candles
```

---

## 7. 백테스트 (선택)

```bash
# DM
python -m src.dual_momentum

# v3 (체제 어댑티브)
python -m src.swing_backtest_v3 NVDA

# v4 (v3 + F&G)
python -m src.swing_backtest_v4

# 견고성 검증
python -m src.robustness
```

---

## 8. 드라이런 (실 주문 X)

자동 실행 전에 모든 스크립트가 정상 동작하는지 확인:

```bash
python -m src.monthly_rebalance         # DM
python -m src.daily_swing_v3_kr         # KR 스윙
python -m src.daily_swing_v3_us         # US 스윙
python -m src.daily_snapshot            # 스냅샷
python -m src.us_closing_report         # US 마감 보고
```

각 스크립트가 정상 출력 + Telegram 메시지 확인.

---

## 9. 자동 스케줄 설치 (macOS launchd)

```bash
# 5개 plist 설치
for plist in deploy/*.plist; do
    cp "$plist" ~/Library/LaunchAgents/
    launchctl load "$HOME/Library/LaunchAgents/$(basename $plist)"
done

# 확인 (5개 떠야 정상)
launchctl list | grep autotrading
```

기대:
```
- 0  com.sangjisair.autotrading.monthly
- 0  com.sangjisair.autotrading.daily_swing_v3_kr
- 0  com.sangjisair.autotrading.daily_swing_v3_us
- 0  com.sangjisair.autotrading.snapshot
- 0  com.sangjisair.autotrading.us_closing_report
```

---

## 10. 운영 시 주의

### 맥 전원 / 네트워크
- **전원 ON 유지** (Sleep 은 OK, launchd 가 깨움)
- **Wi-Fi 연결** 유지 (자동 실행 시 KIS API 접근 필요)
- **lid 닫는 건 OK** (외부 모니터 없으면 불안정 가능)

### 첫 자동 실행 후 확인
1. 다음 평일 09:05 / 09:20 / 15:45 / 23:50 알림 확인
2. 로그 파일: `logs/*.log`
3. 트레이드 기록: `sqlite3 data.db "SELECT * FROM trades ORDER BY id DESC LIMIT 10"`

---

## 11. 자동 작업 비활성화 / 제거

### 일시 정지
```bash
launchctl unload ~/Library/LaunchAgents/com.sangjisair.autotrading.daily_swing_v3_kr.plist
```

### 영구 제거
```bash
rm ~/Library/LaunchAgents/com.sangjisair.autotrading.*.plist
```

---

## 디스클레이머

### 🚨 매우 중요

1. **이 코드는 학습 / 실험 목적**. 실거래 사용 시 **본인 책임**.
2. **모의투자 (`KIS_ENV=paper`) 로 최소 3-6개월 검증** 후 실거래 전환.
3. **백테스트 결과는 미래 보장 X**. 과적합 위험.
4. **API 키 / 토큰 / 계좌번호는 절대 공유 금지.**
   - `.env` 가 `.gitignore` 에 있는지 확인
   - 채팅, 이메일, AI 에이전트, 깃허브 등에 절대 붙여넣지 않기
5. **자동매매는 잘못 설정하면 빠르게 큰 손실 가능.** 일일 손실 한도, 포지션 크기 등 안전장치 필수.

### 알려진 한계
- 모든 전략이 BH 못 이김 (장기 강추세 종목 기준)
- F&G 데이터는 암호화폐 sentiment 기반 (글로벌 위험선호도 프록시)
- 일봉만 사용 (분봉 / 인트라데이 미지원)

### API 키 유출 시
즉시 https://apiportal.koreainvestment.com 에서 재발급. 기존 키 자동 무효화.
