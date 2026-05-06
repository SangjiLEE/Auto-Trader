"""실거래 가드 — KIS_ENV=paper 가 아니면 주문 차단.

모든 실주문 진입점 (CLI / 내부 주문 함수) 은 이 모듈을 거쳐야 함.
신규 전략 추가 시 가드 누락으로 인한 실거래 사고 방지가 목적.

두 종류의 가드 제공:
  1. block_execute_if_real(args.execute) — CLI 진입점 (return 1 패턴)
  2. assert_paper()                       — 주문 전송 함수 내부 (raise)
"""
from __future__ import annotations

from . import config

PAPER_ENV = "paper"


def is_paper() -> bool:
    """현재 KIS_ENV 가 paper 면 True."""
    return config.KIS_ENV == PAPER_ENV


def assert_paper(*, label: str = "주문") -> None:
    """주문 전송 함수 진입점에서 호출. paper 가 아니면 RuntimeError.

    이 가드는 절대 우회되면 안 되는 안전선. CLI 가드를 통과해도
    주문 함수가 별도로 다시 검사한다 (방어 심층).
    """
    if not is_paper():
        raise RuntimeError(
            f"[차단] {label}: 실거래 모드. .env 의 KIS_ENV=paper 확인."
        )


def block_execute_if_real(execute: bool) -> bool:
    """CLI --execute 가드. 차단해야 하면 True 반환.

    Usage:
        if safety.block_execute_if_real(args.execute):
            return 1
    """
    if execute and not is_paper():
        print(
            f"[차단] 실거래 모드 (KIS_ENV={config.KIS_ENV}). "
            "--execute 는 paper 모드에서만 허용."
        )
        return True
    return False
