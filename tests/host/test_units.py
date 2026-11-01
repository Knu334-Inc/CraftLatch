"""systemd ユニット・tmpfiles の値の検査と、配置後と同じ構成での import 検査。

ユニットの構文自体は Task 15 の `systemd-analyze verify` で確認する。
ユニットは同じキーを複数回書く（Environment= など）ため、configparser では
最後の値しか残らない。そこで全テストで共通の読み取り関数を使い、
{セクション: {キー: [値, ...]}} の形（キーの大文字小文字は保持）で扱う。
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOST = Path(__file__).resolve().parents[2] / "host"
SYSTEMD = HOST / "systemd"
LIB = HOST / "lib"

UNIT_FILES = [
    "craftlatch-firewall.service", "craftlatch-poll.service", "craftlatch-poll.timer",
    "craftlatch-apply.path", "craftlatch-apply.service", "craftlatch-idle.service",
    "craftlatch-idle.timer", "craftlatch-notify@.service", "minecraft.socket",
    "minecraft.service",
]


def load(name):
    """ユニットファイルを {セクション: {キー: [値...]}} として返す。"""
    result = {}
    section = None
    for raw in (SYSTEMD / name).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = result.setdefault(line[1:-1], {})
            continue
        key, _, value = line.partition("=")
        section.setdefault(key, []).append(value)
    return result


def one(unit, section, key):
    values = load(unit)[section][key]
    assert len(values) == 1, (unit, key, values)
    return values[0]


def test_all_unit_files_exist_without_comment_header():
    for name in UNIT_FILES:
        text = (SYSTEMD / name).read_text(encoding="utf-8")
        assert text.startswith("[Unit]\n"), name


@pytest.mark.parametrize("unit,script", [
    ("craftlatch-poll.service", "poll.py"),
    ("craftlatch-apply.service", "apply.py"),
    ("craftlatch-idle.service", "idle.py"),
    ("craftlatch-notify@.service", "notify.py"),
])
def test_python_units_use_E_s_not_I(unit, script):
    start = one(unit, "Service", "ExecStart")
    assert start.startswith("/usr/bin/python3 -E -s /usr/local/lib/craftlatch/" + script)
    assert " -I" not in start


@pytest.mark.parametrize("unit", [
    "craftlatch-poll.service", "craftlatch-apply.service", "craftlatch-apply.path",
    "craftlatch-idle.service", "craftlatch-firewall.service", "minecraft.service",
])
def test_onfailure_on_required_units(unit):
    assert load(unit)["Unit"]["OnFailure"] == ["craftlatch-notify@%n.service"]


def test_minecraft_unit_values():
    u = load("minecraft.service")
    assert u["Service"]["ExecStartPre"] == ["+/usr/local/lib/craftlatch/fwcheck"]
    start = u["Service"]["ExecStart"][0]
    assert "-jar /media/ssd/paper/paper.jar" in start
    assert "-Xms4G" in start and "-Xmx4G" in start
    assert u["Service"]["MemoryHigh"] == ["5G"]
    assert u["Service"]["MemoryMax"] == ["5632M"]
    assert u["Service"]["CPUQuota"] == ["300%"]
    assert u["Service"]["Restart"] == ["no"]
    assert "Install" not in u
    assert u["Unit"]["RequiresMountsFor"] == ["/media/ssd/paper"]


def test_firewall_unit_has_no_execstop():
    s = load("craftlatch-firewall.service")["Service"]
    assert not any(k.startswith("ExecStop") for k in s)
    assert s["ExecStart"] == ["/usr/sbin/nft -f /etc/craftlatch/craftlatch.nft"]


def test_poll_timeouts():
    s = load("craftlatch-poll.service")["Service"]
    assert s["TimeoutStartSec"] == ["50s"]
    assert s["TimeoutStopSec"] == ["10s"]
    assert "Environment" in s and len(s["Environment"]) == 2


def test_apply_service_hardening():
    s = load("craftlatch-apply.service")["Service"]
    assert s["CapabilityBoundingSet"] == ["CAP_NET_ADMIN"]
    assert s["IPAddressDeny"] == ["any"]
    assert "PrivateNetwork" not in s


def test_tmpfiles_entries():
    text = (HOST / "tmpfiles.d" / "craftlatch.conf").read_text(encoding="utf-8")
    assert "d /run/craftlatch/requests 1770 root craftlatch -" in text
    assert "d /run/craftlatch/quarantine 0700 root root -" in text


@pytest.mark.parametrize("script", [
    "poll.py", "apply.py", "idle.py", "notify.py", "nftgen.py", "routermac.py", "fwcheck",
])
def test_scripts_import_common_under_E_s(script, tmp_path):
    lib = tmp_path / "lib"
    shutil.copytree(LIB, lib, ignore=shutil.ignore_patterns("__pycache__"))
    other = tmp_path / "elsewhere"
    other.mkdir()
    r = subprocess.run(
        [sys.executable, "-E", "-s", str(lib / script), "--import-check"],
        cwd=other, capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "import-ok"
