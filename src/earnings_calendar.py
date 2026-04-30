"""
실적 발표 캘린더 (Earnings Calendar) — Phase B-Lite catalyst 신호.

[Phase B-Lite] 진짜 catalyst (실적 발표) 기반 매매 신호.

데이터 소스:
  yfinance.Ticker(sym).calendar — 다음 실적 발표일 (1개)
  yfinance 백업: get_earnings_dates(limit=N) — 과거+미래 N개

룰:
  1. 매일 실행 시 universe 종목들의 다음 실적 발표일 조회
  2. 발표일 ±N일 이내면 "catalyst window" 진입
  3. catalyst window 내에서 매수 시그널 평가 (외부 모듈에서)

캐시: 1일 1회 호출, 로컬 JSON 저장. yfinance API rate limit 회피.

운영:
  python -m src.earnings_calendar  # 다음 실적 일정 조회
  python -m src.earnings_calendar --refresh  # 강제 갱신
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

_CACHE_FILE = Path(__file__).parent.parent / ".earnings_cache.json"


# Phase D 백테스트에서 양수 EV 확인된 universe (ORCL 제외)
DEFAULT_UNIVERSE = ["NVDA", "TSLA", "AAPL", "SOXL", "TSLL", "IREN", "BMNR"]


def _load_cache() -> dict | None:
    if not _CACHE_FILE.exists():
        return None
    try:
        data = json.loads(_CACHE_FILE.read_text())
        if data.get("date") == date.today().isoformat():
            return data.get("calendar", {})
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _save_cache(calendar: dict) -> None:
    try:
        _CACHE_FILE.write_text(json.dumps({
            "date": date.today().isoformat(),
            "calendar": calendar,
        }, default=str))
    except OSError:
        pass


def fetch_earnings_dates(symbols: list[str], force: bool = False) -> dict[str, list[str]]:
    """
    각 종목의 미래 실적 발표일 (오늘 ± 365일).

    반환: {symbol: [YYYY-MM-DD, ...]} (정렬됨)

    네트워크 실패 시 빈 dict. 캐시 1일.
    """
    if not force:
        cached = _load_cache()
        if cached is not None:
            return {s: cached.get(s, []) for s in symbols}

    try:
        import yfinance as yf
    except ImportError:
        print("[경고] yfinance 미설치. pip install yfinance 필요.")
        return {s: [] for s in symbols}

    result: dict[str, list[str]] = {}
    today = date.today()
    cutoff_past = today - timedelta(days=30)
    cutoff_future = today + timedelta(days=180)

    for sym in symbols:
        try:
            ticker = yf.Ticker(sym)
            # 과거+미래 12개 이벤트 시도
            try:
                ed = ticker.get_earnings_dates(limit=12)
            except Exception:
                ed = None

            dates = []
            if ed is not None and not ed.empty:
                for ts in ed.index:
                    try:
                        d = ts.date()
                        if cutoff_past <= d <= cutoff_future:
                            dates.append(d.isoformat())
                    except Exception:
                        continue

            # calendar (다음 1개) 백업
            try:
                cal = ticker.calendar
                if isinstance(cal, dict) and "Earnings Date" in cal:
                    val = cal["Earnings Date"]
                    if isinstance(val, list) and val:
                        for v in val:
                            try:
                                if hasattr(v, 'isoformat'):
                                    iso = v.isoformat()
                                else:
                                    iso = str(v)
                                if iso not in dates:
                                    dates.append(iso)
                            except Exception:
                                continue
            except Exception:
                pass

            result[sym] = sorted(set(dates))
        except Exception as e:
            print(f"  [경고] {sym} earnings 조회 실패: {e}")
            result[sym] = []

    _save_cache(result)
    return result


def is_in_catalyst_window(
    symbol_dates: list[str],
    today: date | None = None,
    days_before: int = 1,
    days_after: int = 2,
) -> tuple[bool, str | None, int]:
    """
    오늘이 실적 발표 ±N일 이내인지.

    days_before=1: 발표일 전날부터 catalyst window 활성
    days_after=2: 발표일 + 2일 까지 catalyst window 활성 (PEAD 효과 활용)

    반환: (in_window, nearest_earnings_date, days_to_event)
      days_to_event: 음수 = 이미 발표됨, 양수 = 발표 전, 0 = 오늘
    """
    if today is None:
        today = date.today()
    if not symbol_dates:
        return False, None, 0

    nearest = None
    nearest_delta = None
    for d_str in symbol_dates:
        try:
            d = date.fromisoformat(d_str.split("T")[0])
        except (ValueError, AttributeError):
            continue
        delta = (d - today).days
        if nearest_delta is None or abs(delta) < abs(nearest_delta):
            nearest_delta = delta
            nearest = d_str

    if nearest is None or nearest_delta is None:
        return False, None, 0

    # In window: -days_after ≤ delta ≤ days_before
    in_window = -days_after <= nearest_delta <= days_before
    return in_window, nearest, nearest_delta


def main() -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="실적 캘린더 조회")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_UNIVERSE)
    parser.add_argument("--refresh", action="store_true", help="캐시 무시")
    args = parser.parse_args()

    print("=" * 70)
    print(f"실적 캘린더 ({len(args.symbols)} 종목)")
    print("=" * 70)

    cal = fetch_earnings_dates(args.symbols, force=args.refresh)

    today = date.today()
    print(f"\n오늘: {today.isoformat()}")
    print(f"\n{'심볼':<8} {'다음 실적':<14} {'D-day':>8} {'Catalyst':>10}")
    print("-" * 50)

    for sym in args.symbols:
        dates = cal.get(sym, [])
        if not dates:
            print(f"{sym:<8} {'없음':<14}")
            continue
        in_win, near, delta = is_in_catalyst_window(dates)
        flag = "✅ ACTIVE" if in_win else ""
        if near:
            d_label = "오늘" if delta == 0 else f"{delta:+}일"
            print(f"{sym:<8} {near:<14} {d_label:>8} {flag:>10}")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
