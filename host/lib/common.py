"""CraftLatch 共通モジュール: 設定の読み込み、値の検証、要求ファイルの読み書き。"""
import ipaddress
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

REQUESTS_DIR = Path("/run/craftlatch/requests")
STATE_DIR = Path("/run/craftlatch/state")
QUARANTINE_DIR = Path("/run/craftlatch/quarantine")

ALLOW_MAX_TTL = 600
STOP_MAX_TTL = 120
MAX_FUTURE_IAT = 60

_USER_RE = re.compile(r"[A-Za-z0-9._@+-]{1,128}")
_INVOCATION_RE = re.compile(r"[0-9a-f]{32}")
_IFACE_RE = re.compile(r"[A-Za-z0-9_.-]{1,15}")
_MAC_RE = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")
_REGION_RE = re.compile(r"[a-z]{2}(-[a-z]+)+-[0-9]")
_QUEUE_URL_RE = re.compile(r"https://sqs\.[a-z0-9-]+\.amazonaws\.com/[0-9]{12}/[A-Za-z0-9_-]{1,80}")
_STOP_REASONS = ("idle", "probe-failure")


class ValidationError(ValueError):
    """入力が規則に合わない。"""


@dataclass(frozen=True)
class TrustedSource:
    iface: str
    cidr: str
    exclude_ether_saddr: str | None


@dataclass(frozen=True)
class Config:
    region: str
    queue_url: str
    java_port: int
    bedrock_port: int
    grant_timeout_sec: int
    idle_stop_sec: int
    slp_timeout_sec: int
    key_warn_days: int
    trusted_sources: tuple[TrustedSource, ...]


@dataclass(frozen=True)
class AllowGrant:
    ip: str
    iat: int
    exp: int
    id: str
    user: str


@dataclass(frozen=True)
class StopRequest:
    reason: str
    invocation: str
    iat: int
    exp: int


# ---- 基本の検証 ----

def _is_int(x) -> bool:
    return type(x) is int


def _match(pattern: re.Pattern, value, what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValidationError(f"{what} が不正です")
    return value


def validate_iface(s) -> str:
    return _match(_IFACE_RE, s, "iface")


def validate_mac(s) -> str:
    return _match(_MAC_RE, s, "MAC アドレス")


def validate_cidr(s) -> str:
    if not isinstance(s, str):
        raise ValidationError("cidr が不正です")
    try:
        net = ipaddress.IPv4Network(s, strict=True)
    except ValueError as e:
        raise ValidationError("cidr が不正です") from e
    if str(net) != s:
        raise ValidationError("cidr が正規形ではありません")
    return s


def _validate_ip(ip) -> str:
    if not isinstance(ip, str):
        raise ValidationError("ip が不正です")
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError as e:
        raise ValidationError("ip が不正です") from e
    if not addr.is_global or str(addr) != ip:
        raise ValidationError("ip がグローバルな正規形の IPv4 ではありません")
    return ip


def _validate_uuid(s) -> str:
    if not isinstance(s, str):
        raise ValidationError("id が不正です")
    try:
        ok = str(uuid.UUID(s)) == s
    except ValueError as e:
        raise ValidationError("id が不正です") from e
    if not ok:
        raise ValidationError("id が正規形ではありません")
    return s


def _validate_times(obj: dict, now: int, max_ttl: int) -> tuple[int, int]:
    iat, exp = obj["iat"], obj["exp"]
    if not (_is_int(iat) and _is_int(exp)):
        raise ValidationError("iat・exp は整数でなければなりません")
    if not (0 < iat < exp):
        raise ValidationError("iat・exp の大小が不正です")
    if iat > now + MAX_FUTURE_IAT:
        raise ValidationError("iat が未来すぎます")
    if not now < exp:
        raise ValidationError("期限切れです")
    if exp - iat > max_ttl:
        raise ValidationError("有効期間が上限を超えています")
    return iat, exp


def _check_keys(obj: dict, keys: set) -> None:
    if set(obj) != keys:
        raise ValidationError("キーが過不足です")


def parse_json_strict(data: bytes, max_bytes: int = 1024) -> dict:
    if len(data) > max_bytes:
        raise ValidationError("サイズが上限を超えています")

    def reject(name):
        raise ValidationError(f"数値定数 {name} は使えません")

    try:
        obj = json.loads(data.decode("utf-8"), parse_constant=reject)
    except ValidationError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise ValidationError("JSON として読めません") from e
    if not isinstance(obj, dict):
        raise ValidationError("トップレベルはオブジェクトでなければなりません")
    return obj


# ---- 要求（allow / stop） ----

_ALLOW_KEYS = {"v", "ip", "iat", "exp", "id", "user"}
_STOP_KEYS = {"v", "reason", "invocation", "iat", "exp"}


def _validate_allow(obj: dict, kind_key: str, kind: str, now: int) -> AllowGrant:
    _check_keys(obj, _ALLOW_KEYS | {kind_key})
    if not (_is_int(obj["v"]) and obj["v"] == 1):
        raise ValidationError("v が不正です")
    if obj[kind_key] != kind:
        raise ValidationError(f"{kind_key} が不正です")
    iat, exp = _validate_times(obj, now, ALLOW_MAX_TTL)
    return AllowGrant(
        ip=_validate_ip(obj["ip"]),
        iat=iat,
        exp=exp,
        id=_validate_uuid(obj["id"]),
        user=_match(_USER_RE, obj["user"], "user"),
    )


def _validate_stop(obj: dict, now: int) -> StopRequest:
    _check_keys(obj, _STOP_KEYS | {"action"})
    if not (_is_int(obj["v"]) and obj["v"] == 1):
        raise ValidationError("v が不正です")
    if obj["action"] != "stop":
        raise ValidationError("action が不正です")
    iat, exp = _validate_times(obj, now, STOP_MAX_TTL)
    reason = obj["reason"]
    if reason not in _STOP_REASONS:
        raise ValidationError("reason が不正です")
    return StopRequest(reason, _match(_INVOCATION_RE, obj["invocation"], "invocation"), iat, exp)


def validate_sqs_message(obj: dict, now: int) -> AllowGrant:
    return _validate_allow(obj, "type", "allow", now)


def parse_request(data: bytes, now: int) -> AllowGrant | StopRequest:
    obj = parse_json_strict(data)
    action = obj.get("action")
    if action == "allow":
        return _validate_allow(obj, "action", "allow", now)
    if action == "stop":
        return _validate_stop(obj, now)
    raise ValidationError("action が不正です")


def allow_to_request(g: AllowGrant) -> dict:
    return {"v": 1, "action": "allow", "ip": g.ip, "iat": g.iat, "exp": g.exp, "id": g.id, "user": g.user}


def stop_to_request(s: StopRequest) -> dict:
    return {"v": 1, "action": "stop", "reason": s.reason, "invocation": s.invocation, "iat": s.iat, "exp": s.exp}


# ---- ファイル書き込み ----

def _write_atomic(path: Path, data: bytes, mode: int) -> None:
    tmp = path.parent / f".tmp-{uuid.uuid4().hex}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            os.fchmod(fd, mode)  # umask に依存させない
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: Path, text: str, mode: int) -> None:
    _write_atomic(Path(path), text.encode("utf-8"), mode)


