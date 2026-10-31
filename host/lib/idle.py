"""CraftLatch idle: SLP で人数を確認し、無人が続けば stop 要求を置く。"""
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from common import (
    REQUESTS_DIR,
    STATE_DIR,
    StopRequest,
    ValidationError,
    atomic_write_text,
    load_config,
    stop_to_request,
    write_request,
)

PROTOCOL_VERSION = 767
MAX_PLAYERS = 10000
STOP_TTL = 120
_INVOCATION_RE = re.compile(r"[0-9a-f]{32}")


def _log(line: str) -> None:
    # 非 ASCII・surrogate でも例外を出さない
    print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


class SlpError(Exception):
    pass


# ---- SLP ----

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _recv_exact(sock: socket.socket, n: int, deadline: float) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SlpError("timeout")
        sock.settimeout(remaining)
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise SlpError("truncated")
        buf += chunk
    return bytes(buf)


def _read_varint_sock(sock: socket.socket, deadline: float) -> int:
    value = 0
    for i in range(5):
        b = _recv_exact(sock, 1, deadline)[0]
        value |= (b & 0x7F) << (7 * i)
        if not b & 0x80:
            return value
    raise SlpError("varint too long")


def _read_varint_buf(buf: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for i in range(5):
        if pos >= len(buf):
            raise SlpError("truncated varint")
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << (7 * i)
        if not b & 0x80:
            return value, pos
    raise SlpError("varint too long")


def _reject_constant(name: str):
    raise ValueError("invalid constant")


def slp_player_count(host: str, port: int, timeout: float, max_bytes: int = 65536) -> int:
    try:
        return _slp(host, port, timeout, max_bytes)
    except SlpError:
        raise
    except Exception as e:  # OSError・socket.timeout・デコード・JSON など全て
        raise SlpError(type(e).__name__) from e


def _slp(host: str, port: int, timeout: float, max_bytes: int) -> int:
    deadline = time.monotonic() + timeout
    host_b = host.encode("utf-8")
    handshake = b"\x00" + _varint(PROTOCOL_VERSION) + _varint(len(host_b)) + host_b \
        + struct.pack(">H", port) + _varint(1)
    request = _varint(len(handshake)) + handshake + _varint(1) + b"\x00"
    sock = socket.create_connection((host, port), timeout=max(deadline - time.monotonic(), 0.001))
    try:
        sock.settimeout(max(deadline - time.monotonic(), 0.001))
        sock.sendall(request)
        length = _read_varint_sock(sock, deadline)
        if length <= 0 or length > max_bytes:
            raise SlpError("bad length")
        packet = _recv_exact(sock, length, deadline)
    finally:
        sock.close()
    if packet[0] != 0x00:
        raise SlpError("bad packet id")
    slen, pos = _read_varint_buf(packet, 1)
    if slen > len(packet) - pos:
        raise SlpError("bad string length")
    text = packet[pos:pos + slen].decode("utf-8", errors="strict")
    obj = json.loads(text, parse_constant=_reject_constant)
    online = obj["players"]["online"]
    if type(online) is not int or not 0 <= online <= MAX_PLAYERS:
        raise SlpError("bad online")
    return online


# ---- 判定 ----

@dataclass
class IdleState:
    invocation: str
    empty_since: int | None
    probe_ok_since_empty: bool
    last_stop_request: int | None


def decide(state: IdleState, now: int, players: int | None, invocation: str, last_allow: int,
           idle_stop_sec: int = 900, rerequest_sec: int = 300) -> tuple[IdleState, StopRequest | None]:
    if state.invocation != invocation:
        state = IdleState(invocation, None, False, None)
    if players is not None and players >= 1:
        return IdleState(invocation, None, False, None), None
    empty_since = state.empty_since if state.empty_since is not None else now
    probe_ok = state.probe_ok_since_empty or players == 0
    last_req = state.last_stop_request
    req = None
    since = max(empty_since, last_allow)
    if now - since >= idle_stop_sec and (last_req is None or now - last_req >= rerequest_sec):
        req = StopRequest("idle" if probe_ok else "probe-failure", invocation, now, now + STOP_TTL)
        last_req = now
    return IdleState(invocation, empty_since, probe_ok, last_req), req


# ---- main ----

def _now() -> int:
    return int(time.time())


def _systemctl_invocation() -> str:
    r = subprocess.run(
        ["systemctl", "show", "-p", "InvocationID", "--value", "minecraft.service"],
        capture_output=True, text=True, shell=False, timeout=10, check=False,
    )
    return r.stdout.strip()


def _read_last_allow() -> int:
    try:
        return int((Path(STATE_DIR) / "last-allow").read_text().strip())
    except (OSError, ValueError):
        return 0


def _load_state(path: Path, invocation: str) -> IdleState:
    fresh = IdleState(invocation, None, False, None)
    try:
        obj = json.loads(path.read_text())
        inv, es, ok, lr = (obj["invocation"], obj["empty_since"],
                           obj["probe_ok_since_empty"], obj["last_stop_request"])
        if not isinstance(inv, str) or type(ok) is not bool:
            return fresh
        for v in (es, lr):
            if v is not None and type(v) is not int:
                return fresh
        return IdleState(inv, es, ok, lr)
    except (OSError, ValueError, KeyError, TypeError):
        return fresh


def _save_state(path: Path, s: IdleState) -> None:
    text = json.dumps({
        "invocation": s.invocation, "empty_since": s.empty_since,
        "probe_ok_since_empty": s.probe_ok_since_empty, "last_stop_request": s.last_stop_request,
    }) + "\n"
    atomic_write_text(path, text, 0o600)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--import-check" in argv:
        print("import-ok")
        return 0
    try:
        cfg = load_config()
    except ValidationError as e:
        _log(f"event=idle result=error error=config detail={e}")
        return 1
    try:
        invocation = _systemctl_invocation()
    except (OSError, subprocess.SubprocessError) as e:
        _log(f"event=idle result=error error={type(e).__name__}")
        return 1
    if not _INVOCATION_RE.fullmatch(invocation):
        _log("event=idle result=skipped reason=service-not-running")
        return 0
    state_path = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/craftlatch")) / "idle.json"
    now = _now()
    state = _load_state(state_path, invocation)
    last_allow = _read_last_allow()
    try:
        players: int | None = slp_player_count("127.0.0.1", cfg.java_port, cfg.slp_timeout_sec)
    except SlpError as e:
        players = None
        _log(f"event=slp result=error error={type(e).__name__}")
    new_state, req = decide(state, now, players, invocation, last_allow, idle_stop_sec=cfg.idle_stop_sec)
    empty_for = "" if new_state.empty_since is None else now - new_state.empty_since
    result = "none"
    if req is not None:
        try:
            write_request(REQUESTS_DIR, stop_to_request(req), now)
        except OSError as e:
            _log(f"event=idle result=error error={type(e).__name__}")
            return 1
        result = f"stop-requested reason={req.reason}"
    _log(f"event=idle players={'unknown' if players is None else players} "
         f"empty_for={empty_for} result={result}")
    try:
        _save_state(state_path, new_state)
    except OSError as e:
        _log(f"event=idle result=error error={type(e).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
