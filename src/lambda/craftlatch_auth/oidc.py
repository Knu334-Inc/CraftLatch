"""OIDC Code + PKCE の補助関数。署名検証はしない（設計書 4.3）。"""
import base64
import binascii
import hashlib
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request

_USER_RE = re.compile(r"[A-Za-z0-9._@+-]{1,128}")


class AuthError(Exception):
    """kind は relogin / forbidden / account / temporary。"""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_url(s, state: str, nonce: str, challenge: str) -> str:
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": s.client_id,
        "redirect_uri": s.redirect_uri,
        "scope": "openid",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
    })
    return f"https://{s.cognito_domain}/oauth2/authorize?{query}"


def exchange_code(s, code: str, verifier: str, urlopen=urllib.request.urlopen) -> dict:
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": s.client_id,
        "code": code,
        "redirect_uri": s.redirect_uri,
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(
        f"https://{s.cognito_domain}/oauth2/token", data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # 400 は期限切れ・使用済みの認可コード。詳細（本文）は出力しない
        raise AuthError("relogin" if e.code == 400 else "temporary") from None
    except Exception:
        raise AuthError("temporary") from None
    if not isinstance(body, dict) or not isinstance(body.get("id_token"), str):
        raise AuthError("temporary")
    return body


def decode_claims(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, binascii.Error, AttributeError):
        raise AuthError("temporary") from None
    if not isinstance(claims, dict):
        raise AuthError("temporary")
    return claims


def validate_claims(claims: dict, s, nonce: str, now: int) -> str:
    exp, auth_time = claims.get("exp"), claims.get("auth_time")
    if not (
        claims.get("iss") == s.issuer
        and isinstance(claims.get("aud"), str) and claims["aud"] == s.client_id
        and claims.get("token_use") == "id"
        and type(exp) is int and exp > now
        and isinstance(claims.get("nonce"), str) and claims["nonce"] == nonce
        and type(auth_time) is int and now - s.max_auth_age <= auth_time <= now + 60
    ):
        raise AuthError("relogin")
    groups = claims.get("cognito:groups")
    if not (isinstance(groups, list) and all(isinstance(g, str) for g in groups)
            and s.allowed_group in groups):
        raise AuthError("forbidden")
    user = claims.get("cognito:username")
    if not isinstance(user, str) or not _USER_RE.fullmatch(user):
        raise AuthError("account")
    return user
