import json
import os
from datetime import date
from types import SimpleNamespace

import poll
from poll import PollResult, key_age_warning, make_sqs_client, run_poll, update_failures

NOW = 1793460000
U = "123e4567-e89b-42d3-a456-426614174000"


def body(**patch) -> str:
    m = {"v": 1, "type": "allow", "ip": "8.8.8.8", "iat": NOW, "exp": NOW + 600, "id": U, "user": "alice"}
    m.update(patch)
    return json.dumps(m)


def msg(n=0, b=None):
    return {"MessageId": f"m{n}", "ReceiptHandle": f"rh{n}", "Body": body() if b is None else b}


class FakeSQS:
    def __init__(self, batches, events=None):
        self.batches = list(batches)
        self.calls = []
        self.events = events if events is not None else []

    def receive_message(self, **kw):
        self.calls.append(("receive", kw))
        self.events.append("receive")
        msgs = self.batches.pop(0) if self.batches else []
        return {"Messages": msgs} if msgs else {}

    def delete_message(self, **kw):
        self.calls.append(("delete", kw))
        self.events.append("delete")
        return {}

    def of(self, name):
        return [kw for n, kw in self.calls if n == name]


class Mono:
    def __init__(self, *values):
        self.values = list(values)
        self.last = values[-1]

    def __call__(self):
        if self.values:
            self.last = self.values.pop(0)
        return self.last


def run(sqs, d, mono=None):
    return run_poll(sqs, "q", d, lambda: NOW, mono or (lambda: 0.0))


def test_valid_message_written_then_deleted(tmp_path):
    events = []
    sqs = FakeSQS([[msg()]], events)
    d = tmp_path / "req"
    d.mkdir()
    orig = poll.write_request

    def spy(*a, **k):
        events.append("write")
        return orig(*a, **k)

    poll.write_request = spy
    try:
        r = run(sqs, d)
    finally:
        poll.write_request = orig
    assert r == PollResult(received=1, written=1, deleted=1, invalid=0, failure=None)
    assert len(list(d.glob("*.req"))) == 1
    assert sqs.of("delete") == [{"QueueUrl": "q", "ReceiptHandle": "rh0"}]
    assert events == ["receive", "write", "delete", "receive"]


def test_invalid_message_deleted_without_file(tmp_path):
    sqs = FakeSQS([[msg(b=body(ip="10.0.0.5"))]])
    r = run(sqs, tmp_path)
    assert r.invalid == 1 and r.written == 0 and r.deleted == 1 and r.failure is None
    assert list(tmp_path.iterdir()) == []
    assert len(sqs.of("delete")) == 1


def test_oversized_body_is_invalid(tmp_path):
    sqs = FakeSQS([[msg(b=body(user="a") + " " * 1100)]])
    r = run(sqs, tmp_path)
    assert r.invalid == 1 and r.deleted == 1 and list(tmp_path.iterdir()) == []


def test_write_failure_keeps_message(tmp_path):
    sqs = FakeSQS([[msg()]])
    r = run(sqs, tmp_path / "missing-dir")
    assert sqs.of("delete") == []
    assert r.written == 0 and r.deleted == 0 and r.failure is None


def test_receive_loops_max_5_times(tmp_path):
    sqs = FakeSQS([[msg(i) for i in range(10)] for _ in range(10)])
    r = run(sqs, tmp_path)
    assert len(sqs.of("receive")) == 5
    assert r.received == 50


def test_receive_stops_on_empty(tmp_path):
    sqs = FakeSQS([[msg()], []])
    run(sqs, tmp_path)
    assert len(sqs.of("receive")) == 2


