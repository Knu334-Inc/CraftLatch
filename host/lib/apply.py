"""CraftLatch apply: root で要求ファイルを再検証し、nft と systemd に適用する。"""
import json
import os
import pwd
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from common import (
    QUARANTINE_DIR,
    REQUESTS_DIR,
    STATE_DIR,
    AllowGrant,
    StopRequest,
    ValidationError,
    atomic_write_text,
    parse_request,
)

Runner = Callable[[list[str], str | None], subprocess.CompletedProcess]

MAX_REQUEST_BYTES = 1024
MAX_FILES_PER_RUN = 100
MAX_QUARANTINE = 100
TMP_MAX_AGE = 60
UNIT = "minecraft.service"


def _log(line: str) -> None:
    print(line, flush=True)


def default_runner(cmd: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, input=stdin, text=True, capture_output=True, shell=False, timeout=10, check=False
    )


def read_request_file(path: Path, expected_uid: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise ValidationError(f"開けません: {type(e).__name__}") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValidationError("通常ファイルではありません")
        if st.st_uid != expected_uid:
            raise ValidationError("所有者が不正です")
        if st.st_size > MAX_REQUEST_BYTES:
            raise ValidationError("サイズが上限を超えています")
        data = os.read(fd, MAX_REQUEST_BYTES + 1)
        if len(data) > MAX_REQUEST_BYTES:
            raise ValidationError("サイズが上限を超えています")
        return data
    except OSError as e:
        raise ValidationError(f"読み取れません: {type(e).__name__}") from e
    finally:
        os.close(fd)


def _trim_quarantine(quarantine_dir: Path) -> None:
    entries = []
    for p in quarantine_dir.iterdir():
        try:
            entries.append((p.lstat().st_mtime_ns, p.name, p))
        except OSError:
            continue
    entries.sort()
    for _, _, p in entries[: max(0, len(entries) - MAX_QUARANTINE)]:
        try:
            p.unlink()
        except OSError as e:
            _log(f"event=quarantine-trim-failed name={p.name} error={type(e).__name__}")


def _quarantine(path: Path, quarantine_dir: Path) -> bool:
    """隔離する。隔離も削除もできなければ False。"""
    dest = quarantine_dir / path.name
    try:
        if os.path.lexists(dest):
            dest = quarantine_dir / f"{path.name}.{uuid.uuid4().hex[:8]}"
        os.rename(path, dest)
        return True
    except OSError:
        pass
    try:
        path.unlink()
        return True
    except OSError as e:
        _log(f"event=quarantine-failed name={path.name} error={type(e).__name__}")
        return False


def collect(requests_dir: Path, quarantine_dir: Path, expected_uid: int, now: int):
    allows: list[tuple[Path, AllowGrant]] = []
    stops: list[tuple[Path, StopRequest]] = []
    failed = False
    quarantined = False

    try:
        names = sorted(os.listdir(requests_dir))
    except OSError as e:
        _log(f"event=list-failed error={type(e).__name__}")
        return allows, stops, True

    for name in names:
        if name.startswith(".tmp-"):
            p = requests_dir / name
            try:
                if now - p.lstat().st_mtime > TMP_MAX_AGE:
                    p.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                _log(f"event=tmp-cleanup-failed name={name} error={type(e).__name__}")
                failed = True

    req_names = [n for n in names if n.endswith(".req") and not n.startswith(".")]
    for name in req_names[:MAX_FILES_PER_RUN]:
        path = requests_dir / name
        try:
            parsed = parse_request(read_request_file(path, expected_uid), now)
        except Exception as e:  # 1 件の失敗で他を止めない
            _log(f"event=invalid-request name={name} reason={str(e)!r}")
            quarantined = True
            if not _quarantine(path, quarantine_dir):
                failed = True
            continue
        if isinstance(parsed, AllowGrant):
            allows.append((path, parsed))
        else:
            stops.append((path, parsed))

    if quarantined:
        try:
            _trim_quarantine(quarantine_dir)
        except OSError as e:
            _log(f"event=quarantine-trim-failed error={type(e).__name__}")
    return allows, stops, failed


def build_nft_batch(ips: list[str]) -> str:
    lines = []
    for ip in ips:
        lines.append(f"add element inet craftlatch allow4 {{ {ip} }}\n")
        lines.append(f"delete element inet craftlatch allow4 {{ {ip} }}\n")
        lines.append(f"add element inet craftlatch allow4 {{ {ip} timeout 10m }}\n")
    return "".join(lines)


class StateStore:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)

    def last_allow(self) -> int:
        try:
            v = int((self.dir / "last-allow").read_text().strip())
            return v if v >= 0 else 0
        except (OSError, ValueError):
            return 0

    def set_last_allow(self, now: int) -> None:
        atomic_write_text(self.dir / "last-allow", f"{now}\n", 0o644)

    def seen(self) -> dict[str, int]:
        try:
            obj = json.loads((self.dir / "seen.json").read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(obj, dict):
            return {}
        return {k: v for k, v in obj.items() if isinstance(k, str) and type(v) is int}

    def mark_seen(self, ids_exp: dict[str, int], now: int) -> None:
        merged = {**self.seen(), **ids_exp}
        merged = {k: v for k, v in merged.items() if v > now}
        atomic_write_text(self.dir / "seen.json", json.dumps(merged, sort_keys=True), 0o644)


class Systemd:
    def __init__(self, runner: Runner):
        self.runner = runner

    def start_no_block(self) -> bool:
        return self.runner(["systemctl", "start", "--no-block", UNIT], None).returncode == 0

    def stop_no_block(self) -> bool:
        return self.runner(["systemctl", "stop", "--no-block", UNIT], None).returncode == 0

    def _show(self, prop: str) -> str:
        r = self.runner(["systemctl", "show", "-p", prop, "--value", UNIT], None)
        return (r.stdout or "").strip() if r.returncode == 0 else ""

    def invocation_id(self) -> str:
        return self._show("InvocationID")

    def active_state(self) -> str:
        return self._show("ActiveState")


def _cleanup(paths: list[Path]) -> bool:
    ok = True
    for p in paths:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            _log(f"event=cleanup-failed name={p.name} error={type(e).__name__}")
            ok = False
    return ok


def apply_batch(allows, stops, store: StateStore, systemd: Systemd, runner: Runner, now: int) -> bool:
    ok = True
    files = [p for p, _ in allows] + [p for p, _ in stops]

    seen = store.seen()
    fresh: list[AllowGrant] = []
    for _, g in allows:
        if g.id in seen:
            _log(f"event=allow id={g.id} result=duplicate")
        else:
            fresh.append(g)

    if fresh:
        ips = list(dict.fromkeys(g.ip for g in fresh))
        r = runner(["nft", "-f", "-"], build_nft_batch(ips))
        if r.returncode != 0:
            _log(f"event=allow nft=error ids={','.join(g.id for g in fresh)}")
            _cleanup(files)
            return False
        store.set_last_allow(now)
        started = systemd.start_no_block()
        store.mark_seen({g.id: g.exp for g in fresh}, now)
        for g in fresh:
            _log(f"event=allow ip={g.ip} user={g.user} id={g.id} nft=ok start_job={'ok' if started else 'error'}")
        if not started:
            ok = False

    if allows:
        # 同じ回に allow があれば stop はすべて破棄（allow 優先）
        for _, s in stops:
            _log(f"event=stop reason={s.reason} result=stale")
    elif stops:
        valid = False
        invocation = active = None
        for _, s in stops:
            good = now < s.exp and s.iat > store.last_allow()
            if good:
                if invocation is None:
                    invocation = systemd.invocation_id()
                good = s.invocation == invocation
            if good:
                if active is None:
                    active = systemd.active_state()
                good = active == "active"
            if good:
                valid = True
            else:
                _log(f"event=stop reason={s.reason} result=stale")
        if valid:
            if systemd.stop_no_block():
                _log("event=stop reason=idle result=ok")
            else:
                _log("event=stop reason=idle result=error")
                ok = False

    if not _cleanup(files):
        ok = False
    return ok


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--import-check" in argv:
        print("import-ok")
        return 0
    uid = pwd.getpwnam("craftlatch").pw_uid
    now = int(time.time())
    allows, stops, failed = collect(REQUESTS_DIR, QUARANTINE_DIR, uid, now)
    store = StateStore(STATE_DIR)
    systemd = Systemd(default_runner)
    ok = apply_batch(allows, stops, store, systemd, default_runner, now)
    return 1 if failed or not ok else 0


if __name__ == "__main__":
    sys.exit(main())
