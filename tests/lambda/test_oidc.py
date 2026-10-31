import base64
import hashlib
import io
import json
import urllib.error
import urllib.parse

import pytest
from .helpers import NOW, good_claims, make_jwt

from craftlatch_auth import oidc, settings


def test_pkce_s256():
    verifier, challenge = oidc.new_pkce()
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def test_authorize_url(env):
    s = settings.load()
    url = oidc.authorize_url(s, "ST", "NO", "CH")
    parts = urllib.parse.urlsplit(url)
    assert parts.netloc == env["COGNITO_DOMAIN"] and parts.path == "/oauth2/authorize"
    q = dict(urllib.parse.parse_qsl(parts.query))
    assert q == {
        "response_type": "code", "client_id": "client123",
        "redirect_uri": env["REDIRECT_URI"], "scope": "openid", "state": "ST",
        "nonce": "NO", "code_challenge": "CH", "code_challenge_method": "S256",
        "prompt": "login",
    }


def test_settings_invalid(env, monkeypatch):
    monkeypatch.setenv("MAX_AUTH_AGE", "abc")
    with pytest.raises(Exception):
        settings.load()
    monkeypatch.setenv("MAX_AUTH_AGE", "120")
    monkeypatch.delenv("QUEUE_URL")
    with pytest.raises(Exception):
        settings.load()


class FakeResp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_exchange_code_posts_form(env):
    seen = {}

    def fake(req, timeout=None):
        seen["req"], seen["timeout"] = req, timeout
        return FakeResp(json.dumps({"id_token": "tok"}).encode())

    out = oidc.exchange_code(settings.load(), "CODE", "VER", urlopen=fake)
    assert out["id_token"] == "tok"
    req = seen["req"]
    assert seen["timeout"] == 5
    assert req.full_url == f"https://{env['COGNITO_DOMAIN']}/oauth2/token"
    assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert dict(urllib.parse.parse_qsl(req.data.decode())) == {
        "grant_type": "authorization_code", "client_id": "client123",
        "code": "CODE", "redirect_uri": env["REDIRECT_URI"], "code_verifier": "VER",
    }


def test_exchange_code_errors(env):
    s = settings.load()

    def http(code):
        def f(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b"CODE secret"))
        return f

    with pytest.raises(oidc.AuthError) as e:
        oidc.exchange_code(s, "SECRETCODE", "SECRETVER", urlopen=http(400))
    assert e.value.kind == "relogin"
    with pytest.raises(oidc.AuthError) as e:
        oidc.exchange_code(s, "SECRETCODE", "SECRETVER", urlopen=http(500))
    assert e.value.kind == "temporary"
    assert "SECRET" not in str(e.value)

    def boom(req, timeout=None):
        raise OSError("down")

    with pytest.raises(oidc.AuthError) as e:
        oidc.exchange_code(s, "c", "v", urlopen=boom)
    assert e.value.kind == "temporary"
    for body in (b"not json", b"[]", b'{"id_token": 1}', b"{}"):
        with pytest.raises(oidc.AuthError) as e:
            oidc.exchange_code(s, "c", "v", urlopen=lambda r, timeout=None, b=body: FakeResp(b))
        assert e.value.kind == "temporary"


def test_decode_claims():
    assert oidc.decode_claims(make_jwt({"a": 1})) == {"a": 1}
    for bad in ("x", "a.!!!.c", "a.bm90anNvbg.c", "a.W10.c"):
        with pytest.raises(oidc.AuthError) as e:
            oidc.decode_claims(bad)
        assert e.value.kind == "temporary"


def test_validate_ok(env):
    assert oidc.validate_claims(good_claims(), settings.load(), "N0NCE", NOW) == "alice"


@pytest.mark.parametrize("patch,kind", [
    ({"auth_time": NOW - 121}, "relogin"), ({"auth_time": NOW + 61}, "relogin"),
    ({"auth_time": True}, "relogin"), ({"auth_time": "1"}, "relogin"),
    ({"aud": ["client123"]}, "relogin"), ({"exp": "9" * 10}, "relogin"),
    ({"cognito:groups": ["a", 1]}, "forbidden"), ({"cognito:groups": ["minecraft", 1]}, "forbidden"),
    ({"cognito:username": "a b"}, "account"), ({"cognito:username": "a" * 129}, "account"),
    ({"cognito:username": "alice\n"}, "account"), ({"cognito:username": 5}, "account"),
])
def test_validate_failures(env, patch, kind):
    with pytest.raises(oidc.AuthError) as e:
        oidc.validate_claims(good_claims(**patch), settings.load(), "N0NCE", NOW)
    assert e.value.kind == kind


def test_validate_boundaries(env):
    s = settings.load()
    for t in (NOW - 120, NOW + 60):
        assert oidc.validate_claims(good_claims(auth_time=t), s, "N0NCE", NOW) == "alice"
