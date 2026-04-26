"""
공포탐욕지수 (Fear & Greed Index) 클라이언트.

데이터 소스: Alternative.me API (https://api.alternative.me/fng/)
  - 무료, 무제한, 매일 업데이트
  - 0~100 사이 정수값 + 분류 (Extreme Fear / Fear / Neutral / Greed / Extreme Greed)
  - 주의: 원본은 암호화폐 시장용. 글로벌 위험선호도 프록시로 활용.

캐시: 하루 1회 호출, 로컬 JSON 파일에 저장.

활용 방식:
  - get_value() → 현재값 0~100
  - is_extreme_fear() → 25 미만
  - is_extreme_greed() → 75 초과
  - position_size_modifier() → 사이즈 배율 (0.0 ~ 1.3)
  - should_block_entry() → 진입 차단 여부
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import requests

_CACHE_FILE = Path(__file__).parent.parent / ".fear_greed_cache.json"
_API_URL = "https://api.alternative.me/fng/?limit=1"
_TIMEOUT = 5.0
_FALLBACK = {"value": 50, "classification": "Neutral", "error": "fetch failed"}


def _load_cache() -> dict | None:
    if not _CACHE_FILE.exists():
        return None
    try:
        data = json.loads(_CACHE_FILE.read_text())
        today = date.today().isoformat()
        if data.get("date") == today:
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _save_cache(payload: dict) -> None:
    try:
        _CACHE_FILE.write_text(json.dumps(payload))
    except OSError:
        pass


def fetch_index(force: bool = False) -> dict:
    """현재 F&G 지수. 캐시 있으면 재사용."""
    if not force:
        cached = _load_cache()
        if cached is not None:
            return cached

    today = date.today().isoformat()
    try:
        r = requests.get(_API_URL, timeout=_TIMEOUT)
        r.raise_for_status()
        raw = r.json()["data"][0]
        result = {
            "date": today,
            "value": int(raw["value"]),
            "classification": raw["value_classification"],
            "fetched_at": datetime.now().isoformat(),
            "source": "alternative.me",
        }
        _save_cache(result)
        return result
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return {**_FALLBACK, "date": today}


def get_value() -> int:
    return int(fetch_index().get("value", 50))


def get_classification() -> str:
    return fetch_index().get("classification", "Neutral")


def is_extreme_fear() -> bool:
    return get_value() < 25


def is_extreme_greed() -> bool:
    return get_value() > 75


def should_block_entry() -> bool:
    """Extreme Greed (>80) 시 진입 차단."""
    return get_value() > 80


def position_size_modifier() -> float:
    """
    F&G 기반 사이즈 배율.

    Extreme Fear (<20):   1.3 (30% 더 크게, 저점 매수 강화)
    Fear (20-30):         1.15 (15% 더)
    Neutral (30-70):      1.0 (그대로)
    Greed (70-80):        0.5 (절반, 과열 경계)
    Extreme Greed (>80):  0.0 (진입 차단)
    """
    val = get_value()
    if val < 20:
        return 1.3
    if val < 30:
        return 1.15
    if val > 80:
        return 0.0
    if val > 70:
        return 0.5
    return 1.0


def status_text() -> str:
    """Telegram 등에 표시할 한 줄 요약."""
    info = fetch_index()
    val = info.get("value", 50)
    cls = info.get("classification", "?")
    if "error" in info:
        return f"F&G: 데이터 없음 ({info.get('error', '')})"
    emoji = "😱" if val < 25 else "😨" if val < 45 else "😐" if val < 55 else "🤑" if val < 75 else "🚨"
    return f"F&G: {val} ({cls}) {emoji}"


def fetch_history(limit: int = 0) -> dict[str, int]:
    """
    F&G 과거 히스토리를 dict 로 반환. {YYYY-MM-DD: value}.

    limit=0 이면 전체 (Alternative.me 는 2018-02 부터 시작).
    백테스트용.

    [B6 fix] timestamp → UTC date 명시 변환.
      - alternative.me timestamp 는 그 날의 00:00:00 UTC 기준
      - 이전엔 datetime.fromtimestamp() (로컬 timezone) 사용 → 머신
        timezone 에 따라 날짜 ±1일 흔들림
      - 이제 UTC 명시 → 백테스트가 어느 머신에서 돌아도 일관된 매핑
    """
    url = f"https://api.alternative.me/fng/?limit={limit}"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json().get("data", [])
    except (requests.RequestException, KeyError, ValueError):
        return {}

    result: dict[str, int] = {}
    for d in data:
        try:
            ts = int(d["timestamp"])
            # UTC 명시 (B6 fix): alternative.me ts = 그 날 00:00 UTC
            date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            result[date_str] = int(d["value"])
        except (KeyError, ValueError, TypeError):
            continue
    return result


def main() -> int:
    """단독 실행 시 현재 지수 출력."""
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "history":
        history = fetch_history(limit=0)
        print(f"F&G 히스토리: {len(history)}일 (가장 오래된 → 최신)")
        if history:
            sorted_dates = sorted(history.keys())
            print(f"  시작: {sorted_dates[0]} (값 {history[sorted_dates[0]]})")
            print(f"  최신: {sorted_dates[-1]} (값 {history[sorted_dates[-1]]})")
            # 극단 카운트
            extreme_fear = sum(1 for v in history.values() if v <= 7)
            very_low = sum(1 for v in history.values() if 7 < v <= 25)
            high = sum(1 for v in history.values() if 75 <= v < 88)
            extreme_greed = sum(1 for v in history.values() if v >= 88)
            print(f"\n  극단 공포 (≤7) : {extreme_fear}일")
            print(f"  공포 (8~25)    : {very_low}일")
            print(f"  탐욕 (75~87)   : {high}일")
            print(f"  극단 탐욕 (≥88): {extreme_greed}일")
        return 0

    info = fetch_index(force=True)
    print(f"공포탐욕지수: {info.get('value')} ({info.get('classification', '?')})")
    print(f"분류: {info.get('classification')}")
    print(f"갱신: {info.get('fetched_at', info.get('date'))}")
    print()
    print(f"진입 차단 여부: {should_block_entry()}")
    print(f"사이즈 배율: {position_size_modifier():.2f}")
    print(f"한 줄 요약: {status_text()}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
