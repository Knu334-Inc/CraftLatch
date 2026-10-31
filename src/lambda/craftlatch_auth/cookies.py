"""ログイン取引用の Cookie（署名なし。設計書 4.3）。"""
import base64
import binascii
import json

NAME = "__Host-craftlatch"
MAX_AGE = 600
_KEYS = {"s", "n", "v", "t"}


def encode(state: str, nonce: str, verifier: str, t: int) -> str:
    raw = json.dumps({"s": state, "n": nonce, "v": verifier, "t": t}, separators=(",", ":"))
    value = base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()
    return f"{NAME}={value}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age={MAX_AGE}"


def decode(values: list[str]) -> dict | None:
    found = None
    for entry in values or []:
        if isinstance(entry, str) and entry.startswith(NAME + "="):
            found = entry[len(NAME) + 1:]
    if found is None:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(found + "=" * (-len(found) % 4)))
    except (ValueError, binascii.Error):
        return None
    if not isinstance(data, dict) or set(data) != _KEYS:
        return None
    if type(data["t"]) is not int:
        return None
    # token_urlsafe の出力は常に ASCII。孤立サロゲート等は拒否する
    if not all(isinstance(data[k], str) and data[k] and data[k].isascii() for k in "snv"):
        return None
    return data
