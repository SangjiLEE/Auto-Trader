"""pytest 공통 fixture.

각 테스트마다 독립된 임시 DB 사용 (실제 data.db 오염 방지).
KIS_ENV 는 'paper' 로 고정.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# .env 로드 막기 (테스트는 환경 격리)
os.environ["KIS_ENV"] = "paper"
os.environ["KIS_PAPER_APP_KEY"] = "test"
os.environ["KIS_PAPER_APP_SECRET"] = "test"
os.environ["KIS_PAPER_ACCOUNT_NO"] = "12345678-01"
os.environ["KIS_PAPER_BASE_URL"] = "https://example.com"
# Slack/Telegram 비활성
for k in list(os.environ):
    if k.startswith("SLACK_WEBHOOK_") or k.startswith("TELEGRAM_"):
        del os.environ[k]

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """각 테스트마다 깨끗한 임시 DB."""
    from src import db

    test_db = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", test_db)
    db.init_schema()
    yield test_db
