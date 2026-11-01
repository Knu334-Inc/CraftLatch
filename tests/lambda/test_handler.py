import json
import subprocess
import sys
import urllib.parse
import zipfile
from pathlib import Path

import pytest
from .helpers import ENV, NOW, good_claims, make_jwt

from craftlatch_auth import cookies, handler

SEC = {"content-type", "content-security-policy", "cache-control", "referrer-policy",
       "x-content-type-options"}
STATE, NONCE, VER = "STATE", "N0NCE", "VERIFIER"


class FakeSqs:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_message(self, **kw):
        if self.fail:
            raise RuntimeError("sqs down")
        self.sent.append(kw)


class Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def ctx(env, monkeypatch):
    sqs = FakeSqs()
    monkeypatch.setattr(handler, "_sqs_client", lambda: sqs)
    monkeypatch.setattr(handler, "now_fn", lambda: NOW)
    state = {"claims": good_claims()}

    def fake_urlopen(req, timeout=None):
        return Resp(json.dumps({"id_token": make_jwt(state["claims"])}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return sqs, state


def callback_event(ip="8.8.8.8", state=STATE, cookie_state=STATE, headers=None, qs=None):
    cookie = cookies.encode(cookie_state, NONCE, VER, NOW).split(";")[0]
    q = {"code": "CODE", "state": state}
    if qs is not None:
        q = qs
    return {"routeKey": "GET /callback", "queryStringParameters": q, "cookies": [cookie],
            "headers": headers or {}, "requestContext": {"http": {"sourceIp": ip}}}


def test_login_redirect_and_cookie(env, monkeypatch):
    monkeypatch.setattr(handler, "now_fn", lambda: NOW)
    r = handler.lambda_handler({"routeKey": "GET /login"}, None)
    assert r["statusCode"] == 302
    loc = r["headers"]["location"]
    for frag in ("prompt=login", "code_challenge_method=S256", "scope=openid"):
        assert frag in loc
    assert SEC - {"content-type"} <= set(r["headers"])
    assert len(r["cookies"]) == 1 and r["cookies"][0].startswith("__Host-craftlatch=")
    c = cookies.decode([r["cookies"][0].split(";")[0]])
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(loc).query))
    assert c["s"] == q["state"] and c["n"] == q["nonce"] and c["t"] == NOW


def test_callback_success_sends_message(ctx, caplog):
    sqs, _ = ctx
    caplog.set_level("INFO")
    r = handler.lambda_handler(callback_event(), None)
    assert r["statusCode"] == 200
    assert r["headers"]["content-type"] == "text/html; charset=utf-8"
    assert "8.8.8.8" in r["body"]
    assert r["cookies"] == ["__Host-craftlatch=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Lax"]
    assert len(sqs.sent) == 1
    kw = sqs.sent[0]
    assert kw["QueueUrl"] == ENV["QUEUE_URL"]
    body = json.loads(kw["MessageBody"])
    assert kw["MessageBody"] == json.dumps(body, separators=(",", ":"))
    uid = body.pop("id")
    import uuid
    assert str(uuid.UUID(uid)) == uid
    assert body == {"v": 1, "type": "allow", "ip": "8.8.8.8", "iat": NOW, "exp": NOW + 600, "user": "alice"}
    text = caplog.text
    assert "event=grant result=ok" in text and "user=alice" in text and "ip=8.8.8.8" in text
    assert "CODE" not in text and STATE not in text


def test_callback_state_mismatch_returns_relogin_page(ctx):
    sqs, _ = ctx
    r = handler.lambda_handler(callback_event(state="OTHER"), None)
    assert r["statusCode"] == 400 and "<html" in r["body"]
    assert sqs.sent == []


def test_callback_missing_inputs(ctx):
    sqs, _ = ctx
    ev = callback_event()
    del ev["cookies"]
    assert handler.lambda_handler(ev, None)["statusCode"] == 400
    assert handler.lambda_handler(callback_event(qs={"state": STATE}), None)["statusCode"] == 400
    assert handler.lambda_handler(callback_event(qs={"error": "access_denied"}), None)["statusCode"] == 400
    ev = callback_event()
    ev["queryStringParameters"] = None
    assert handler.lambda_handler(ev, None)["statusCode"] == 400
    old = callback_event()
    old["cookies"] = [cookies.encode(STATE, NONCE, VER, NOW - 601).split(";")[0]]
    assert handler.lambda_handler(old, None)["statusCode"] == 400
    fut = callback_event()
    fut["cookies"] = [cookies.encode(STATE, NONCE, VER, NOW + 61).split(";")[0]]
    assert handler.lambda_handler(fut, None)["statusCode"] == 400
    assert sqs.sent == []


