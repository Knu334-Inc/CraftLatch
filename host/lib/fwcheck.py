"""CraftLatch fwcheck: live の nft テーブルが期待構造と完全一致しなければ失敗する。

RestrictNamespaces=yes 下で動くため、名前空間は作らず nft -j list だけを実行する。
"""
import copy
import json
import subprocess
import sys
from pathlib import Path

from common import Config, ValidationError, load_config

TEMPLATE_PATH = Path("/usr/local/lib/craftlatch/fwcheck-expected.json")
PLACEHOLDER_MAC = "aa:bb:cc:dd:ee:ff"
NFT_CMD = ["nft", "-j", "list", "table", "inet", "craftlatch"]


def _log(line: str) -> None:
    # 非 ASCII・surrogate でも例外を出さない
    print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, shell=False, timeout=10, check=False)


RUN = _run


def _strip(obj):
    if isinstance(obj, dict):
        if set(obj) == {"packets", "bytes"}:
            return {}
        return {k: _strip(v) for k, v in obj.items() if k not in ("handle", "expires")}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def normalize(nft_json: dict) -> list:
    out = []
    for item in nft_json.get("nftables", []):
        if not isinstance(item, dict) or "metainfo" in item:
            continue
        item = copy.deepcopy(item)
        if isinstance(item.get("set"), dict):
            item["set"].pop("elem", None)
        out.append(_strip(item))
    return out


def _rules(norm: list) -> list[list]:
    return [i["rule"]["expr"] for i in norm if isinstance(i, dict) and "rule" in i]


def _match_of(expr: list, left_key: str, field: str | None = None):
    for e in expr:
        m = e.get("match") if isinstance(e, dict) else None
        if not m:
            continue
        left = m.get("left", {})
        if left_key == "meta" and left.get("meta", {}).get("key") == field:
            return m
        if left_key == "payload" and left.get("payload", {}).get("protocol") == field[0] \
                and left.get("payload", {}).get("field") == field[1]:
            return m
    return None


def _template_sources(rules: list[list]) -> list[tuple]:
    out = []
    for expr in rules:
        iif = _match_of(expr, "meta", "iifname")
        ip = _match_of(expr, "payload", ("ip", "saddr"))
        if iif is None or ip is None:
            continue
        prefix = ip.get("right", {}).get("prefix", {})
        eth = _match_of(expr, "payload", ("ether", "saddr"))
        out.append((iif.get("right"), f"{prefix.get('addr')}/{prefix.get('len')}",
                    eth is not None and eth.get("op") == "!="))
    return out


def _replace_mac(obj, mac: str):
    if isinstance(obj, str):
        return mac if obj == PLACEHOLDER_MAC else obj
    if isinstance(obj, dict):
        return {k: _replace_mac(v, mac) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_replace_mac(v, mac) for v in obj]
    return obj


def expected_json(cfg: Config, template_path: Path = TEMPLATE_PATH) -> dict:
    try:
        tpl = json.loads(Path(template_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValidationError(f"雛形を読めません: {type(e).__name__}") from e
    try:
        norm = normalize(tpl)
        rules = _rules(norm)
        want = [(s.iface, s.cidr, s.exclude_ether_saddr is not None) for s in cfg.trusted_sources]
        if _template_sources(rules) != want:
            raise ValidationError("trusted_sources が雛形と一致しません。雛形を取り直してください")
        sets = [i["set"] for i in norm if "set" in i]
        tpl_timeout = sets[0].get("timeout")
        tpl_java = _match_of(rules[-2], "payload", ("tcp", "dport"))["right"]
        tpl_bedrock = _match_of(rules[-1], "payload", ("udp", "dport"))["right"]
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise ValidationError("雛形の構造が不正です") from e
    if (cfg.java_port, cfg.bedrock_port, cfg.grant_timeout_sec) != (tpl_java, tpl_bedrock, tpl_timeout):
        raise ValidationError("ポートまたは許可期限が雛形と一致しません。雛形を取り直してください")
    mac = next((s.exclude_ether_saddr for s in cfg.trusted_sources if s.exclude_ether_saddr), None)
    return _replace_mac(tpl, mac) if mac else tpl


def _check_shapes_impl():
    def one_set(n, cfg):
        sets = [i["set"] for i in n if "set" in i]
        return (len(sets) == 1 and sets[0].get("name") == "allow4"
                and {"dynamic", "timeout"} <= set(sets[0].get("flags", [])))

    def lo_first(n, cfg):
        r = _rules(n)
        return bool(r) and r[0] == [
            {"match": {"op": "==", "left": {"meta": {"key": "iif"}}, "right": "lo"}},
            {"accept": None}]

    def drops_last(n, cfg):
        r = _rules(n)

        def drop(expr, proto, port):
            return expr == [
                {"match": {"op": "==", "left": {"payload": {"protocol": proto, "field": "dport"}},
                           "right": port}},
                {"counter": {}}, {"drop": None}]
        return len(r) >= 2 and drop(r[-2], "tcp", cfg.java_port) and drop(r[-1], "udp", cfg.bedrock_port)

    def one_chain(n, cfg):
        ch = [i["chain"] for i in n if "chain" in i]
        return (len(ch) == 1 and ch[0].get("name") == "input" and ch[0].get("hook") == "input"
                and ch[0].get("policy") == "accept")

    return (("allow4_set", one_set), ("lo_accept_first", lo_first),
            ("drop_rules_last", drops_last), ("input_chain", one_chain))


REQUIRED_RULE_SHAPES = _check_shapes_impl()


def check_required_shapes(normalized: list, cfg: Config) -> list[str]:
    failed = []
    for name, fn in REQUIRED_RULE_SHAPES:
        try:
            ok = fn(normalized, cfg)
        except (KeyError, IndexError, TypeError, AttributeError):
            ok = False
        if not ok:
            failed.append(name)
    return failed


def _first_diff(a: list, b: list) -> str:
    for i in range(max(len(a), len(b))):
        if i >= len(a) or i >= len(b):
            return f"index {i}: length differs (live={len(a)} expected={len(b)})"
        if a[i] != b[i]:
            kind = next(iter(a[i]), "?") if isinstance(a[i], dict) else "?"
            return f"index {i}: kind {kind} differs"
    return "no difference"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--import-check" in argv:
        print("import-ok")
        return 0
    try:
        cfg = load_config()
        expected = normalize(expected_json(cfg, TEMPLATE_PATH))
    except ValidationError as e:
        _log(f"fwcheck: config/template error: {e}")
        return 1
    try:
        proc = RUN(list(NFT_CMD))
        if proc.returncode != 0:
            _log(f"fwcheck: nft failed rc={proc.returncode}")
            return 1
        live_raw = json.loads(proc.stdout)
        live = normalize(live_raw)
    except (subprocess.TimeoutExpired, OSError, ValueError, AttributeError, TypeError) as e:
        _log(f"fwcheck: cannot read live table: {type(e).__name__}")
        return 1
    if live != expected:
        _log("fwcheck: mismatch at " + _first_diff(live, expected))
        return 1
    failed = check_required_shapes(live, cfg)
    if failed:
        _log("fwcheck: shape check failed: " + ",".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
