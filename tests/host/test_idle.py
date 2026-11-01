import json
import socket
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

import idle
from common import StopRequest
from idle import IdleState, SlpError, decide, slp_player_count

now = 1_000_000
INV = "a" * 32


def varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def status_packet(obj_text: str) -> bytes:
    s = obj_text.encode()
    body = b"\x00" + varint(len(s)) + s
    return varint(len(body)) + body


def fake_server(payload: bytes | None, hold: float = 0.0):
    """payload を返して閉じる（None なら応答せず hold 秒保持）サーバー。"""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        try:
            conn.settimeout(2)
            try:
                conn.recv(4096)
            except OSError:
                pass
            if payload is None:
                time.sleep(hold)
            else:
                conn.sendall(payload)
        finally:
            conn.close()
            srv.close()

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


def test_slp_against_local_fake_server():
    port = fake_server(status_packet('{"players":{"online":2,"max":20}}'))
    assert slp_player_count("127.0.0.1", port, 2.0) == 2


@pytest.mark.parametrize("payload", [
    status_packet('{"players":{"online":2}}')[:-5],                       # 途中切断
    b"\xff\xff\xff\xff\xff\xff\x01" + b"\x00",                              # VarInt 6 バイト
    varint(65537) + b"\x00" + b"x" * 10,                                  # 長さ超過
    status_packet("not json"),                                            # JSON でない
    status_packet('{"players":{"online":true}}'),                         # bool
    status_packet('{"players":{"online":-1}}'),                           # 負数
    status_packet('{"players":{"online":NaN}}'),                          # NaN
    varint(0),                                                            # 長さ 0
    b"",                                                                  # 即切断
])
def test_slp_truncated_and_garbage_responses_raise_slperror(payload):
    port = fake_server(payload)
    with pytest.raises(SlpError):
        slp_player_count("127.0.0.1", port, 2.0)


def test_slp_connection_refused_raises():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(SlpError):
        slp_player_count("127.0.0.1", port, 1.0)


def test_slp_timeout_raises():
    port = fake_server(None, hold=3)
    t0 = time.monotonic()
    with pytest.raises(SlpError):
        slp_player_count("127.0.0.1", port, 0.5)
    assert time.monotonic() - t0 < 1.5


def test_reset_on_new_invocation():
    s, _ = decide(IdleState("old", 100, True, 200), now, 0, "new", 0)
    assert s.empty_since == now and s.invocation == "new" and s.last_stop_request is None


def test_stop_after_900s_with_reason_idle():
    _, req = decide(IdleState("i", now - 900, True, None), now, 0, "i", 0)
    assert req == StopRequest("idle", "i", now, now + 120)


def test_not_yet_899s():
    _, req = decide(IdleState("i", now - 899, True, None), now, 0, "i", 0)
    assert req is None


def test_probe_failure_reason_when_never_ok():
    s, req = decide(IdleState("i", now - 900, False, None), now, None, "i", 0)
    assert req == StopRequest("probe-failure", "i", now, now + 120)
    assert s.last_stop_request == now and s.probe_ok_since_empty is False


def test_last_allow_defers_stop():
    _, req = decide(IdleState("i", now - 1000, True, None), now, 0, "i", last_allow=now - 60)
    assert req is None


def test_rerequest_after_300s():
    _, a = decide(IdleState("i", now - 2000, True, now - 299), now, 0, "i", 0)
    assert a is None
    _, b = decide(IdleState("i", now - 2000, True, now - 300), now, 0, "i", 0)
    assert b is not None


def test_players_reset_all():
    s, req = decide(IdleState("i", now - 500, True, now - 10), now, 1, "i", 0)
    assert (s.empty_since, s.probe_ok_since_empty, s.last_stop_request) == (None, False, None)
    assert req is None


def test_empty_starts_and_zero_marks_probe_ok():
    s, _ = decide(IdleState("i", None, False, None), now, 0, "i", 0)
    assert (s.empty_since, s.probe_ok_since_empty) == (now, True)


# ---- main ----