def test_deadline_stops_new_calls_and_counts_as_failure(tmp_path, monkeypatch):
    sqs = FakeSQS([[msg(i) for i in range(10)] for _ in range(10)])
    r = run(sqs, tmp_path, Mono(0.0, 10.0, 36.0))
    assert r.failure == "deadline"
    assert len(sqs.of("receive")) == 1
    assert sqs.of("delete") == []

    # main 経由で poll-failures が 1 になる
    state = tmp_path / "state"
    state.mkdir()
    req = tmp_path / "req"
    req.mkdir()
    sqs2 = FakeSQS([[msg(i) for i in range(10)] for _ in range(10)])
    mono = Mono(0.0, 10.0, 36.0)
    monkeypatch.setenv("STATE_DIRECTORY", str(state))
    monkeypatch.setattr(poll, "load_config", lambda: SimpleNamespace(region="ap-northeast-1", queue_url="q", key_warn_days=335))
    monkeypatch.setattr(poll, "make_sqs_client", lambda region: sqs2)
    monkeypatch.setattr(poll, "REQUESTS_DIR", req)
    monkeypatch.setattr(poll, "ROTATED_FILE", tmp_path / "none")
    monkeypatch.setattr(poll, "time", SimpleNamespace(monotonic=mono, time=lambda: NOW))
    assert poll.main([]) == 0
    assert (state / "poll-failures").read_text().strip() == "1"
    assert sqs2.of("delete") == []


def test_aws_error_sets_failure_and_stops(tmp_path):
    from botocore.exceptions import EndpointConnectionError

    class Bad(FakeSQS):
        def receive_message(self, **kw):
            self.calls.append(("receive", kw))
            raise EndpointConnectionError(endpoint_url="https://x")

    sqs = Bad([])
    r = run(sqs, tmp_path)
    assert r.failure == "EndpointConnectionError" and len(sqs.of("receive")) == 1


def test_receive_args(tmp_path):
    sqs = FakeSQS([])
    run(sqs, tmp_path)
    assert sqs.of("receive")[0] == {"QueueUrl": "q", "MaxNumberOfMessages": 10, "WaitTimeSeconds": 1}


def test_make_sqs_client_config():
    c = make_sqs_client("ap-northeast-1")
    cfg = c.meta.config
    assert cfg.connect_timeout == 3 and cfg.read_timeout == 5
    assert cfg.retries["total_max_attempts"] == 1


def test_failures_counter_and_exit_code(tmp_path):
    for i in range(1, 6):
        assert update_failures(tmp_path, "EndpointConnectionError") == i
    assert update_failures(tmp_path, None) == 0
    (tmp_path / "poll-failures").write_text("garbage")
    assert update_failures(tmp_path, "x") == 1


def f_with(tmp_path, text):
    p = tmp_path / "rotated"
    p.write_text(text)
    return p


def test_key_age_warning(tmp_path):
    f = f_with(tmp_path, "2025-11-01\n")
    assert key_age_warning(f, date(2026, 10, 1), 335) is None
    assert "335" in key_age_warning(f, date(2026, 10, 2), 335)


def test_key_age_warning_unknown_date(tmp_path):
    assert key_age_warning(tmp_path / "none", date(2026, 10, 1), 335)
    assert key_age_warning(f_with(tmp_path, "bogus"), date(2026, 10, 1), 335)


def test_main_import_check(capsys):
    assert poll.main(["--import-check"]) == 0
    assert capsys.readouterr().out.strip() == "import-ok"


def test_main_bad_config_returns_1(monkeypatch):
    def bad():
        raise poll.ValidationError("x")

    monkeypatch.setattr(poll, "load_config", bad)
    assert poll.main([]) == 1


def test_main_exit_1_at_5_failures(tmp_path, monkeypatch):
    (tmp_path / "poll-failures").write_text("4")
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
    monkeypatch.setattr(poll, "load_config", lambda: SimpleNamespace(region="r", queue_url="q", key_warn_days=335))
    monkeypatch.setattr(poll, "ROTATED_FILE", tmp_path / "none")
    monkeypatch.setattr(poll, "make_sqs_client", lambda region: FakeSQS([]))
    monkeypatch.setattr(poll, "run_poll", lambda *a, **k: PollResult(0, 0, 0, 0, "ClientError"))
    assert poll.main([]) == 1
    assert (tmp_path / "poll-failures").read_text().strip() == "5"
