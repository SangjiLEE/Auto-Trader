"""한국/미국 시장 운영시간 + 휴장일 + KIS 모의투자 가드.

자체 정적 캘린더 (외부 의존성 추가 회피). 매년 휴장일 데이터 갱신 필요.
2027년 이후 새 연도 추가 시 KR_HOLIDAYS_<YEAR> / US_HOLIDAYS_<YEAR> 딕셔너리 추가.

사용:
    from . import market_hours

    # 매매 진입점 (KR)
    market_hours.assert_kis_paper_market_open()  # 14:30 외, 휴장일에 RuntimeError

    # 휴장일 체크 (trigger_check, healthcheck 등)
    if not market_hours.is_kr_market_open():
        return  # KR 휴장 — false alarm 발생 안 함

알려진 한계:
- 한국 임시공휴일 (선거일 등) 자동 추적 불가 — 매년 수동 업데이트
- KIS 모의투자 운영 시간 정확하지 않음 — 09:00~14:30 안전 가정
"""
from __future__ import annotations

from datetime import date, datetime, time

# 한국거래소 정규장 휴장일 (2026)
# Source: https://open.krx.co.kr/
KR_HOLIDAYS_2026 = frozenset({
    date(2026, 1, 1),    # 신정
    date(2026, 2, 16),   # 설날
    date(2026, 2, 17),   # 설날 대체
    date(2026, 2, 18),   # 설날 대체
    date(2026, 3, 1),    # 삼일절
    date(2026, 3, 2),    # 삼일절 대체
    date(2026, 5, 5),    # 어린이날
    date(2026, 5, 25),   # 부처님오신날
    date(2026, 6, 6),    # 현충일
    date(2026, 8, 15),   # 광복절 (토)
    date(2026, 8, 17),   # 광복절 대체
    date(2026, 9, 24),   # 추석
    date(2026, 9, 25),   # 추석
    date(2026, 9, 26),   # 추석 (토 - 거래소는 토요일도 표시)
    date(2026, 10, 3),   # 개천절 (토)
    date(2026, 10, 5),   # 개천절 대체
    date(2026, 10, 9),   # 한글날
    date(2026, 12, 25),  # 크리스마스
    date(2026, 12, 31),  # 연말 휴장
})

# NYSE / NASDAQ 정규장 휴장일 (2026)
# Source: https://www.nyse.com/markets/hours-calendars
US_HOLIDAYS_2026 = frozenset({
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Day
    date(2026, 2, 16),   # Presidents Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day observed (7/4 = Sat)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
})

# 연도별 캘린더 dispatch — 2027 이후 추가 시 여기 매핑 늘리기
_KR_HOLIDAYS_BY_YEAR = {2026: KR_HOLIDAYS_2026}
_US_HOLIDAYS_BY_YEAR = {2026: US_HOLIDAYS_2026}

# KIS 모의투자 안전 운영 시간 (KST)
# 정확한 cutoff 모름 → 보수적 14:30 까지 가정 (실제 정규장은 15:30 마감)
PAPER_MARKET_OPEN = time(9, 0)
PAPER_MARKET_CLOSE = time(14, 30)


def is_kr_market_open(d: date | None = None) -> bool:
    """한국거래소 정규장 영업일 여부. 주말 / 휴장일이면 False."""
    if d is None:
        d = date.today()
    if d.weekday() >= 5:  # Sat=5, Sun=6
        return False
    holidays = _KR_HOLIDAYS_BY_YEAR.get(d.year, frozenset())
    return d not in holidays


def is_us_market_open(d: date | None = None) -> bool:
    """미국 정규장 (NYSE/NASDAQ) 영업일 여부. 주말 / 휴장일이면 False."""
    if d is None:
        d = date.today()
    if d.weekday() >= 5:
        return False
    holidays = _US_HOLIDAYS_BY_YEAR.get(d.year, frozenset())
    return d not in holidays


def is_kis_paper_open(now: datetime | None = None) -> bool:
    """KIS 모의투자 운영 시간 + 한국 영업일."""
    if now is None:
        now = datetime.now()
    if not is_kr_market_open(now.date()):
        return False
    return PAPER_MARKET_OPEN <= now.time() <= PAPER_MARKET_CLOSE


def assert_kis_paper_market_open() -> None:
    """KIS 모의투자 운영 시간 외 / 한국 휴장일 호출 시 RuntimeError.

    수동 실행이나 launchd 가 잘못된 시간에 trigger 한 경우 즉각 차단.
    KIS 가 응답으로 거부하기 전에 클라이언트에서 빠른 fail.
    """
    now = datetime.now()
    if not is_kr_market_open(now.date()):
        raise RuntimeError(
            f"한국 거래소 휴장일 ({now.date()}). 매매 작업 진행 불가."
        )
    t = now.time()
    if not (PAPER_MARKET_OPEN <= t <= PAPER_MARKET_CLOSE):
        raise RuntimeError(
            f"KIS 모의투자 운영 시간 외 (현재 {t.strftime('%H:%M')}, "
            f"운영 시간 {PAPER_MARKET_OPEN.strftime('%H:%M')}~"
            f"{PAPER_MARKET_CLOSE.strftime('%H:%M')} KST)."
        )
