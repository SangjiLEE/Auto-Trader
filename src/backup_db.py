"""DB 백업 자동화.

매일 06:00 KST 실행 (launchd):
  data.db → backups/data-YYYY-MM-DD.db.gz

retention:
  - daily: 최근 7일
  - weekly: 일요일 백업은 4주 보존
  - monthly: 매월 1일 백업은 12개월 보존

`sqlite3.Connection.backup()` 으로 동시 쓰기 충돌 회피
(트랜잭션 중인 DB 도 안전하게 복사).

실행:
  python -m src.backup_db                # 백업 + retention 정리
  python -m src.backup_db --no-prune     # 정리 안함
  python -m src.backup_db --list         # 백업 목록만
"""
from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import db
from . import notify

BACKUP_DIR = Path(__file__).parent.parent / "backups"

DAILY_RETENTION_DAYS = 7
WEEKLY_RETENTION_DAYS = 28        # 4주 (일요일 백업)
MONTHLY_RETENTION_DAYS = 365      # 12개월 (매월 1일 백업)


def _today_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def _is_weekly(d: date) -> bool:
    """일요일 = weekday() 6."""
    return d.weekday() == 6


def _is_monthly(d: date) -> bool:
    return d.day == 1


def _parse_backup_date(name: str) -> date | None:
    """data-YYYY-MM-DD.db.gz → date or None."""
    try:
        stem = name.replace("data-", "").replace(".db.gz", "").replace(".db", "")
        return date.fromisoformat(stem)
    except ValueError:
        return None


def take_backup(target_dir: Path | None = None) -> Path:
    """현재 DB 를 백업. sqlite3 .backup → gzip → backups/data-YYYY-MM-DD.db.gz.

    같은 날짜 백업이 있으면 덮어씀 (멱등).
    """
    target_dir = target_dir or BACKUP_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / f"data-{_today_str()}.db.gz"

    if not db.DB_PATH.exists():
        raise FileNotFoundError(f"DB 파일 없음: {db.DB_PATH}")

    # 임시 복사본 (sqlite .backup 으로 안전하게)
    tmp_path = target_dir / f"data-{_today_str()}.db.tmp"
    src = sqlite3.connect(db.DB_PATH)
    dst = sqlite3.connect(tmp_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()

    # gzip 압축
    with open(tmp_path, "rb") as f_in, gzip.open(out_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    tmp_path.unlink()

    return out_path


def list_backups(target_dir: Path | None = None) -> list[tuple[date, Path, int]]:
    """백업 목록 정렬 (오래된 순). 반환: [(date, path, size_bytes), ...]"""
    target_dir = target_dir or BACKUP_DIR
    if not target_dir.exists():
        return []
    items = []
    for p in target_dir.iterdir():
        d = _parse_backup_date(p.name)
        if d is not None and p.is_file():
            items.append((d, p, p.stat().st_size))
    items.sort(key=lambda x: x[0])
    return items


def prune_backups(target_dir: Path | None = None) -> list[Path]:
    """retention 정책 적용. 삭제된 파일 list 반환.

    keep:
      - 최근 7일 daily
      - 28일 내 일요일 백업
      - 365일 내 매월 1일 백업
    그 외는 삭제.
    """
    target_dir = target_dir or BACKUP_DIR
    today = date.today()
    deleted: list[Path] = []

    for backup_date, path, _size in list_backups(target_dir):
        age = (today - backup_date).days
        keep = False

        if age <= DAILY_RETENTION_DAYS:
            keep = True
        elif _is_weekly(backup_date) and age <= WEEKLY_RETENTION_DAYS:
            keep = True
        elif _is_monthly(backup_date) and age <= MONTHLY_RETENTION_DAYS:
            keep = True

        if not keep:
            path.unlink()
            deleted.append(path)

    return deleted


def fmt_size(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if n < 1024:
            return f"{n:,.1f}{unit}"
        n /= 1024
    return f"{n:,.1f}TB"


@notify.with_error_alert("backup_db")
def main() -> int:
    parser = argparse.ArgumentParser(description="DB 백업")
    parser.add_argument("--no-prune", action="store_true", help="retention 정리 스킵")
    parser.add_argument("--list", action="store_true", help="백업 목록만")
    args = parser.parse_args()

    if args.list:
        backups = list_backups()
        if not backups:
            print(f"백업 없음 ({BACKUP_DIR})")
            return 0
        print(f"백업 목록 ({BACKUP_DIR}, {len(backups)}개):")
        total = 0
        today = date.today()
        for d, p, size in backups:
            age = (today - d).days
            tag = []
            if age <= DAILY_RETENTION_DAYS:
                tag.append("daily")
            if _is_weekly(d):
                tag.append("weekly")
            if _is_monthly(d):
                tag.append("monthly")
            print(f"  {d}  {fmt_size(size):>10}  [{','.join(tag) or '-'}]  {p.name}")
            total += size
        print(f"총합: {fmt_size(total)}")
        return 0

    try:
        out = take_backup()
        size = out.stat().st_size
        print(f"✅ 백업 완료: {out} ({fmt_size(size)})")
    except Exception as e:
        print(f"❌ 백업 실패: {e}")
        return 1

    if not args.no_prune:
        deleted = prune_backups()
        if deleted:
            print(f"🗑️  retention 정리: {len(deleted)}개 삭제")
            for p in deleted:
                print(f"    - {p.name}")
        else:
            print("retention OK (삭제 없음)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
