import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from apply import (
    StateStore,
    Systemd,
    apply_batch,
    build_nft_batch,
    collect,
    read_request_file,
)
from common import AllowGrant, StopRequest, ValidationError, allow_to_request, stop_to_request
from poll import run_poll

NOW = 1793460000
UID = os.getuid()
INV = "a" * 32
U1 = "123e4567-e89b-42d3-a456-426614174000"
U2 = "223e4567-e89b-42d3-a456-426614174000"


class Runner:
    """呼び出しを記録する偽の runner。"""

    def __init__(self, active="active", invocation=INV, fail=(), raise_on=None):
        self.calls = []
        self.active = active
        self.invocation = invocation
        self.fail = set(fail)  # "nft" / "start" / "stop"
        self.raise_on = raise_on  # "nft" / "start"

    def __call__(self, cmd, stdin=None):
        self.calls.append((list(cmd), stdin))
        if cmd[0] == "nft":
            kind = "nft"
        elif cmd[:2] == ["systemctl", "start"]:
            kind = "start"
        elif cmd[:2] == ["systemctl", "stop"]:
            kind = "stop"
        elif "InvocationID" in cmd:
            return subprocess.CompletedProcess(cmd, 0, self.invocation + "\n", "")
        elif "ActiveState" in cmd:
            return subprocess.CompletedProcess(cmd, 0, self.active + "\n", "")
        else:
            raise AssertionError(cmd)
        if self.raise_on == kind:
            self.raise_on = None
            raise RuntimeError("crash")
        return subprocess.CompletedProcess(cmd, 1 if kind in self.fail else 0, "", "")

    def kinds(self):
        out = []
        for cmd, _ in self.calls:
            if cmd[0] == "nft":
                out.append("nft")
            elif cmd[:2] == ["systemctl", "start"]:
                out.append("start")
            elif cmd[:2] == ["systemctl", "stop"]:
                out.append("stop")
        return out


def allow(id=U1, ip="8.8.8.8", iat=NOW, exp=NOW + 600):
    return AllowGrant(ip, iat, exp, id, "alice")


def stop(invocation=INV, iat=NOW, exp=NOW + 120):
    return StopRequest("idle", invocation, iat, exp)


def setup(tmp_path):
    req, q, st = tmp_path / "req", tmp_path / "q", tmp_path / "state"
    for d in (req, q, st):
        d.mkdir()
    return req, q, st


def put(req, name, payload):
    p = req / name
    p.write_text(json.dumps(payload))
    return p


def run_apply(allows, stops, store, runner, now=NOW):
    return apply_batch(allows, stops, store, Systemd(runner), runner, now)


def test_fifo_does_not_block(tmp_path):
    os.mkfifo(tmp_path / "x.req")
    with pytest.raises(ValidationError):
        read_request_file(tmp_path / "x.req", UID)


def test_symlink_and_oversize_and_wrong_owner_quarantined(tmp_path):
    req, q, _ = setup(tmp_path)
    good = tmp_path / "good"
    good.write_text(json.dumps(allow_to_request(allow())))
    os.symlink(good, req / "1-link.req")
    (req / "2-big.req").write_text("x" * 2000)
    put(req, "3-owner.req", allow_to_request(allow()))
    allows, stops, failed = collect(req, q, UID + 1, NOW)
    assert allows == [] and stops == [] and failed is False
    assert sorted(p.name for p in q.iterdir()) == ["1-link.req", "2-big.req", "3-owner.req"]
    assert list(req.iterdir()) == []


def test_expired_file_is_quarantined(tmp_path):
    req, q, _ = setup(tmp_path)
    put(req, "1-a.req", allow_to_request(allow(exp=NOW + 1)))
    allows, _, _ = collect(req, q, UID, NOW + 5)
    assert allows == [] and len(list(q.iterdir())) == 1


