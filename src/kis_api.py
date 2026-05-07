"""
KIS Open API 공통 호출 헬퍼.

- get(): 조회용 GET (시세, 잔고 등)
- post(): 주문·정정·취소용 POST. hashkey 자동 발급·첨부.
- Rate limit (EGW00201) 발생 시 자동 재시도 (지수 백오프).

에러는 서버 응답 본문(msg_cd, msg1)까지 메시지에 담아 던진다.
"""
from __future__ import annotations

import time
from typing import Any, Callable

import requests

from . import config


class KISAPIError(Exception):
    """KIS API 호출 실패."""


# 재시도 대상 에러 코드 (주로 일시적 rate limit / 서버 부하)
_RETRY_CODES = ("EGW00201",)
_MAX_RETRIES = 4
_BASE_DELAY = 1.0  # 초


def _base_headers(tr_id: str, token: str) -> dict[str, str]:
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": config.APP_KEY or "",
        "appsecret": config.APP_SECRET or "",
        "tr_id": tr_id,
        "custtype": "P",
    }


def _extract_error(response: requests.Response) -> str:
    try:
        err = response.json()
        code = err.get("msg_cd", "?")
        msg = err.get("msg1") or err.get("error_description") or response.text[:300]
        return f"HTTP {response.status_code} [{code}] {msg}"
    except (ValueError, requests.exceptions.JSONDecodeError):
        return f"HTTP {response.status_code}: {response.text[:500]}"


def _check(response: requests.Response) -> dict[str, Any]:
    if response.status_code != 200:
        raise KISAPIError(_extract_error(response))

    data = response.json()
    rt_cd = data.get("rt_cd")
    if rt_cd is not None and rt_cd != "0":
        code = data.get("msg_cd", "?")
        msg = data.get("msg1", "?")
        raise KISAPIError(f"API 오류 [{code}] {msg}")
    return data


def _is_retriable(error: KISAPIError) -> bool:
    msg = str(error)
    return any(code in msg for code in _RETRY_CODES)


def _with_retry(
    request_fn: Callable[[], requests.Response],
    *,
    retry_on_network: bool = True,
) -> dict[str, Any]:
    """rate-limit / 일시 네트워크 장애 시 지수 백오프 재시도.

    POST (주문 전송) 의 경우 retry_on_network=False 로 호출 — 서버 도달
    후 응답 끊김 (RemoteDisconnected) 시 중복 주문 위험. 멱등성 보장
    안 되는 호출은 caller 가 명시적으로 끄고 직접 재실행 결정해야 함.

    GET 은 default True. 멱등 호출이라 안전.
    """
    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = request_fn()
            return _check(response)
        except KISAPIError as e:
            last_error = e
            if _is_retriable(e) and attempt < _MAX_RETRIES - 1:
                delay = _BASE_DELAY * (2 ** attempt)
                time.sleep(delay)
                continue
            raise
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as e:
            last_error = e
            if retry_on_network and attempt < _MAX_RETRIES - 1:
                delay = _BASE_DELAY * (2 ** attempt)
                time.sleep(delay)
                continue
            raise KISAPIError(
                f"네트워크 오류 (재시도 {attempt + 1}/{_MAX_RETRIES}): {e}"
            ) from e
    # 안전용 (위 루프가 반드시 return 또는 raise)
    assert last_error is not None
    if isinstance(last_error, KISAPIError):
        raise last_error
    raise KISAPIError(str(last_error))


def _get_hashkey(body: dict[str, Any]) -> str:
    """주문·정정·취소에 필수인 hashkey 발급."""
    url = f"{config.BASE_URL}/uapi/hashkey"
    headers = {
        "content-type": "application/json; charset=utf-8",
        "appkey": config.APP_KEY or "",
        "appsecret": config.APP_SECRET or "",
    }

    def _req() -> requests.Response:
        return requests.post(url, headers=headers, json=body, timeout=10)

    # hashkey 도 rate limit 대상이라 재시도 적용 (조회용, 멱등)
    try:
        data = _with_retry(_req, retry_on_network=True)
    except KISAPIError as e:
        raise KISAPIError(f"hashkey 발급 실패: {e}") from e

    hashkey = data.get("HASH")
    if not hashkey:
        raise KISAPIError(f"hashkey 응답 이상: {data}")
    return hashkey


def get(
    endpoint: str,
    tr_id: str,
    token: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """인증 GET. 자동 재시도 포함."""
    url = f"{config.BASE_URL}{endpoint}"
    headers = _base_headers(tr_id, token)

    def _req() -> requests.Response:
        return requests.get(url, headers=headers, params=params or {}, timeout=10)

    return _with_retry(_req)


def post(
    endpoint: str,
    tr_id: str,
    token: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """인증 POST. hashkey 자동 첨부 + rate-limit 재시도.

    네트워크 retry 는 OFF — 주문 전송 시 응답 끊김 (RemoteDisconnected)
    재시도 시 중복 주문 위험. 멱등 키 보장은 caller 의 책임.
    """
    hashkey = _get_hashkey(body)
    headers = _base_headers(tr_id, token)
    headers["hashkey"] = hashkey

    url = f"{config.BASE_URL}{endpoint}"

    def _req() -> requests.Response:
        return requests.post(url, headers=headers, json=body, timeout=10)

    return _with_retry(_req, retry_on_network=False)
