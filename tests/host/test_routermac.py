import json
from types import SimpleNamespace

import pytest

import routermac
from common import ValidationError
from routermac import default_gateway, neigh_mac, set_router_mac

MAC = "aa:bb:cc:dd:ee:ff"


def _cfg(tmp_path, mac="<router-mac>"):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"region": "日本", "trusted_sources": [
        {"iface": "enp3s0", "cidr": "192.168.33.0/24", "exclude_ether_saddr": mac},
        {"iface": "wg0", "cidr": "10.0.0.0/24"}]}))
    return p


def test_neigh_parsing():
    assert neigh_mac("192.168.33.1 dev enp3s0 lladdr aa:bb:cc:dd:ee:ff REACHABLE") == MAC
    assert neigh_mac("192.168.33.1 dev enp3s0 lladdr AA:BB:CC:DD:EE:FF STALE") == MAC
    assert neigh_mac("192.168.33.1 dev enp3s0 INCOMPLETE") is None
    assert neigh_mac("192.168.33.1 dev enp3s0 lladdr aa:bb:cc:dd:ee:ff FAILED") is None
    assert neigh_mac("") is None


def test_default_gateway():
    assert default_gateway("default via 192.168.33.1 dev enp3s0 proto dhcp metric 100\n") == "192.168.33.1"
    with pytest.raises(ValidationError):
        default_gateway("")


def test_set_router_mac_does_not_overwrite(tmp_path):
    p = _cfg(tmp_path)
    assert set_router_mac(p, MAC, overwrite=False) is True
    obj = json.loads(p.read_text())
    assert obj["trusted_sources"][0]["exclude_ether_saddr"] == MAC
    assert "exclude_ether_saddr" not in obj["trusted_sources"][1]
    assert obj["region"] == "日本" and "日本" in p.read_text()  # ensure_ascii=False
    assert p.read_text().endswith("\n") and oct(p.stat().st_mode & 0o777) == "0o644"
    assert set_router_mac(p, "11:22:33:44:55:66", overwrite=False) is False
    assert json.loads(p.read_text())["trusted_sources"][0]["exclude_ether_saddr"] == MAC
    assert set_router_mac(p, "11:22:33:44:55:66", overwrite=True) is True
    assert json.loads(p.read_text())["trusted_sources"][0]["exclude_ether_saddr"] == "11:22:33:44:55:66"


def test_set_router_mac_errors(tmp_path):
    p = _cfg(tmp_path)
    with pytest.raises(ValidationError):
        set_router_mac(p, "bad", overwrite=True)
    q = tmp_path / "q.json"
    q.write_text(json.dumps({"trusted_sources": [{"iface": "wg0", "cidr": "10.0.0.0/24"}]}))
    with pytest.raises(ValidationError):
        set_router_mac(q, MAC, overwrite=True)


def _runner(table, calls):
    def run(cmd):
        calls.append(cmd)
        key = tuple(cmd[:2])
        out = table.get(key, "")
        if isinstance(out, list):
            out = out.pop(0)
        return SimpleNamespace(returncode=0, stdout=out, stderr="")
    return run


def test_main_already_set_runs_nothing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(routermac, "RUN", _runner({}, calls))
    assert routermac.main(["--config", str(_cfg(tmp_path, MAC))]) == 0
    assert calls == []


def test_main_ping_fallback_and_confirm(tmp_path, monkeypatch):
    p = _cfg(tmp_path)
    calls = []
    table = {("ip", "route"): "default via 192.168.33.1 dev enp3s0\n",
             ("ip", "neigh"): ["192.168.33.1 dev enp3s0 INCOMPLETE\n",
                               "192.168.33.1 dev enp3s0 lladdr aa:bb:cc:dd:ee:ff REACHABLE\n"]}
    monkeypatch.setattr(routermac, "RUN", _runner(table, calls))
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert routermac.main(["--config", str(p)]) == 1
    assert "<router-mac>" in p.read_text()
    table[("ip", "neigh")] = ["x INCOMPLETE", "192.168.33.1 dev e lladdr aa:bb:cc:dd:ee:ff STALE"]
    calls.clear()
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    assert routermac.main(["--config", str(p)]) == 0
    assert ["ping", "-c", "1", "-W", "2", "192.168.33.1"] in calls
    assert json.loads(p.read_text())["trusted_sources"][0]["exclude_ether_saddr"] == MAC


def test_main_no_mac_writes_nothing(tmp_path, monkeypatch):
    p = _cfg(tmp_path)
    before = p.read_text()
    table = {("ip", "route"): "default via 192.168.33.1 dev enp3s0\n", ("ip", "neigh"): "x INCOMPLETE"}
    monkeypatch.setattr(routermac, "RUN", _runner(table, []))
    assert routermac.main(["--yes", "--config", str(p)]) == 1
    assert p.read_text() == before


def test_main_refresh_overwrites(tmp_path, monkeypatch):
    p = _cfg(tmp_path, "11:22:33:44:55:66")
    table = {("ip", "route"): "default via 192.168.33.1 dev e\n",
             ("ip", "neigh"): "192.168.33.1 dev e lladdr aa:bb:cc:dd:ee:ff REACHABLE"}
    monkeypatch.setattr(routermac, "RUN", _runner(table, []))
    assert routermac.main(["--yes", "--refresh", "--config", str(p)]) == 0
    assert json.loads(p.read_text())["trusted_sources"][0]["exclude_ether_saddr"] == MAC


def test_import_check(capsys):
    assert routermac.main(["--import-check"]) == 0


def test_set_router_mac_no_write_when_unchanged(tmp_path):
    p = tmp_path / "c.json"
    p.write_bytes(json.dumps({"trusted_sources": [
        {"iface": "enp3s0", "cidr": "192.168.33.0/24", "exclude_ether_saddr": MAC}]},
        separators=(",", ":")).encode())
    p.chmod(0o600)
    before = p.read_bytes()
    assert set_router_mac(p, "11:22:33:44:55:66", overwrite=False) is False
    assert p.read_bytes() == before
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_set_router_mac_two_targets_one_unset(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"trusted_sources": [
        {"iface": "a0", "cidr": "192.168.1.0/24", "exclude_ether_saddr": "11:22:33:44:55:66"},
        {"iface": "b0", "cidr": "192.168.2.0/24", "exclude_ether_saddr": None},
        {"iface": "wg0", "cidr": "10.0.0.0/24"}]}))
    assert set_router_mac(p, MAC, overwrite=False) is True
    s = json.loads(p.read_text())["trusted_sources"]
    assert [x.get("exclude_ether_saddr") for x in s] == ["11:22:33:44:55:66", MAC, None][:2] + [None]
    assert "exclude_ether_saddr" not in s[2]
