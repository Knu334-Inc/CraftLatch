"""環境変数から設定を読み込む。"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    cognito_domain: str
    client_id: str
    issuer: str
    redirect_uri: str
    queue_url: str
    allowed_group: str
    max_auth_age: int
    grant_ttl: int


def _str(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"環境変数 {name} が未設定です")
    return value


def _int(name: str) -> int:
    try:
        value = int(_str(name))
    except ValueError:
        raise ValueError(f"環境変数 {name} が整数ではありません") from None
    if value <= 0:
        raise ValueError(f"環境変数 {name} は正の整数にしてください")
    return value


def load() -> Settings:
    return Settings(
        cognito_domain=_str("COGNITO_DOMAIN"),
        client_id=_str("CLIENT_ID"),
        issuer=_str("ISSUER"),
        redirect_uri=_str("REDIRECT_URI"),
        queue_url=_str("QUEUE_URL"),
        allowed_group=_str("ALLOWED_GROUP"),
        max_auth_age=_int("MAX_AUTH_AGE"),
        grant_ttl=_int("GRANT_TTL"),
    )
