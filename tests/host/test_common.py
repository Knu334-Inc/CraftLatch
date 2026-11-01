import json
from pathlib import Path

import pytest

from common import (
    AllowGrant,
    Config,
    StopRequest,
    ValidationError,
    allow_to_request,
    atomic_write_text,
    load_config,
    parse_json_strict,
    parse_request,
    stop_to_request,
    validate_cidr,
    validate_iface,
    validate_mac,
    validate_sqs_message,
    write_request,
)

now = 1793460000
U = "123e4567-e89b-42d3-a456-426614174000"
g = AllowGrant("8.8.8.8", now, now + 600, U, "alice")
EXAMPLE = Path(__file__).resolve().parents[2] / "host" / "config.json.example"

VALID_CONFIG = {
    "region": "ap-northeast-1",
    "queue_url": "https://sqs.ap-northeast-1.amazonaws.com/123456789012/craftlatch-grants",
    "java_port": 25565,
    "bedrock_port": 19132,
    "grant_timeout_sec": 600,
    "idle_stop_sec": 900,
    "slp_timeout_sec": 3,
    "key_warn_days": 335,
    "trusted_sources": [
        {"iface": "enp3s0", "cidr": "192.168.33.0/24", "exclude_ether_saddr": "aa:bb:cc:dd:ee:ff"},
        {"iface": "wg0", "cidr": "10.0.0.0/24"},
    ],
}


def sqs_msg(**patch):
    m = {"v": 1, "type": "allow", "ip": "8.8.8.8", "iat": now, "exp": now + 600, "id": U, "user": "alice"}
    m.update(patch)
    return m


def test_valid_sqs_message():
    assert validate_sqs_message(sqs_msg(), now) == AllowGrant("8.8.8.8", now, now + 600, U, "alice")


@pytest.mark.parametrize("patch", [
    {"v": True}, {"v": 2}, {"iat": True}, {"exp": now + 601}, {"exp": now},
    {"iat": now + 61}, {"ip": "10.0.0.5"}, {"ip": "::1"}, {"ip": "203.000.113.5"},
    {"id": U.upper()}, {"user": "アリス"}, {"user": "a" * 129}, {"extra": 1},
    {"type": "stop"}, {"user": "alice\n"}, {"ip": 1},
])
def test_invalid_sqs_message_rejected(patch):
    with pytest.raises(ValidationError):
        validate_sqs_message(sqs_msg(**patch), now)


def test_sqs_message_missing_key_rejected():
    m = sqs_msg()
    del m["user"]
    with pytest.raises(ValidationError):
        validate_sqs_message(m, now)


def test_parse_json_strict_rejects_nan_size_and_non_object():
    for data in (b'{"a": NaN}', b'[1]', b'{' + b' ' * 1024 + b'}', b'{"a": Infinity}', b'\xff', b'{'):
        with pytest.raises(ValidationError):
            parse_json_strict(data)
    assert parse_json_strict(b'{"a": 1}') == {"a": 1}


def test_stop_request_ttl_limit_120():
    ok = {"v": 1, "action": "stop", "reason": "idle", "invocation": "a" * 32, "iat": now, "exp": now + 120}
    assert isinstance(parse_request(json.dumps(ok).encode(), now), StopRequest)
    with pytest.raises(ValidationError):
        parse_request(json.dumps({**ok, "exp": now + 121}).encode(), now)
    with pytest.raises(ValidationError):
        parse_request(json.dumps({**ok, "reason": "manual"}).encode(), now)
    with pytest.raises(ValidationError):
        parse_request(json.dumps({**ok, "invocation": "A" * 32}).encode(), now)


def test_parse_request_allow_roundtrip_and_unknown_key():
    req = allow_to_request(g)
    assert parse_request(json.dumps(req).encode(), now) == g
    with pytest.raises(ValidationError):
        parse_request(json.dumps({**req, "extra": 1}).encode(), now)
    with pytest.raises(ValidationError):
        parse_request(json.dumps({**req, "action": "other"}).encode(), now)
    s = StopRequest("probe-failure", "b" * 32, now, now + 120)
    assert parse_request(json.dumps(stop_to_request(s)).encode(), now) == s


def test_write_request_is_atomic_and_0644(tmp_path):
    import os
    old = os.umask(0o077)
    try:
        p = write_request(tmp_path, allow_to_request(g), now)
    finally:
        os.umask(old)
    assert p.name.startswith(f"{now}-") and p.suffix == ".req"
    assert oct(p.stat().st_mode & 0o777) == "0o644"
    assert not list(tmp_path.glob(".tmp-*"))
    assert parse_request(p.read_bytes(), now) == g


def test_atomic_write_text(tmp_path):
    p = tmp_path / "f"
    atomic_write_text(p, "x\n", 0o644)
    assert p.read_text() == "x\n" and oct(p.stat().st_mode & 0o777) == "0o644"
    assert not list(tmp_path.glob(".tmp-*"))


def test_load_config_rejects_missing_and_invalid(tmp_path):
    with pytest.raises(ValidationError):
        load_config(tmp_path / "none.json")
    (tmp_path / "c.json").write_text('{"region": "ap-northeast-1"}')
    with pytest.raises(ValidationError):
        load_config(tmp_path / "c.json")
    with pytest.raises(ValidationError):
        load_config(EXAMPLE)


def test_load_config_valid(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(VALID_CONFIG))
    c = load_config(p)
    assert isinstance(c, Config) and c.java_port == 25565
    assert c.trusted_sources[0].exclude_ether_saddr == "aa:bb:cc:dd:ee:ff"
    assert c.trusted_sources[1].exclude_ether_saddr is None


@pytest.mark.parametrize("mutate", [
    lambda c: c.update(extra=1),
    lambda c: c.update(java_port=True),
    lambda c: c.update(java_port=70000),
    lambda c: c.update(idle_stop_sec=0),
    lambda c: c.update(region="x"),
    lambda c: c.update(trusted_sources=[]),
    lambda c: c["trusted_sources"][1].update(cidr="10.0.0.5/24"),
    lambda c: c["trusted_sources"][1].update(extra=1),
    lambda c: c["trusted_sources"][1].pop("iface"),
])
def test_load_config_rejects_bad_values(tmp_path, mutate):
    import copy
    c = copy.deepcopy(VALID_CONFIG)
    mutate(c)
    p = tmp_path / "c.json"
    p.write_text(json.dumps(c))
    with pytest.raises(ValidationError):
        load_config(p)


def test_field_helpers():
    assert validate_iface("wg0") == "wg0"
    assert validate_cidr("10.0.0.0/24") == "10.0.0.0/24"
    assert validate_mac("aa:bb:cc:dd:ee:ff") == "aa:bb:cc:dd:ee:ff"
    for f, v in ((validate_iface, "a" * 16), (validate_iface, "a b"), (validate_cidr, "10.0.0.1/24"),
                 (validate_mac, "AA:BB:CC:DD:EE:FF"), (validate_mac, "<x>")):
        with pytest.raises(ValidationError):
            f(v)
