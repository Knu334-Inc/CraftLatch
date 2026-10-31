import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "host" / "lib" / "mc-stop"

READER = (
    "import sys\n"
    "f = open(sys.argv[1])\n"
    "while True:\n"
    "    line = f.readline()\n"
    "    if line == '':\n"
    "        continue\n"
    "    if line.strip() == 'stop':\n"
    "        break\n"
)


def run(args, env_extra, timeout=15):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MC_")}
    env.update(env_extra)
    return subprocess.run(
        ["/bin/sh", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=timeout
    )


def test_mc_stop_writes_stop_and_waits(tmp_path):
    fifo = tmp_path / "console"
    os.mkfifo(fifo)
    child = subprocess.Popen([sys.executable, "-c", READER, str(fifo)])
    # 終了した子をすぐ回収する（ゾンビだと kill -0 が成功し続けるため）
    reaper = threading.Thread(target=child.wait, daemon=True)
    reaper.start()
    try:
        start = time.time()
        r = run([str(child.pid)], {"MC_CONSOLE": str(fifo), "MC_STOP_WAIT": "10"})
        elapsed = time.time() - start
        assert r.returncode == 0, r.stderr
        # 待機上限(10 秒)を使い切らず、子の終了を検知して戻ること
        assert elapsed < 5, elapsed
        reaper.join(timeout=5)
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
        reaper.join(timeout=5)


def test_mc_stop_empty_pid_is_noop():
    assert run([], {}).returncode == 0
    assert run([""], {}).returncode == 0


def test_mc_stop_rejects_bad_pid(tmp_path):
    r = run(["12;x"], {"MC_CONSOLE": str(tmp_path / "none")})
    assert r.returncode == 1


def test_mc_stop_missing_fifo_fails_fast(tmp_path):
    start = time.time()
    r = run(["1"], {"MC_CONSOLE": str(tmp_path / "none")}, timeout=6)
    assert r.returncode != 0
    assert time.time() - start < 6
    assert str(tmp_path / "none") in r.stderr


def test_mc_stop_uses_default_path_when_unset():
    if os.path.exists("/run/minecraft.console"):
        pytest.skip("/run/minecraft.console が存在する")
    r = run(["1"], {})
    assert r.returncode != 0
    assert "/run/minecraft.console" in r.stderr


def test_mc_stop_unwritable_fifo_times_out(tmp_path):
    # 読み手のいない FIFO への書き込みは 5 秒で打ち切られ 1 を返す
    fifo = tmp_path / "console"
    os.mkfifo(fifo)
    start = time.time()
    r = run(["1"], {"MC_CONSOLE": str(fifo)}, timeout=15)
    assert r.returncode == 1
    assert time.time() - start < 10
