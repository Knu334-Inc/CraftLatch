import copy
import ipaddress
import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import fwcheck
from common import TrustedSource, ValidationError
from fwcheck import REQUIRED_RULE_SHAPES, check_required_shapes, expected_json, normalize

TEMPLATE = Path(__file__).resolve().parents[2] / "host" / "lib" / "fwcheck-expected.json"
NFT_CMD = ["nft", "-j", "list", "table", "inet", "craftlatch"]


def _contains_key(obj, keys):
    if isinstance(obj, dict):
        return any(k in keys or _contains_key(v, keys) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_contains_key(v, keys) for v in obj)
    return False


def _runner(payload, calls=None):
    def run(cmd):
        if calls is not None:
            calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    return run


@pytest.fixture
def env(monkeypatch, cfg):
    monkeypatch.setattr(fwcheck, "load_config", lambda: cfg)
    monkeypatch.setattr(fwcheck, "TEMPLATE_PATH", TEMPLATE)


def test_normalize_strips_volatile(fixture_json):
    n = normalize(fixture_json)
    assert not any("metainfo" in i for i in n)
    assert not _contains_key(n, {"handle", "expires", "packets", "bytes", "elem"} - {"elem"})
    assert all("elem" not in i["set"] for i in n if "set" in i)
    assert fixture_json["nftables"][0].get("metainfo")  # 入力は変更されない


def test_fixture_matches_itself(fixture_json, cfg):
    n = normalize(fixture_json)
    assert n == normalize(copy.deepcopy(fixture_json))
    assert check_required_shapes(n, cfg) == []
    assert len(REQUIRED_RULE_SHAPES) >= 4


def _mutate(j, mutation):
    rules = [i for i in j["nftables"] if "rule" in i]
    if mutation == "prepend_accept":
        r = copy.deepcopy(rules[0])
        r["rule"]["expr"] = [{"accept": None}]
        j["nftables"].insert(j["nftables"].index(rules[0]), r)
    elif mutation == "swap_rules":
        a, b = j["nftables"].index(rules[1]), j["nftables"].index(rules[2])
        j["nftables"][a], j["nftables"][b] = j["nftables"][b], j["nftables"][a]
    elif mutation == "drop_rule":
        j["nftables"].remove(rules[-1])
    elif mutation == "remove_timeout_flag":
        next(i for i in j["nftables"] if "set" in i)["set"]["flags"].remove("timeout")
    elif mutation == "change_set_size":
        next(i for i in j["nftables"] if "set" in i)["set"]["size"] = 1024


@pytest.mark.parametrize("mutation", ["prepend_accept", "swap_rules", "drop_rule",
                                      "remove_timeout_flag", "change_set_size"])
def test_compare_detects_mutations(fixture_json, env, monkeypatch, mutation):
    j = copy.deepcopy(fixture_json)
    _mutate(j, mutation)
    monkeypatch.setattr(fwcheck, "RUN", _runner(j))
    assert fwcheck.main([]) == 1


def test_main_passes_on_fixture_and_ignores_volatile(fixture_json, env, monkeypatch):
    j = copy.deepcopy(fixture_json)
    for i in j["nftables"]:
        if "set" in i:
            i["set"]["elem"] = [{"elem": {"val": "1.2.3.4", "timeout": 600, "expires": 5}}]
        if "rule" in i:
            i["rule"]["handle"] += 100
            for e in i["rule"]["expr"]:
                if "counter" in e:
                    e["counter"] = {"packets": 9, "bytes": 999}
    monkeypatch.setattr(fwcheck, "RUN", _runner(j))
    assert fwcheck.main([]) == 0


def test_main_fails_closed_on_bad_config(monkeypatch):
    def bad():
        raise ValidationError("x")
    calls = []
    monkeypatch.setattr(fwcheck, "load_config", bad)
    monkeypatch.setattr(fwcheck, "RUN", _runner({}, calls))
    assert fwcheck.main([]) == 1
    assert calls == []


@pytest.mark.parametrize("failure", ["rc", "timeout", "oserror", "badjson"])
def test_main_fails_on_runner_error(env, monkeypatch, failure):
    def run(cmd):
        if failure == "rc":
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, 10)
        if failure == "oserror":
            raise FileNotFoundError()
        return SimpleNamespace(returncode=0, stdout="{not json", stderr="")
    monkeypatch.setattr(fwcheck, "RUN", run)
    assert fwcheck.main([]) == 1