def test_collect_removes_stale_tmp_files(tmp_path):
    req, q, _ = setup(tmp_path)
    old, fresh = req / ".tmp-a", req / ".tmp-b"
    old.write_text("x")
    fresh.write_text("x")
    os.utime(old, (NOW - 61, NOW - 61))
    os.utime(fresh, (NOW - 10, NOW - 10))
    (req / "note.txt").write_text("x")
    collect(req, q, UID, NOW)
    assert not old.exists() and fresh.exists() and (req / "note.txt").exists()


def test_bad_file_does_not_stop_others(tmp_path):
    req, q, _ = setup(tmp_path)
    (req / "1-bad.req").write_text("not json")
    p = put(req, "2-ok.req", allow_to_request(allow()))
    s = put(req, "3-stop.req", stop_to_request(stop()))
    allows, stops, failed = collect(req, q, UID, NOW)
    assert [x[0] for x in allows] == [p] and allows[0][1] == allow()
    assert [x[0] for x in stops] == [s] and stops[0][1] == stop()
    assert failed is False
    assert (q / "1-bad.req").exists()


def test_collect_limits_to_100_and_trims_quarantine(tmp_path):
    req, q, _ = setup(tmp_path)
    for i in range(105):
        (req / f"{i:04d}-x.req").write_text("bad")
    collect(req, q, UID, NOW)
    assert len(list(req.iterdir())) == 5
    assert len(list(q.iterdir())) == 100
    for i in range(5):
        (req / f"{i:04d}-y.req").write_text("bad")
    # 既存 5 件 + 新 5 件 = 計 10 件を隔離 → 隔離先は 100 件上限
    collect(req, q, UID, NOW)
    assert len(list(q.iterdir())) == 100


def test_quarantine_name_collision(tmp_path):
    req, q, _ = setup(tmp_path)
    (q / "1-a.req").write_text("old")
    (req / "1-a.req").write_text("bad")
    collect(req, q, UID, NOW)
    assert len(list(q.iterdir())) == 2


def test_build_nft_batch():
    assert build_nft_batch(["8.8.8.8"]) == (
        "add element inet craftlatch allow4 { 8.8.8.8 }\n"
        "delete element inet craftlatch allow4 { 8.8.8.8 }\n"
        "add element inet craftlatch allow4 { 8.8.8.8 timeout 10m }\n")


def test_state_store_robust(tmp_path):
    st = StateStore(tmp_path)
    assert st.last_allow() == 0 and st.seen() == {}
    (tmp_path / "last-allow").write_text("junk")
    (tmp_path / "seen.json").write_text("[1")
    assert st.last_allow() == 0 and st.seen() == {}
    st.set_last_allow(NOW)
    assert st.last_allow() == NOW
    st.mark_seen({"a": NOW + 10, "b": NOW - 1}, NOW)
    st.mark_seen({"c": NOW + 20}, NOW)
    assert st.seen() == {"a": NOW + 10, "c": NOW + 20}
    assert oct((tmp_path / "seen.json").stat().st_mode & 0o777) == "0o644"


def test_allow_order_and_seen_recorded_last(tmp_path):
    req, q, st = setup(tmp_path)
    events = []
    store = StateStore(st)
    orig_set, orig_mark = store.set_last_allow, store.mark_seen
    store.set_last_allow = lambda n: (events.append("last-allow"), orig_set(n))[1]
    store.mark_seen = lambda d, n: (events.append("seen"), orig_mark(d, n))[1]
    r = Runner()

    def rec(cmd, stdin=None):
        if cmd[0] == "nft" or cmd[1] in ("start",):
            events.append(cmd[0] if cmd[0] == "nft" else "start")
        return r(cmd, stdin)

    p = put(req, "1-a.req", allow_to_request(allow()))
    allows, stops, _ = collect(req, q, UID, NOW)
    assert apply_batch(allows, stops, store, Systemd(rec), rec, NOW) is True
    assert events == ["nft", "last-allow", "start", "seen"]
    assert r.calls[0] == (["nft", "-f", "-"], build_nft_batch(["8.8.8.8"]))
    assert (["systemctl", "start", "--no-block", "minecraft.service"], None) in r.calls
    assert store.seen() == {U1: NOW + 600} and store.last_allow() == NOW
    assert not p.exists()


