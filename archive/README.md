# Archive

CLAUDE.md / 백테스트 결과로 폐기 결정된 전략 모듈과 plist 들이 여기로 이동됨. 실제 운영에서는 사용 안 함.

남겨두는 이유:
- 신규 전략 개발 시 비교 / 참고
- 백테스트 결과 검증 (왜 폐기됐는지 추적 가능)
- git 이력만으로는 "왜" 가 안 보일 때 빠른 reference

## 분류

### `src/` — 폐기 전략 / 진입점

| 파일 | 폐기 사유 |
|---|---|
| `swing_strategy.py`, `swing_backtest.py` | 슬로우 스윙 — 백테스트 미달 |
| `swing_strategy_fast.py`, `swing_backtest_fast.py`, `daily_swing_fast_kr.py`, `daily_swing_fast_us.py` | 단타 — 장기 강추세 종목에서 BH 압도 |
| `swing_strategy_v2.py`, `swing_backtest_v2.py` | v3 으로 대체됨 |
| `strategy_rsi_reversion.py`, `backtest_rsi_reversion.py` | 평균회귀 — 강추세 시장에서 손실 누적 |
| `strategy_bb_reversion.py`, `backtest_bb_reversion.py` | 볼밴 평균회귀 — 동일 사유 |
| `daily_swing.py`, `daily_swing_us.py` | v3 으로 대체됨 |

### `deploy/` — 폐기 launchd plist

이미 `launchctl unload` 처리된 plist 들. 실수로 다시 load 하지 않도록 보존.
