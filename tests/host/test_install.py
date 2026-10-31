"""install.sh のテスト（CRAFTLATCH_ROOT によるテストモード）。"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

HOST = Path(__file__).resolve().parents[2] / "host"
INSTALL = HOST / "install.sh"


@pytest.fixture
def env(tmp_path):
    shim = tmp_path / "bin" / "python3"
    shim.parent.mkdir()
    log = tmp_path / "py.log"
    shim.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit "${{FAKE_RC:-0}}"\n')
    shim.chmod(0o755)
    e = dict(os.environ)
    e.update(CRAFTLATCH_ROOT=str(tmp_path / "root"), PYTHON3=str(shim))
    return e, tmp_path / "root", log


def run(env, *args):
    return subprocess.run(["bash", str(INSTALL), *args], env=env, capture_output=True,
                          text=True, timeout=60)


def test_first_install_places_files_and_prints_enable_commands(env):
    e, root, log = env
    r = run(e, "--yes", "--refresh-router-mac")
    assert r.returncode == 0, r.stderr
    lib = root / "usr/local/lib/craftlatch"
    assert stat.S_IMODE((lib / "fwcheck").stat().st_mode) == 0o755
    assert stat.S_IMODE((lib / "mc-stop").stat().st_mode) == 0o755
    assert stat.S_IMODE((lib / "poll.py").stat().st_mode) == 0o644
    assert (lib / "fwcheck-expected.json").is_file()
    assert (root / "etc/systemd/system/minecraft.service").is_file()
    assert (root / "etc/tmpfiles.d/craftlatch.conf").is_file()
    assert (root / "etc/craftlatch/config.json").is_file()
    assert "-E -s" in log.read_text() and "--yes" in log.read_text()
    assert "--refresh" in log.read_text()
    assert f"--config {root}/etc/craftlatch/config.json" in log.read_text()
    assert "systemctl enable --now craftlatch-firewall.service" in r.stdout
    assert "実行しない: systemctl daemon-reload" in r.stdout


def test_existing_config_and_secrets_untouched_and_guidance_on_update(env):
    e, root, _ = env
    assert run(e).returncode == 0
    etc = root / "etc/craftlatch"
    (etc / "config.json").write_text("{\"mine\": 1}")
    (etc / "aws-credentials").write_text("secret")
    (etc / "craftlatch.nft").write_text("x")  # 初回ではない状態にする
    unit = root / "etc/systemd/system"
    (unit / "craftlatch-poll.timer").write_text("old")
    (unit / "minecraft.service").write_text("old")
    (root / "usr/local/lib/craftlatch/poll.py").write_text("old")
    r = run(e)
    assert r.returncode == 0, r.stderr
    assert (etc / "config.json").read_text() == "{\"mine\": 1}"
    assert (etc / "aws-credentials").read_text() == "secret"
    assert "操作不要（次回の実行から反映）" in r.stdout
    assert "sudo systemctl restart craftlatch-poll.timer" in r.stdout
    assert "restart minecraft" not in r.stdout
    assert "minecraft.service / minecraft.socket の変更: 次回の起動から反映" in r.stdout


def test_no_change_on_second_run(env):
    e, root, _ = env
    run(e)
    (root / "etc/craftlatch/craftlatch.nft").write_text("x")
    r = run(e)
    assert "変更はありません" in r.stdout


def _prepare_installed(env):
    e, root, _ = env
    assert run(e).returncode == 0
    (root / "etc/craftlatch/craftlatch.nft").write_text("x")  # 初回ではない状態にする
    return e, root


def test_guidance_mapping_per_unit(env):
    e, root = _prepare_installed(env)
    unit = root / "etc/systemd/system"
    for name in ("craftlatch-poll.service", "craftlatch-poll.timer", "craftlatch-apply.service",
                 "craftlatch-apply.path", "craftlatch-idle.service", "craftlatch-idle.timer",
                 "craftlatch-notify@.service"):
        (unit / name).write_text("old")
    r = run(e)
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("sudo systemctl restart craftlatch-poll.timer") == 1
    assert r.stdout.count("sudo systemctl restart craftlatch-apply.path") == 1
    assert "restart craftlatch-idle" not in r.stdout
    assert "restart craftlatch-poll.service" not in r.stdout
    assert "craftlatch-idle の変更: 次回の Minecraft 起動から反映" in r.stdout
    assert "操作不要（次回の通知から反映）" in r.stdout
    assert "craftlatch-firewall" not in r.stdout


def test_firewall_unit_change_prints_restart_with_warning(env):
    e, root = _prepare_installed(env)
    (root / "etc/systemd/system/craftlatch-firewall.service").write_text("old")
    r = run(e)
    assert r.stdout.count("sudo systemctl restart craftlatch-firewall.service") == 1
    assert "許可リストが空" in r.stdout


def test_guidance_survives_failure_after_placement(env):
    e, root = _prepare_installed(env)
    (root / "etc/systemd/system/craftlatch-apply.service").write_text("old")
    (root / "usr/local/lib/craftlatch/poll.py").write_text("old")
    pending = root / "etc/craftlatch/.install-pending"
    r = run(e | {"FAKE_RC": "1"})  # 配置後の routermac で失敗する
    assert r.returncode == 1
    assert pending.is_file() and stat.S_IMODE(pending.stat().st_mode) == 0o600
    r = run(e)
    assert r.returncode == 0, r.stderr
    assert "sudo systemctl restart craftlatch-apply.path" in r.stdout
    assert "操作不要（次回の実行から反映）" in r.stdout
    assert "変更はありません" not in r.stdout
    assert not pending.exists()
    assert "変更はありません" in run(e).stdout


def test_routermac_failure_exits_1(env):
    e, _, _ = env
    e["FAKE_RC"] = "1"
    r = run(e)
    assert r.returncode == 1
    assert "MAC" in r.stderr


def test_unknown_option_exits_2(env):
    e, _, _ = env
    r = run(e, "--bogus")
    assert r.returncode == 2
    assert "使い方" in r.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root では事前検証が通りうる")
def test_preflight_fails_without_changes_when_not_root(tmp_path):
    e = dict(os.environ)
    e.pop("CRAFTLATCH_ROOT", None)
    r = run(e)
    assert r.returncode == 1
    assert "root で実行" in r.stderr
    assert "何も変更していません" in r.stderr