def test_duplicate_id_skipped(tmp_path):
    req, q, st = setup(tmp_path)
    store = StateStore(st)
    store.mark_seen({U1: NOW + 600}, NOW)
    p = put(req, "1-a.req", allow_to_request(allow()))
    allows, stops, _ = collect(req, q, UID, NOW)
    r = Runner()
    assert run_apply(allows, stops, store, r) is True
    assert r.kinds() == [] and not p.exists()


def test_same_ip_deduplicated_in_batch(tmp_path):
    st = tmp_path
    r = Runner()
    run_apply([(tmp_path / "x", allow(U1)), (tmp_path / "y", allow(U2))], [], StateStore(st), r)
    assert r.calls[0][1].count("timeout 10m") == 1


def test_allow_wins_over_stop_in_same_batch(tmp_path):
    r = Runner()
    store = StateStore(tmp_path)
    run_apply([(tmp_path / "x", allow())], [(tmp_path / "y", stop(iat=NOW + 1))], store, r)
    assert "stop" not in r.kinds() and "start" in r.kinds()


def test_duplicate_allow_still_discards_stop(tmp_path):
    r = Runner()
    store = StateStore(tmp_path)
    store.mark_seen({U1: NOW + 600}, NOW)
    run_apply([(tmp_path / "x", allow())], [(tmp_path / "y", stop())], store, r)
    assert r.kinds() == []


@pytest.mark.parametrize("case", ["expired", "invocation_mismatch", "iat_not_after_last_allow", "inactive"])
def test_stale_stop_discarded(tmp_path, case):
    store = StateStore(tmp_path)
    r = Runner()
    s = stop()
    if case == "expired":
        s = stop(exp=NOW)
    elif case == "invocation_mismatch":
        s = stop(invocation="b" * 32)
    elif case == "iat_not_after_last_allow":
        store.set_last_allow(NOW)
    elif case == "inactive":
        r = Runner(active="inactive")
    f = tmp_path / "s.req"
    f.write_text("x")
    assert run_apply([], [(f, s)], store, r) is True
    assert "stop" not in r.kinds() and not f.exists()


def test_valid_stop_calls_stop_no_block(tmp_path):
    store = StateStore(tmp_path)
    store.set_last_allow(NOW - 100)
    r = Runner()
    assert run_apply([], [(tmp_path / "a", stop()), (tmp_path / "b", stop())], store, r) is True
    assert r.kinds() == ["stop"]
    assert (["systemctl", "stop", "--no-block", "minecraft.service"], None) in r.calls


def test_stop_failure_returns_false(tmp_path):
    store = StateStore(tmp_path)
    r = Runner(fail={"stop"})
    assert run_apply([], [(tmp_path / "a", stop())], store, r) is False


def test_nft_failure_returns_false_no_start_no_seen(tmp_path):
    f = tmp_path / "a.req"
    f.write_text("x")
    store = StateStore(tmp_path)
    r = Runner(fail={"nft"})
    assert run_apply([(f, allow())], [], store, r) is False
    assert r.kinds() == ["nft"] and store.seen() == {} and not f.exists()


def test_partial_success_keeps_allow_returns_false(tmp_path):
    store = StateStore(tmp_path)
    r = Runner(fail={"start"})
    assert run_apply([(tmp_path / "a", allow())], [], store, r) is False
    assert r.kinds() == ["nft", "start"]  # nft の取り消しはしない
    assert store.seen() == {U1: NOW + 600}


def test_unlink_failure_returns_false(tmp_path):
    store = StateStore(tmp_path)
    # 存在しないファイルの削除は成功扱い、ディレクトリは失敗
    d = tmp_path / "dir"
    d.mkdir()
    (d / "f").write_text("x")
    assert run_apply([(d, allow())], [], store, Runner()) is False


