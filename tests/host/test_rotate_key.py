"""craftlatch-rotate-key のテスト。偽の aws・systemctl で段階別の挙動を検査する。"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "host" / "scripts" / "craftlatch-rotate-key"
OLD = "AKIAOLDKEY0000000001"
NEW = "AKIANEWKEY0000000002"
ARN = "arn:aws:iam::123456789012:user/craftlatch-host"

FAKE_AWS = f"""#!/bin/sh
echo "aws $* | KEY=${{AWS_ACCESS_KEY_ID:-}} CRED=${{AWS_SHARED_CREDENTIALS_FILE:-}} PROFILE=${{AWS_PROFILE:-}}" >> "@LOG@"
case "$*" in
  *create-access-key*) printf '{NEW}\\tnewsecretvalue\\n' ;;
  *get-caller-identity*)
    [ "$(cat "@MODE@" 2>/dev/null)" = verify ] && exit 255
    echo "{ARN}" ;;
  *update-access-key*) exit 0 ;;
  *delete-access-key*{OLD}*)
    [ "$(cat "@MODE@" 2>/dev/null)" = delete_old ] && {{ echo "boom" >&2; exit 255; }}
    [ "$(cat "@MODE@" 2>/dev/null)" = nosuch_old ] && {{ echo "NoSuchEntity: gone" >&2; exit 254; }}
    exit 0 ;;
  *delete-access-key*) exit 0 ;;
esac
"""

FAKE_SYSTEMCTL = f"""#!/bin/sh
echo "systemctl $*" >> "@LOG@"
case "$*" in
  "is-active craftlatch-poll.service")
    echo "${{FAKE_ACTIVE:-inactive}}"
    [ "${{FAKE_ACTIVE:-inactive}}" = inactive ] && exit 3
    exit 0 ;;
  "start craftlatch-poll.service")
    if [ "$(cat "@MODE@" 2>/dev/null)" = receive_both ]; then
      echo 1 > "$CRAFTLATCH_STATE/poll-failures"
    elif [ "$(cat "@MODE@" 2>/dev/null)" = receive ] && grep -q {NEW} "$CRAFTLATCH_ETC/aws-credentials"; then
      echo 1 > "$CRAFTLATCH_STATE/poll-failures"
    else
      echo 0 > "$CRAFTLATCH_STATE/poll-failures"
    fi ;;
