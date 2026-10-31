"""CraftLatch nftgen: 設定から nft ルールファイルを生成する。"""
import argparse
import os
import sys
from pathlib import Path

from common import (
    Config,
    ValidationError,
    atomic_write_text,
    load_config,
    validate_cidr,
    validate_iface,
    validate_mac,
)


def _timeout(sec: int) -> str:
    return f"{sec // 60}m" if sec % 60 == 0 else f"{sec}s"


def render_ruleset(cfg: Config) -> str:
    ports = f"{{ {int(cfg.java_port)}, {int(cfg.bedrock_port)} }}"
    java, bedrock = int(cfg.java_port), int(cfg.bedrock_port)
    timeout = _timeout(int(cfg.grant_timeout_sec))
    src_lines = []
    for s in cfg.trusted_sources:
        iface = validate_iface(s.iface)
        cidr = validate_cidr(s.cidr)
        mac = ""
        if s.exclude_ether_saddr is not None:
            mac = f"ether saddr != {validate_mac(s.exclude_ether_saddr)} "
        src_lines.append(
            f'    iifname "{iface}" {mac}ip saddr {cidr} meta l4proto {{ tcp, udp }} '
            f"th dport {ports} counter accept\n"
        )
    return (
        "#!/usr/sbin/nft -f\n"
        "table inet craftlatch\n"
        "delete table inet craftlatch\n"
        "table inet craftlatch {\n"
        "  set allow4 {\n"
        "    type ipv4_addr\n"
        "    flags dynamic, timeout\n"
        f"    timeout {timeout}\n"
        "    size 256\n"
        "  }\n"
        "  chain input {\n"
        "    type filter hook input priority filter - 10; policy accept;\n"
        '    iif "lo" accept\n'
        + "".join(src_lines)
        + f"    tcp dport {java} ip saddr @allow4 update @allow4 {{ ip saddr timeout {timeout} }} counter accept\n"
        f"    udp dport {bedrock} ip saddr @allow4 update @allow4 {{ ip saddr timeout {timeout} }} counter accept\n"
        f"    tcp dport {java} counter drop\n"
        f"    udp dport {bedrock} counter drop\n"
        "  }\n"
        "}\n"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nftgen")
    ap.add_argument("--output")
    ap.add_argument("--import-check", action="store_true")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if args.import_check:
        print("import-ok")
        return 0
    if not args.output:
        print("nftgen: --output が必要です", file=sys.stderr)
        return 1
    out = Path(args.output)
    tmp = out.with_name(out.name + ".tmp")
    try:
        text = render_ruleset(load_config())
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fchmod(f.fileno(), 0o644)
            os.fsync(f.fileno())
        os.replace(tmp, out)
    except (ValidationError, OSError) as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        print(f"nftgen: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