@pytest.mark.parametrize("crash_at", ["before_nft", "after_nft", "after_start"])
def test_crash_recovery_is_at_least_once(tmp_path, crash_at):
    req, q, st = setup(tmp_path)
    p = put(req, "1-a.req", allow_to_request(allow()))
    store = StateStore(st)
    r1 = Runner(raise_on={"before_nft": "nft", "after_nft": "start"}.get(crash_at))
    if crash_at == "after_start":
        def boom(*a, **k):
            raise RuntimeError("crash")
        store.mark_seen = boom
    allows, stops, _ = collect(req, q, UID, NOW)
    with pytest.raises(RuntimeError):
        apply_batch(allows, stops, store, Systemd(r1), r1, NOW)
    assert p.exists()

    store2 = StateStore(st)
    r2 = Runner()
    allows, stops, _ = collect(req, q, UID, NOW)
    assert apply_batch(allows, stops, store2, Systemd(r2), r2, NOW) is True
    total = r1.kinds() + r2.kinds()
    expected = {
        "before_nft": ["nft", "nft", "start"],
        "after_nft": ["nft", "start", "nft", "start"],
        "after_start": ["nft", "start", "nft", "start"],
    }[crash_at]
    assert total == expected
    assert U1 in store2.seen() and not p.exists()


class FakeSQS:
    def __init__(self, bodies):
        self.batches = [[{"MessageId": "m", "ReceiptHandle": "rh", "Body": b} for b in bodies]]
        self.deleted = []

    def receive_message(self, **kw):
        msgs = self.batches.pop(0) if self.batches else []
        return {"Messages": msgs} if msgs else {}

    def delete_message(self, **kw):
        self.deleted.append(kw)
        return {}


def test_poll_to_apply_integration(tmp_path):
    req, q, st = setup(tmp_path)
    body = json.dumps({"v": 1, "type": "allow", "ip": "8.8.8.8", "iat": NOW, "exp": NOW + 600, "id": U1, "user": "alice"})
    sqs = FakeSQS([body])
    run_poll(sqs, "q", req, lambda: NOW, lambda: 0.0)
    assert len(list(req.glob("*.req"))) == 1
    allows, stops, failed = collect(req, q, UID, NOW)
    r = Runner()
    assert apply_batch(allows, stops, StateStore(st), Systemd(r), r, NOW) is True
    assert "8.8.8.8" in r.calls[0][1]
    assert "start" in r.kinds() and list(req.glob("*.req")) == []


def _strict_stdout(monkeypatch):
    import io
    import sys

    buf = io.BytesIO()
    w = io.TextIOWrapper(buf, encoding="utf-8", errors="strict")
    monkeypatch.setattr(sys, "stdout", w)
    return w, buf


def test_non_utf8_filename_does_not_crash_and_is_quarantined(tmp_path, monkeypatch):
    req, q, _ = setup(tmp_path)
    fd = os.open(os.fsencode(req) + b"/\xff.req", os.O_CREAT | os.O_WRONLY, 0o644)
    os.write(fd, b"bad")
    os.close(fd)
    ok = put(req, "2-ok.req", allow_to_request(allow()))
    w, buf = _strict_stdout(monkeypatch)
    allows, stops, failed = collect(req, q, UID, NOW)
    w.flush()
    assert [x[0] for x in allows] == [ok] and failed is False
    assert len(list(q.iterdir())) == 1 and [p.name for p in req.iterdir()] == ["2-ok.req"]
    assert b"\\udcff" in buf.getvalue()


def test_newline_in_filename_cannot_forge_log_lines(tmp_path, capsys):
    req, q, _ = setup(tmp_path)
    (req / "1-x\nevent=allow ip=1.1.1.1 nft=ok.req").write_text("bad")
    collect(req, q, UID, NOW)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and lines[0].startswith("event=invalid-request")
