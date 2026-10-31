from .helpers import b64

from craftlatch_auth import cookies


def test_roundtrip():
    c = cookies.encode("s", "n", "v", 100)
    assert c.startswith("__Host-craftlatch=")
    assert c.endswith("; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=600")
    value = c.split(";")[0]
    assert "=" not in value.split("=", 1)[1]
    assert cookies.decode(["a=b", value]) == {"s": "s", "n": "n", "v": "v", "t": 100}


def test_last_wins_and_malformed():
    good = cookies.encode("s", "n", "v", 1).split(";")[0]
    assert cookies.decode([good, "__Host-craftlatch=%%%"]) is None
    assert cookies.decode(["__Host-craftlatch=%%%", good])["s"] == "s"
    assert cookies.decode([]) is None
    for raw in (b"nope", b"[]", b'{"s":"a","n":"b","v":"c"}',
                b'{"s":"a","n":"b","v":"c","t":true}', b'{"s":"a","n":"b","v":"c","t":1,"x":1}',
                b'{"s":1,"n":"b","v":"c","t":1}'):
        assert cookies.decode(["__Host-craftlatch=" + b64(raw)]) is None