def test_expected_json_substitutes_mac_and_checks_sources(fixture_json, cfg, tmp_path):
    assert expected_json(cfg, TEMPLATE)["nftables"]
    new_mac = "11:22:33:44:55:66"
    cfg2 = type(cfg)(**{**cfg.__dict__, "trusted_sources": (
        TrustedSource("enp3s0", "192.0.2.0/24", new_mac), cfg.trusted_sources[1])})
    text = json.dumps(expected_json(cfg2, TEMPLATE))
    assert new_mac in text and "aa:bb:cc:dd:ee:ff" not in text
    for bad_sources in (
        (TrustedSource("eth9", "192.0.2.0/24", "aa:bb:cc:dd:ee:ff"), cfg.trusted_sources[1]),
        (TrustedSource("enp3s0", "192.0.2.0/24", None), cfg.trusted_sources[1]),
        (cfg.trusted_sources[0],),
        (cfg.trusted_sources[1], cfg.trusted_sources[0]),
    ):
        with pytest.raises(ValidationError):
            expected_json(type(cfg)(**{**cfg.__dict__, "trusted_sources": bad_sources}), TEMPLATE)
    for field, value in (("java_port", 25566), ("bedrock_port", 19133), ("grant_timeout_sec", 300)):
        with pytest.raises(ValidationError):
            expected_json(type(cfg)(**{**cfg.__dict__, field: value}), TEMPLATE)


def _with_cidrs(cfg, lan, wg):
    s = cfg.trusted_sources
    return type(cfg)(**{**cfg.__dict__, "trusted_sources": (
        TrustedSource(s[0].iface, lan, s[0].exclude_ether_saddr), TrustedSource(s[1].iface, wg, None))})


def _saddr_prefixes(j):
    out = []
    for i in j["nftables"]:
        for e in i.get("rule", {}).get("expr", []):
            m = e.get("match", {})
            if m.get("left", {}).get("payload", {}).get("field") == "saddr" \
                    and m["left"]["payload"].get("protocol") == "ip" and isinstance(m["right"], dict):
                out.append(m["right"])
    return out


def test_expected_json_substitutes_cidrs_in_order(cfg):
    real = _with_cidrs(cfg, "172.16.0.0/16", "172.31.5.0/28")
    assert _saddr_prefixes(expected_json(real, TEMPLATE)) == [
        {"prefix": {"addr": "172.16.0.0", "len": 16}}, {"prefix": {"addr": "172.31.5.0", "len": 28}}]


def test_template_holds_only_documentation_cidrs():
    # 公開リポジトリの雛形に実際の LAN・WireGuard の範囲を置かない（例示用の RFC 5737 だけ）
    docs = [ipaddress.IPv4Network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
    prefixes = _saddr_prefixes(json.loads(TEMPLATE.read_text()))
    assert prefixes
    for p in prefixes:
        net = ipaddress.IPv4Network(f"{p['prefix']['addr']}/{p['prefix']['len']}")
        assert any(net.subnet_of(d) for d in docs), net


def test_main_uses_config_cidrs_not_template_cidrs(fixture_json, cfg, env, monkeypatch):
    real = _with_cidrs(cfg, "172.16.0.0/16", "172.31.5.0/28")
    monkeypatch.setattr(fwcheck, "load_config", lambda: real)
    live = expected_json(real, TEMPLATE)
    monkeypatch.setattr(fwcheck, "RUN", _runner(live))
    assert fwcheck.main([]) == 0
    # 雛形の例示アドレスのままのルールは、設定と一致しないので失敗する
    monkeypatch.setattr(fwcheck, "RUN", _runner(fixture_json))
    assert fwcheck.main([]) == 1


def test_expected_json_bad_template(cfg, tmp_path):
    with pytest.raises(ValidationError):
        expected_json(cfg, tmp_path / "missing.json")
    p = tmp_path / "bad.json"
    p.write_text("{oops")
    with pytest.raises(ValidationError):
        expected_json(cfg, p)


def test_fwcheck_runs_only_nft_list(fixture_json, env, monkeypatch):
    calls = []
    monkeypatch.setattr(fwcheck, "RUN", _runner(fixture_json, calls))
    assert fwcheck.main([]) == 0
    assert calls == [NFT_CMD]


def test_mismatch_log_is_short_and_ascii(fixture_json, env, monkeypatch, capsys):
    j = copy.deepcopy(fixture_json)
    _mutate(j, "drop_rule")
    monkeypatch.setattr(fwcheck, "RUN", _runner(j))
    assert fwcheck.main([]) == 1
    out = capsys.readouterr().out
    assert "index" in out and len(out) < 200 and out.isascii()


def test_import_check_and_launcher(capsys):
    assert fwcheck.main(["--import-check"]) == 0
    assert "import-ok" in capsys.readouterr().out
    launcher = Path(__file__).resolve().parents[2] / "host" / "lib" / "fwcheck"
    assert launcher.read_text() == "#!/usr/bin/python3 -Es\nimport fwcheck, sys; sys.exit(fwcheck.main())\n"
    assert launcher.stat().st_mode & stat.S_IXUSR