esac
exit 0
"""


@pytest.fixture
def sandbox(tmp_path):
    (tmp_path / "bin").mkdir()
    # env -i 下の偽 aws は環境変数を受け取れないため、ログと失敗モードのパスは埋め込む
    for name, body in (("aws", FAKE_AWS), ("systemctl", FAKE_SYSTEMCTL)):
        p = tmp_path / "bin" / name
        p.write_text(body.replace("@LOG@", str(tmp_path / "calls.log"))
                     .replace("@MODE@", str(tmp_path / "mode")))
        p.chmod(0o755)
    etc, state = tmp_path / "etc", tmp_path / "state"
    etc.mkdir()
    state.mkdir()
    cred = etc / "aws-credentials"
    cred.write_text(f"[default]\naws_access_key_id = {OLD}\naws_secret_access_key = oldsecretvalue\n")
    cred.chmod(0o400)
    (state / "poll-failures").write_text("0\n")
    log = tmp_path / "calls.log"
    log.write_text("")
    env = dict(os.environ)
    env.update(
        AWS_CLI=str(tmp_path / "bin" / "aws"), SYSTEMCTL=str(tmp_path / "bin" / "systemctl"),
        CRAFTLATCH_ETC=str(etc), CRAFTLATCH_STATE=str(state), FAKE_LOG=str(log),
        CRAFTLATCH_VERIFY_TIMEOUT="1", CRAFTLATCH_POLL_WAIT_TIMEOUT="1", CRAFTLATCH_SLEEP="1",
        AWS_ACCESS_KEY_ID="ADMINKEY", AWS_PROFILE="admin",
    )
    return env, etc, state, log


def run(env, *args):
    mode = Path(env["FAKE_LOG"]).with_name("mode")
    mode.write_text(env.get("FAKE_FAIL", ""))
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True,
                          text=True, timeout=60)


def calls(log):
    return log.read_text().splitlines()


def has(log, text):
    return any(text in line for line in calls(log))


def test_script_is_executable():
    assert stat.S_IMODE(SCRIPT.stat().st_mode) == 0o755


def test_success_commits_and_cleans_up(sandbox):
    env, etc, _, log = sandbox
    r = run(env)
    assert r.returncode == 0, r.stderr
    cred = (etc / "aws-credentials").read_text()
    assert NEW in cred and "[default]" in cred and "newsecretvalue" in cred
    assert not (etc / "aws-credentials.new").exists()
    assert not (etc / "aws-credentials.old").exists()
    assert len((etc / "aws-credentials.rotated").read_text().strip()) == 10
    assert has(log, f"update-access-key --user-name craftlatch-host --access-key-id {OLD} --status Inactive")
    assert has(log, f"delete-access-key --user-name craftlatch-host --access-key-id {OLD}")
    assert not has(log, f"delete-access-key --user-name craftlatch-host --access-key-id {NEW}")
    assert has(log, "systemctl stop craftlatch-poll.timer")
    assert has(log, "systemctl start craftlatch-poll.timer")
    assert "newsecretvalue" not in r.stdout + r.stderr


def test_a_verify_failure_keeps_current_credentials(sandbox):
    env, etc, _, log = sandbox
    before = (etc / "aws-credentials").read_text()
    r = run(env | {"FAKE_FAIL": "verify"})
    assert r.returncode == 1
    assert (etc / "aws-credentials").read_text() == before
    assert not (etc / "aws-credentials.new").exists()
    assert has(log, f"delete-access-key --user-name craftlatch-host --access-key-id {NEW}")
    assert not has(log, "systemctl stop craftlatch-poll.timer")


def test_d_verify_runs_with_clean_env_and_new_credentials_file(sandbox):
    env, etc, _, log = sandbox
    assert run(env).returncode == 0
    line = next(c for c in calls(log) if "get-caller-identity" in c)
    assert "KEY= " in line  # 管理者の AWS_ACCESS_KEY_ID を引き継がない
    assert f"CRED={etc}/aws-credentials.new" in line
    assert "PROFILE=default" in line
    assert "ADMINKEY" not in line


def test_b_receive_failure_after_switch_restores_old(sandbox):
    env, etc, state, log = sandbox
    before = (etc / "aws-credentials").read_text()
    r = run(env | {"FAKE_FAIL": "receive"})
    assert r.returncode == 1
    assert (etc / "aws-credentials").read_text() == before
    assert not (etc / "aws-credentials.old").exists()
    assert not (etc / "aws-credentials.new").exists()
    assert has(log, f"delete-access-key --user-name craftlatch-host --access-key-id {NEW}")
    assert not has(log, f"delete-access-key --user-name craftlatch-host --access-key-id {OLD}")
    assert (state / "poll-failures").read_text().strip() == "0"
    assert has(log, "systemctl start craftlatch-poll.timer")


def test_c_old_key_delete_failure_keeps_new_and_finish_completes(sandbox):
    env, etc, _, log = sandbox
    r = run(env | {"FAKE_FAIL": "delete_old"})
    assert r.returncode == 1
    assert NEW in (etc / "aws-credentials").read_text()
    assert not has(log, f"delete-access-key --user-name craftlatch-host --access-key-id {NEW}")
    assert "delete-access-key" in r.stderr
    log.write_text("")
    r = run(env, "--finish", OLD)
    assert r.returncode == 0, r.stderr
    assert has(log, f"delete-access-key --user-name craftlatch-host --access-key-id {OLD}")
    assert not has(log, "create-access-key")
    assert not (etc / "aws-credentials.old").exists()


def test_finish_treats_nosuchentity_as_done(sandbox):
    env, etc, _, _ = sandbox
    (etc / "aws-credentials").chmod(0o600)
    (etc / "aws-credentials").write_text(f"[default]\naws_access_key_id = {NEW}\n")
    r = run(env | {"FAKE_FAIL": "nosuch_old"}, "--finish", OLD)
    assert r.returncode == 0, r.stderr


def test_finish_rejects_bad_key_id(sandbox):
    env, _, _, log = sandbox
    r = run(env, "--finish", "bad id;rm")
    assert r.returncode != 0
    assert calls(log) == []


def test_poll_wait_timeout_recovers_before_switch(sandbox):
    env, etc, _, log = sandbox
    before = (etc / "aws-credentials").read_text()
    r = run(env | {"FAKE_ACTIVE": "active"})
    assert r.returncode == 1
    assert (etc / "aws-credentials").read_text() == before
    assert has(log, f"delete-access-key --user-name craftlatch-host --access-key-id {NEW}")
    assert has(log, "systemctl start craftlatch-poll.timer")
    assert not has(log, "start craftlatch-poll.service")


def test_b2_old_key_also_failing_warns_but_restarts_timer_and_deletes_new(sandbox):
    env, etc, _, log = sandbox
    before = (etc / "aws-credentials").read_text()
    r = run(env | {"FAKE_FAIL": "receive_both"})
    assert r.returncode == 1
    assert (etc / "aws-credentials").read_text() == before
    assert "旧キーでも受信に失敗" in r.stderr
    assert has(log, "systemctl start craftlatch-poll.timer")
    assert has(log, f"delete-access-key --user-name craftlatch-host --access-key-id {NEW}")


def test_finish_refuses_the_active_key_before_any_iam_call(sandbox):
    env, _, _, log = sandbox
    r = run(env, "--finish", OLD)  # OLD は現行の aws-credentials のキー
    assert r.returncode == 1
    assert "使用中" in r.stderr
    assert not has(log, "aws iam")
