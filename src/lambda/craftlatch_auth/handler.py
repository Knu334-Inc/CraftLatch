"""Lambda craftlatch-auth のエントリーポイント。"""
import hmac
import ipaddress
import json
import logging
import secrets
import time
import urllib.request
import uuid

from . import cookies, oidc, pages, settings

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_client = None


def now_fn() -> int:
    return int(time.time())


def _sqs_client():
    global _client
    if _client is None:
        import boto3

        _client = boto3.client("sqs")
    return _client


def _urlopen(req, timeout=None):
    return urllib.request.urlopen(req, timeout=timeout)


def _log(result: str, **fields) -> None:
    parts = [f"event=grant result={result}"]
    parts += [f"{k}={v}" for k, v in fields.items() if v]
    logger.info(" ".join(parts))


def _login(s) -> dict:
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    verifier, challenge = oidc.new_pkce()
    headers = dict(pages.HEADERS)
    headers["location"] = oidc.authorize_url(s, state, nonce, challenge)
    return {
        "statusCode": 302,
        "headers": headers,
        "cookies": [cookies.encode(state, nonce, verifier, now_fn())],
        "body": "",
    }


def _callback(event, s) -> dict:
    now = now_fn()
    query = event.get("queryStringParameters") or {}
    c = cookies.decode(event.get("cookies") or [])
    code, state = query.get("code"), query.get("state")
    if (
        "error" in query or c is None or not code or not state
        or now - c["t"] > cookies.MAX_AGE or c["t"] > now + 60
        or not hmac.compare_digest(state.encode(), c["s"].encode())
    ):
        _log("relogin")
        return pages.render("relogin")
    user = ip = None
    try:
        tokens = oidc.exchange_code(s, code, c["v"], urlopen=_urlopen)
        claims = oidc.decode_claims(tokens["id_token"])
        user = oidc.validate_claims(claims, s, c["n"], now)
        raw_ip = ((event.get("requestContext") or {}).get("http") or {}).get("sourceIp")
        try:
            addr = ipaddress.ip_address(raw_ip)
        except ValueError:
            addr = None
        if not isinstance(addr, ipaddress.IPv4Address) or not addr.is_global:
            _log("not_ipv4", user=user)
            return pages.render("not_ipv4")
        ip = str(addr)
        grant_id = str(uuid.uuid4())
        body = json.dumps({
            "v": 1, "type": "allow", "ip": ip, "iat": now, "exp": now + s.grant_ttl,
            "id": grant_id, "user": user,
        }, separators=(",", ":"))
        _sqs_client().send_message(QueueUrl=s.queue_url, MessageBody=body)
    except oidc.AuthError as e:
        _log(e.kind, user=user, ip=ip)
        return pages.render(e.kind)
    except Exception as e:
        _log("temporary", user=user, ip=ip)
        logger.error("event=error type=%s", type(e).__name__)
        return pages.render("temporary")
    _log("ok", user=user, ip=ip, id=grant_id)
    return pages.render("ok", ip)


def lambda_handler(event, context):
    route = event.get("routeKey")
    if route not in ("GET /login", "GET /callback"):
        return pages.not_found()
    try:
        s = settings.load()
        return _login(s) if route == "GET /login" else _callback(event, s)
    except Exception as e:
        logger.error("event=error type=%s", type(e).__name__)
        return pages.render("temporary")
