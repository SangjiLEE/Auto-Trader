"""
Sprint 1 Week 1: Hello World.

목적:
  1. .env 파일이 제대로 로드되는지 확인
  2. KIS Open API 네트워크 접근 가능한지 확인
  3. OAuth2 인증 플로우가 정상 동작하는지 확인

주문은 아직 안 한다. 토큰만 받으면 성공.

실행:
  python -m src.hello_world
"""
import sys

from . import config
from . import kis_auth


def main() -> int:
    print("=" * 60)
    print("KIS Open API Hello World")
    print("=" * 60)

    try:
        config.validate()
    except ValueError as e:
        print(f"[설정 오류] {e}")
        print("\n.env 파일을 다시 확인하세요.")
        return 1

    mode = "모의투자" if config.KIS_ENV == "paper" else "실거래"
    print(f"환경: {config.KIS_ENV} ({mode})")
    print(f"Base URL: {config.BASE_URL}")
    print(f"계좌번호: {config.CANO}-{config.ACNT_PRDT_CD}")
    print()

    print("인증 요청 중...")
    try:
        token = kis_auth.get_access_token()
    except kis_auth.KISAuthError as e:
        print(f"[인증 실패] {e}")
        return 2

    print(f"토큰 발급 성공 (길이: {len(token)} 문자)")
    print(f"토큰 앞 20자: {token[:20]}...")
    print()
    print("Hello World 완료. 인증 플로우 작동.")
    print("다음 단계: 계좌 잔고 조회.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
