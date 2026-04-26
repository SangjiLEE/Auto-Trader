"""
.env 파일에서 설정을 읽는 모듈.
다른 코드는 여기서 재노출한 값만 참조. os.getenv를 여기저기 흩뿌리지 않는다.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")

KIS_ENV = os.getenv("KIS_ENV", "paper").lower()

if KIS_ENV == "paper":
    APP_KEY = os.getenv("KIS_PAPER_APP_KEY")
    APP_SECRET = os.getenv("KIS_PAPER_APP_SECRET")
    ACCOUNT_NO = os.getenv("KIS_PAPER_ACCOUNT_NO")
    BASE_URL = os.getenv(
        "KIS_PAPER_BASE_URL",
        "https://openapivts.koreainvestment.com:29443",
    )
elif KIS_ENV == "real":
    APP_KEY = os.getenv("KIS_REAL_APP_KEY")
    APP_SECRET = os.getenv("KIS_REAL_APP_SECRET")
    ACCOUNT_NO = os.getenv("KIS_REAL_ACCOUNT_NO")
    BASE_URL = os.getenv(
        "KIS_REAL_BASE_URL",
        "https://openapi.koreainvestment.com:9443",
    )
else:
    raise ValueError(
        f"KIS_ENV는 'paper' 또는 'real'이어야 합니다. 현재 값: {KIS_ENV}"
    )

# "12345678-01" 형식을 CANO(앞 8자리)와 ACNT_PRDT_CD(뒤 2자리)로 분리.
# KIS API는 이 둘을 별도 파라미터로 요구.
if ACCOUNT_NO and "-" in ACCOUNT_NO:
    CANO, ACNT_PRDT_CD = ACCOUNT_NO.split("-")
else:
    CANO = ACCOUNT_NO or ""
    ACNT_PRDT_CD = "01"


def validate() -> None:
    """필수 값이 채워졌는지 체크. 템플릿 그대로면 에러."""
    problems = []
    if not APP_KEY or APP_KEY.startswith("your_"):
        problems.append(f"KIS_{KIS_ENV.upper()}_APP_KEY 비어있거나 템플릿 값")
    if not APP_SECRET or APP_SECRET.startswith("your_"):
        problems.append(f"KIS_{KIS_ENV.upper()}_APP_SECRET 비어있거나 템플릿 값")
    if not CANO or CANO in ("", "12345678", "50123456"):
        problems.append(f"KIS_{KIS_ENV.upper()}_ACCOUNT_NO 비어있거나 템플릿 값")
    if problems:
        msg = "\n - ".join(problems)
        raise ValueError(f".env 설정 문제:\n - {msg}")
