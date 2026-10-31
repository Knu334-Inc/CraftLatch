"""CraftLatch routermac: デフォルトゲートウェイの MAC を設定へ補完する。"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from common import ValidationError, atomic_write_text, validate_mac

_GOOD_STATES = {"REACHABLE", "STALE", "DELAY", "PERMANENT"}
_IPV4_RE = re.compile(r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, shell=False, timeout=10, check=False)


RUN = _run


def default_gateway(out: str) -> str:
    for line in out.splitlines():
        t = line.split()
        if len(t) >= 3 and t[0] == "default" and t[1] == "via" and _IPV4_RE.fullmatch(t[2]):
            return t[2]
    raise ValidationError("デフォルトルートが見つかりません")


def neigh_mac(out: str) -> str | None:
    for line in out.splitlines():
        t = line.split()
        if "lladdr" not in t or not t or t[-1] not in _GOOD_STATES:
            continue
        i = t.index("lladdr")
        if i + 1 < len(t):
            mac = t[i + 1].lower()
            try:
                return validate_mac(mac)
            except ValidationError:
                continue
    return None


def _is_unset(v) -> bool:
    return v is None or (isinstance(v, str) and v.startswith("<"))


def _load(config_path: Path) -> dict:
    try:
        with open(config_path, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError) as e:
        raise ValidationError(f"設定ファイルを読めません: {type(e).__name__}") from e
    srcs = obj.get("trusted_sources") if isinstance(obj, dict) else None
    if not isinstance(srcs, list):
        raise ValidationError("trusted_sources がありません")
    return obj


def _targets(obj: dict) -> list[dict]:
    t = [s for s in obj["trusted_sources"] if isinstance(s, dict) and "exclude_ether_saddr" in s]
    if not t:
        raise ValidationError("exclude_ether_saddr を持つ trusted_sources がありません")
    return t


def set_router_mac(config_path: Path, mac: str, overwrite: bool) -> bool:
    mac = validate_mac(mac)
    obj = _load(config_path)
    targets = _targets(obj)
    wrote = False
    for s in targets:
        if _is_unset(s["exclude_ether_saddr"]) or overwrite:
            s["exclude_ether_saddr"] = mac
            wrote = True
        atomic_write_text(Path(config_path), json.dumps(obj, ensure_ascii=False, indent=2) + "\n", 0o644)
    return wrote
    return True


def _already_set(config_path: Path) -> bool:
    return not any(_is_unset(s["exclude_ether_saddr"]) for s in _targets(_load(config_path)))


def _out(cmd: list[str]) -> str:
    try:
        p = RUN(cmd)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return p.stdout if p.returncode == 0 else ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="routermac")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--config", default="/etc/craftlatch/config.json")
    ap.add_argument("--import-check", action="store_true")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if args.import_check:
        print("import-ok")
        return 0
    path = Path(args.config)
    try:
        if _already_set(path) and not args.refresh:
            print("ルーター MAC は設定済みです")
            return 0
        gw = default_gateway(_out(["ip", "route", "show", "default"]))
        mac = neigh_mac(_out(["ip", "neigh", "show", gw]))
        if mac is None:
            _out(["ping", "-c", "1", "-W", "2", gw])
            mac = neigh_mac(_out(["ip", "neigh", "show", gw]))
        if mac is None:
            print("ルーターの MAC を取得できませんでした", file=sys.stderr)
            return 1
        print(f"ゲートウェイ {gw} の MAC: {mac}")
        if not args.yes and input("この MAC を設定しますか? [y/N] ").strip().lower() != "y":
            print("中止しました")
            return 1
        set_router_mac(path, mac, overwrite=args.refresh)
    except ValidationError as e:
        print(f"routermac: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
