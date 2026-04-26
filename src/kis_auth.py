"""
KIS Open API OAuth2 인증.

토큰은 24시간 유효. KIS 서버는 토큰 발급에 분당 제한이 있어서
파일 캐시로 재사용한다. 캐시 만료 5분 전부터 새로 발급.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

from . import config

_CACHE_FILE = Path(__file__).parent.parent / ".token_cache.json"
_REFRESH_MARGIN_SEC = 300  # 만료 5분 전이면 재발급


class KISAuthError(Exception):
    """KIS 인증 실패."""


def get_access_token(force_refresh: bool = False) -> str:
    """access_token 반환. 유효한 캐시 있으면 재사용."""
    if not force_refresh:
        cached = _load_cached_token()
        if cached is not None:
            return cached

    token, expires_at = _request_new_token()
    _save_cached_token(token, expires_at)
    return token


def _load_cached_token() -> str | None:
    if not _CACHE_FILE.exists():
        return None
    try:
        data = json.loads(_CACHE_FILE.read_text())
    except json.JSONDecodeError:
        return None

    if data.get("env") != config.KIS_ENV:
        return None
    if time.time() >= data.get("expires_at", 0) - _REFRESH_MARGIN_SEC:
        return None
    return data.get("token")


def _save_cached_token(token: str, expires_at: float) -> None:
    _CACHE_FILE.write_text(
        json.dumps(
            {
                "env": config.KIS_ENV,
                "token": token,
                "expires_at": expires_at,
            }
        )
    )


def _request_new_token() -> tuple[str, float]:
    url = f"{config.BASE_URL}/oauth2/tokenP"
    payload = {
        "grant_type": "client_credentials",
        "appkey": config.APP_KEY,
        "appsecret": config.APP_SECRET,
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
    except requests.RequestException as e:
        raise KISAuthError(f"네트워크 오류: {e}") from e

    if response.status_code != 200:
        raise KISAuthError(
            f"인증 실패 ({response.status_code}): {response.text}"
        )

    data = response.json()
    token = data.get("access_token")
    if not token:
        raise KISAuthError(f"응답에 access_token 없음: {data}")

    expires_in = int(data.get("expires_in", 86400))
    return token, time.time() + expires_in