def _setup_main(monkeypatch, tmp_path, players, invocation=INV, last_allow=None):
    req_dir = tmp_path / "req"
    req_dir.mkdir()
    st_dir = tmp_path / "run-state"
    st_dir.mkdir()
    var = tmp_path / "var"
    var.mkdir()
    if last_allow is not None:
        (st_dir / "last-allow").write_text(f"{last_allow}\n")
    monkeypatch.setattr(idle, "REQUESTS_DIR", req_dir)
    monkeypatch.setattr(idle, "STATE_DIR", st_dir)
    monkeypatch.setenv("STATE_DIRECTORY", str(var))
    monkeypatch.setattr(idle, "load_config", lambda: SimpleNamespace(
        java_port=25565, idle_stop_sec=900, slp_timeout_sec=3))
    monkeypatch.setattr(idle, "_now", lambda: now)
    monkeypatch.setattr(idle, "_systemctl_invocation", lambda: invocation)

    def fake_slp(host, port, timeout, max_bytes=65536):
        if players is None:
            raise SlpError("x")
        return players

    monkeypatch.setattr(idle, "slp_player_count", fake_slp)
    return req_dir, var


def test_main_writes_request_and_saves_state(monkeypatch, tmp_path):
    req_dir, var = _setup_main(monkeypatch, tmp_path, 0)
    (var / "idle.json").write_text(json.dumps({
        "invocation": INV, "empty_since": now - 1000,
        "probe_ok_since_empty": True, "last_stop_request": None}))
    assert idle.main([]) == 0
    files = list(req_dir.glob("*.req"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["reason"] == "idle"
    saved = json.loads((var / "idle.json").read_text())
    assert saved["last_stop_request"] == now and saved["invocation"] == INV


def test_main_no_state_starts_fresh_no_request(monkeypatch, tmp_path, capsys):
    req_dir, var = _setup_main(monkeypatch, tmp_path, None)
    assert idle.main([]) == 0
    assert list(req_dir.glob("*.req")) == []
    saved = json.loads((var / "idle.json").read_text())
    assert saved["empty_since"] == now and saved["probe_ok_since_empty"] is False
    assert "event=slp result=error" in capsys.readouterr().out


def test_main_corrupt_state_is_fresh(monkeypatch, tmp_path):
    _, var = _setup_main(monkeypatch, tmp_path, 0)
    (var / "idle.json").write_text("{garbage")
    assert idle.main([]) == 0
    assert json.loads((var / "idle.json").read_text())["empty_since"] == now


def test_main_last_allow_defers(monkeypatch, tmp_path):
    req_dir, var = _setup_main(monkeypatch, tmp_path, 0, last_allow=now - 10)
    (var / "idle.json").write_text(json.dumps({
        "invocation": INV, "empty_since": now - 1000,
        "probe_ok_since_empty": True, "last_stop_request": None}))
    assert idle.main([]) == 0
    assert list(req_dir.glob("*.req")) == []


def test_main_write_failure_returns_1_without_saving_request_time(monkeypatch, tmp_path):
    req_dir, var = _setup_main(monkeypatch, tmp_path, 0)
    (var / "idle.json").write_text(json.dumps({
        "invocation": INV, "empty_since": now - 1000,
        "probe_ok_since_empty": True, "last_stop_request": None}))

    def boom(*a, **k):
        raise OSError("ro")

    monkeypatch.setattr(idle, "write_request", boom)
    assert idle.main([]) == 1
    assert json.loads((var / "idle.json").read_text())["last_stop_request"] is None


def test_main_service_not_running_does_nothing(monkeypatch, tmp_path):
    req_dir, var = _setup_main(monkeypatch, tmp_path, 0, invocation="")
    assert idle.main([]) == 0
    assert not (var / "idle.json").exists()


def test_main_bad_config_returns_1(monkeypatch, tmp_path):
    _setup_main(monkeypatch, tmp_path, 0)
    from common import ValidationError

    def bad():
        raise ValidationError("x")

    monkeypatch.setattr(idle, "load_config", bad)
    assert idle.main([]) == 1


def test_import_check(capsys):
    assert idle.main(["--import-check"]) == 0
    assert "import-ok" in capsys.readouterr().out
