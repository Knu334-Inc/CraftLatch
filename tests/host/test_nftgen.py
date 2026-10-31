import dataclasses
import re
from pathlib import Path

import pytest

import nftgen
from common import TrustedSource, ValidationError
from nftgen import render_ruleset

PROBE = Path(__file__).parent / "fixtures" / "craftlatch-probe.nft"


def test_render_contains_rules_in_order(cfg):
    lines = [l.strip() for l in render_ruleset(cfg).splitlines()]
    i = lines.index('iif "lo" accept')
    assert lines[i+1] == 'iifname "enp3s0" ether saddr != aa:bb:cc:dd:ee:ff ip saddr 192.168.33.0/24 meta l4proto { tcp, udp } th dport { 25565, 19132 } counter accept'
    assert lines[i+2].startswith('iifname "wg0" ip saddr 10.0.0.0/24')
    assert lines[-4:-2] == ["tcp dport 25565 counter drop", "udp dport 19132 counter drop"]


@pytest.mark.parametrize("src", [
    TrustedSource('en p0', "192.168.33.0/24", None),
    TrustedSource('en"0', "192.168.33.0/24", None),
    TrustedSource("enp3s0", "192.168.33.1/24", None),
    TrustedSource("enp3s0", "192.168.33.0/24", "AA:BB:CC:DD:EE:FF"),
    TrustedSource("enp3s0", "192.168.33.0/24", "<router-mac>"),
])
def test_render_rejects_bad_iface_or_mac(cfg, src):
    bad = dataclasses.replace(cfg, trusted_sources=(src,))
    with pytest.raises(ValidationError):
        render_ruleset(bad)


def test_generator_matches_probe_source(cfg):
    text = render_ruleset(cfg)
    src = PROBE.read_text(encoding="utf-8")
    assert re.sub(r"\s+", " ", text) == re.sub(r"\s+", " ", src)
    assert text == src


def test_timeout_rendering(cfg):
    assert "timeout 10m" in render_ruleset(cfg)
    assert "timeout 90s" in render_ruleset(dataclasses.replace(cfg, grant_timeout_sec=90))


def test_main_writes_atomically_and_keeps_file_on_error(cfg, tmp_path, monkeypatch):
    out = tmp_path / "rules.nft"
    out.write_text("old")
    monkeypatch.setattr(nftgen, "load_config", lambda: cfg)
    assert nftgen.main(["--output", str(out)]) == 0
    assert out.read_text() == render_ruleset(cfg)
    assert oct(out.stat().st_mode & 0o777) == "0o644"
    assert not (tmp_path / "rules.nft.tmp").exists()

    def bad():
        raise ValidationError("x")
    out.write_text("old")
    monkeypatch.setattr(nftgen, "load_config", bad)
    assert nftgen.main(["--output", str(out)]) == 1
    assert out.read_text() == "old"
    assert not (tmp_path / "rules.nft.tmp").exists()


def test_import_check(capsys):
    assert nftgen.main(["--import-check"]) == 0
    assert "import-ok" in capsys.readouterr().out
