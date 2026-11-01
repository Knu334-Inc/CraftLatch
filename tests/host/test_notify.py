import urllib.error

import pytest

import notify
from notify import build_payload, main, post_webhook, record_sent, should_send

NOW = 1793460000
UNIT = "craftlatch-poll.service"


def test_rate_limit(tmp_path):
    assert should_send(tmp_path, UNIT, NOW)
    record_sent(tmp_path, UNIT, NOW)
    assert not should_send(tmp_path, UNIT, NOW + 3599)
    assert should_send(tmp_path, UNIT, NOW + 3600)
    assert (tmp_path / f"{UNIT}.last").stat().st_mode & 0o777 == 0o600


def test_should_send_garbage_file(tmp_path):
    (tmp_path / f"{UNIT}.last").write_text("xyz")
    assert should_send(tmp_path, UNIT, NOW)


def test_payload_has_no_log_body():
    p = build_payload("host1", UNIT, NOW)
    assert p["content"] == (
        f"[CraftLatch] host1: {UNIT} が失敗しました "
        f"(2026-11-01T00:20:00+09:00)。確認: journalctl -u {UNIT} -n 50"
    )
    assert "journalctl -u" in p["content"]


def test_post_webhook_rejects_non_https():
    with pytest.raises(ValueError):
        post_webhook("http://example.com/x", {"content": "a"})


def _env(tmp_path, monkeypatch, url="https://discord.example/api/webhooks/x\n"):
    cred = tmp_path / "cred"
    state = tmp_path / "state"
    cred.mkdir()
    state.mkdir()
    (cred / "webhook").write_text(url)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(cred))
    monkeypatch.setenv("STATE_DIRECTORY", str(state))
    return state


def test_main_success_records(tmp_path, monkeypatch):
    state = _env(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(notify, "post_webhook", lambda url, payload, timeout=10.0: calls.append(url))
    assert main([UNIT]) == 0
    assert calls == ["https://discord.example/api/webhooks/x"]
    assert (state / f"{UNIT}.last").exists()
    # 2 回目はレート制限
    assert main([UNIT]) == 0
    assert len(calls) == 1


def test_send_failure_does_not_record_and_returns_1(tmp_path, monkeypatch, capsys):
    state = _env(tmp_path, monkeypatch)

    def boom(url, payload, timeout=10.0):
        raise urllib.error.HTTPError(url, 429, "Too Many", {}, None)

    monkeypatch.setattr(notify, "post_webhook", boom)
    assert main([UNIT]) == 1
    assert not (state / f"{UNIT}.last").exists()
    out = capsys.readouterr().out
    assert "discord.example" not in out


@pytest.mark.parametrize("unit", ["", "a b", "x/y", "a" * 129, "日本"])
def test_invalid_unit_name_returns_1(unit, tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(notify, "post_webhook", lambda *a, **k: pytest.fail("called"))
    assert main([unit]) == 1


def test_main_missing_env_or_args(tmp_path, monkeypatch):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.delenv("STATE_DIRECTORY", raising=False)
    assert main([UNIT]) == 1
    assert main([]) == 1
    _env(tmp_path, monkeypatch)
    (tmp_path / "cred" / "webhook").unlink()
    assert main([UNIT]) == 1


def test_import_check(capsys):
    assert main(["--import-check"]) == 0
    assert "import-ok" in capsys.readouterr().out
