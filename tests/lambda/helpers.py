import base64
import json

NOW = 1793460000
ENV = {
    "COGNITO_DOMAIN": "example.auth.ap-northeast-1.amazoncognito.com",
    "CLIENT_ID": "client123",
    "ISSUER": "https://cognito-idp.ap-northeast-1.amazonaws.com/pool",
    "REDIRECT_URI": "https://api.example.invalid/callback",
    "QUEUE_URL": "https://sqs.example.invalid/queue",
    "ALLOWED_GROUP": "minecraft",
    "MAX_AUTH_AGE": "120",
    "GRANT_TTL": "600",
}


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(claims: dict) -> str:
    return ".".join([b64(b'{"alg":"none"}'), b64(json.dumps(claims).encode()), "sig"])


def good_claims(nonce="N0NCE", **patch) -> dict:
    claims = {
        "iss": ENV["ISSUER"], "aud": ENV["CLIENT_ID"], "token_use": "id",
        "exp": NOW + 3000, "nonce": nonce, "auth_time": NOW - 10,
        "cognito:groups": ["minecraft"], "cognito:username": "alice",
    }
    claims.update(patch)
    return claims