@pytest.mark.parametrize("patch,code", [
    ({"auth_time": NOW - 121}, 400), ({"auth_time": NOW + 61}, 400), ({"auth_time": True}, 400),
    ({"auth_time": "1793460000"}, 400),
    ({"iss": "https://cognito-idp.ap-northeast-1.amazonaws.com/other"}, 400),
    ({"exp": NOW}, 400), ({"nonce": "x"}, 400), ({"aud": "other"}, 400),
    ({"token_use": "access"}, 400), ({"cognito:groups": ["other"]}, 403),
    ({"cognito:groups": "minecraft"}, 403), ({"cognito:username": "アリス"}, 500),
])
def test_claim_failures(ctx, patch, code):
    sqs, state = ctx
    state["claims"] = good_claims(**patch)
    r = handler.lambda_handler(callback_event(), None)
    assert r["statusCode"] == code
    assert sqs.sent == []


@pytest.mark.parametrize("ip", ["2001:db8::1", "10.0.0.5", "garbage", ""])
def test_non_ipv4_or_private_source(ctx, ip):
    sqs, _ = ctx
    r = handler.lambda_handler(callback_event(ip=ip), None)
    assert r["statusCode"] == 400 and "IPv4" in r["body"]
    assert sqs.sent == []


def test_token_exchange_error_temporary(ctx, monkeypatch):
    sqs, _ = ctx

    def boom(req, timeout=None):
        raise OSError("down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    r = handler.lambda_handler(callback_event(), None)
    assert r["statusCode"] == 503 and sqs.sent == []


def test_sqs_failure_temporary(ctx, monkeypatch):
    monkeypatch.setattr(handler, "_sqs_client", lambda: FakeSqs(fail=True))
    assert handler.lambda_handler(callback_event(), None)["statusCode"] == 503


def test_ignores_x_forwarded_for(ctx):
    sqs, _ = ctx
    ev = callback_event(headers={"x-forwarded-for": "9.9.9.9"})
    assert handler.lambda_handler(ev, None)["statusCode"] == 200
    assert json.loads(sqs.sent[0]["MessageBody"])["ip"] == "8.8.8.8"


def test_unknown_route_404(env):
    for rk in ("GET /other", "POST /login", "$default"):
        r = handler.lambda_handler({"routeKey": rk}, None)
        assert r["statusCode"] == 404
        assert SEC <= set(r["headers"])


def test_security_headers_present(ctx):
    r = handler.lambda_handler(callback_event(), None)
    assert r["headers"] == {
        "content-type": "text/html; charset=utf-8",
        "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'",
        "cache-control": "no-store", "referrer-policy": "no-referrer",
        "x-content-type-options": "nosniff",
    }
    assert "<script" not in r["body"]


def test_zip_layout_importable(tmp_path):
    src = Path(__file__).resolve().parents[2] / "src" / "lambda"
    zpath = tmp_path / "lambda.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        for p in sorted(src.rglob("*")):
            rel = p.relative_to(src)
            if p.is_dir() or "__pycache__" in rel.parts or p.suffix == ".pyc":
                continue
            z.write(p, rel.as_posix())
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        assert "craftlatch_auth/handler.py" in names and "handler.py" not in names
        out = tmp_path / "out"
        z.extractall(out)
    code = (f"import sys; sys.path.insert(0, {str(out)!r}); import craftlatch_auth.handler as h; "
            f"assert h.__file__.startswith({str(out)!r}); assert callable(h.lambda_handler)")
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("key", ["s", "n", "v"])
@pytest.mark.parametrize("bad", ["\\ud800", "\\u3042"])
def test_callback_bad_cookie_strings_relogin(ctx, key, bad):
    from .helpers import b64
    sqs, _ = ctx
    d = {"s": STATE, "n": NONCE, "v": VER, "t": NOW}
    parts = {k: json.dumps(v) for k, v in d.items()}
    parts[key] = f'"{bad}"'
    raw = "{" + ",".join(f'"{k}":{v}' for k, v in parts.items()) + "}"
    ev = callback_event()
    ev["cookies"] = ["__Host-craftlatch=" + b64(raw.encode())]
    r = handler.lambda_handler(ev, None)
    assert r["statusCode"] == 400
    assert sqs.sent == []


def test_unexpected_exception_returns_temporary(ctx, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(handler, "_callback", boom)
    r = handler.lambda_handler(callback_event(), None)
    assert r["statusCode"] == 503
