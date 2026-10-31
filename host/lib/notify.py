"""失敗通知: Discord Webhook に短いメッセージを送る（同一ユニットは 60 分に 1 回まで）。"""

import json
import os
import re
import socket
import time
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import atomic_write_text

UNIT_RE = re.compile(r"^[A-Za-z0-9@._-]{1,128}$")
WINDOW = 3600
JST = timezone(timedelta(hours=9))


def _log(line: str) -> None:
    # 非 ASCII・surrogate でも例外を出さない
    print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def _last_path(state_dir: Path, unit: str) -> Path:
    return Path(state_dir) / f"{unit}.last"


def should_send(state_dir: Path, unit: str, now: int, window: int = WINDOW) -> bool:
    try:
        last = int(_last_path(state_dir, unit).read_text().strip())
    except (OSError, ValueError):
        return True
    return now - last >= window


def record_sent(state_dir: Path, unit: str, now: int) -> None:
    atomic_write_text(_last_path(state_dir, unit), f"{now}\n", 0o600)


def build_payload(hostname: str, unit: str, now: int) -> dict:
    ts = datetime.fromtimestamp(now, JST).isoformat(timespec="seconds")
    return {
        "content": f"[CraftLatch] {hostname}: {unit} が失敗しました ({ts})。"
        f"確認: journalctl -u {unit} -n 50"
    }


def post_webhook(url: str, payload: dict, timeout: float = 10.0) -> None:
    if not url.startswith("https://"):
        raise ValueError("https の URL ではありません")
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "CraftLatch-notify/1"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = resp.status
        if not 200 <= status < 300:
            raise RuntimeError(f"HTTP {status}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if args == ["--import-check"]:
        print("import-ok")
        return 0
    if len(args) != 1 or not UNIT_RE.fullmatch(args[0]):
        _log("notify: ユニット名が不正です")
        return 1
    unit = args[0]
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    state_dir = os.environ.get("STATE_DIRECTORY")
    if not cred_dir or not state_dir:
        _log("notify: CREDENTIALS_DIRECTORY / STATE_DIRECTORY が未設定です")
        return 1
    try:
        url = (Path(cred_dir) / "webhook").read_text().strip()
    except OSError as e:
        _log(f"notify: webhook を読めません: {type(e).__name__}")
        return 1
    now = int(time.time())
    state = Path(state_dir)
    if not should_send(state, unit, now):
        _log(f"notify: {unit} は 60 分以内に通知済みのため送信しません")
        return 0
    try:
        post_webhook(url, build_payload(socket.gethostname(), unit, now))
        record_sent(state, unit, now)
    except Exception as e:  # noqa: BLE001 - 種類名だけ記録する（URL・本文は出さない）
        _log(f"notify: 送信に失敗しました: {type(e).__name__}")
        return 1
    _log(f"notify: {unit} の失敗を通知しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