def write_request(directory: Path, payload: dict, now: int) -> Path:
    path = Path(directory) / f"{now}-{uuid.uuid4()}.req"
    data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    _write_atomic(path, data, 0o644)
    return path


# ---- 設定 ----

_CONFIG_INT_KEYS = ("java_port", "bedrock_port", "grant_timeout_sec", "idle_stop_sec",
                    "slp_timeout_sec", "key_warn_days")
_CONFIG_KEYS = {"region", "queue_url", "trusted_sources", *_CONFIG_INT_KEYS}


def _positive_int(v, what: str, maximum: int | None = None) -> int:
    if not _is_int(v) or v <= 0 or (maximum is not None and v > maximum):
        raise ValidationError(f"{what} が不正です")
    return v


def _trusted_source(obj) -> TrustedSource:
    if not isinstance(obj, dict) or not {"iface", "cidr"} <= set(obj) <= {"iface", "cidr", "exclude_ether_saddr"}:
        raise ValidationError("trusted_sources の要素が不正です")
    mac = obj.get("exclude_ether_saddr")
    return TrustedSource(
        iface=validate_iface(obj["iface"]),
        cidr=validate_cidr(obj["cidr"]),
        exclude_ether_saddr=None if "exclude_ether_saddr" not in obj else validate_mac(mac),
    )


def load_config(path: Path = Path("/etc/craftlatch/config.json")) -> Config:
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise ValidationError(f"設定ファイルを読めません: {e}") from e
    obj = parse_json_strict(data, max_bytes=65536)
    _check_keys(obj, _CONFIG_KEYS)
    sources = obj["trusted_sources"]
    if not isinstance(sources, list) or not sources:
        raise ValidationError("trusted_sources は空でない配列でなければなりません")
    ints = {k: _positive_int(obj[k], k, 65535 if k.endswith("_port") else None) for k in _CONFIG_INT_KEYS}
    return Config(
        region=_match(_REGION_RE, obj["region"], "region"),
        queue_url=_match(_QUEUE_URL_RE, obj["queue_url"], "queue_url"),
        trusted_sources=tuple(_trusted_source(s) for s in sources),
        **ints,
    )
