# CraftLatch 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cognito 認証に成功したグローバル IPv4 からのみ Minecraft（TCP/25565・UDP/19132）へ到達でき、認証で自動起動・15 分無人で自動停止する仕組みを、AWS（Terraform/GitHub Actions）と自宅 Ubuntu（systemd/nftables）に実装する。

**Architecture:** ブラウザ → API Gateway HTTP API（IPv4）→ Lambda（OIDC Code+PKCE）→ SQS。Ubuntu の非特権 poll が SQS を毎分受信して要求ファイルを置き、path unit が root の apply を起動して nftables の set 登録と `minecraft.service` の起動・停止を行う。idle は SLP で人数を取得し stop 要求を置く。

**Tech Stack:** Python 3.12（ホスト）/ 3.13（Lambda）標準ライブラリ + boto3、pytest（uv で実行）、systemd 255、nftables 1.0.9、Terraform ≥1.10（AWS・archive プロバイダー）、GitHub Actions（OIDC）。

**Spec:** `docs/superpowers/specs/2026-11-01-craftlatch-design.md`（以下「設計書」。章番号は設計書の章）

## Global Constraints

- 命名: 接頭辞 `craftlatch-`、AWS タグ `Project=CraftLatch`。nft は `table inet craftlatch`・set `allow4`。Linux ユーザーは `minecraft`・`craftlatch`（設計書 3.2）。
- 公開リポジトリ: アカウント ID・メールアドレス・API/キュー URL・Cognito ドメインをリポジトリにもログにも書かない（設計書 4.6・4.7）。
- 秘密情報（アクセスキー・Discord Webhook URL）は Terraform state にもリポジトリにも置かない。
- ホストの Python は `python3 -E -s` で起動（`-I` 禁止。同じディレクトリの `common.py` を import するため）。ホストの依存は標準ライブラリと apt の `python3-boto3` のみ。コードは Python 3.12 で動くこと。
- Lambda は標準ライブラリ + ランタイム同梱 boto3 のみ（外部パッケージ禁止）。Python 3.13・arm64・128MB・10 秒。
- 値: 許可期限 600 秒（`timeout 10m`）、無人停止 900 秒、stop 要求の有効期間 120 秒・再要求間隔 300 秒、`MAX_AUTH_AGE=120`、要求ファイル 1024 バイト以下、apply 1 回最大 100 件、SLP タイムアウト 3 秒・応答上限 65,536 バイト、poll 全体期限 35 秒、キー警告 335 日（設計書 5 章・4.3）。
- ポート: Java TCP/25565、Bedrock UDP/19132。信頼する送信元は `enp3s0`+`192.168.33.0/24`（ルーター MAC 除外）と `wg0`+`10.0.0.0/24`。
- 外部コマンドは `subprocess.run([...], shell=False, timeout=10)`。CLI の例示は 1 行で書く。
- GitHub Actions の外部 Action はコミット SHA で固定。Terraform 本体・プロバイダーはバージョン固定し `.terraform.lock.hcl` をコミット。
- コミットは各タスクの最後に行い、メッセージ末尾に `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` を付ける。

## Review Focus

1. **同じブラウザで 2 つのタブからログインした場合**（Cookie の state が後勝ちで上書きされる）: 先に戻ったタブは「期限切れ・再ログイン」ページになり、例外や 500 にならないこと → Task 9 の `test_callback_state_mismatch_returns_relogin_page`。
2. **`/etc/craftlatch/config.json` の欠落・破損**: poll・apply・idle は例外を握り潰さず終了コード 1（通知）、fwcheck は終了コード 1（Minecraft を起動させない）こと → Task 2 の `test_load_config_rejects_missing_and_invalid`、Task 6 の `test_main_fails_closed_on_bad_config`。
3. **poll が書き込み途中で異常終了して残った `.tmp-*` ファイル**: apply は `.req` 以外を処理しないが、60 秒より古い `.tmp-*` は削除してたまり続けないこと → Task 4 の `test_collect_removes_stale_tmp_files`。
4. **起動途中の Paper が返す不完全・異常な SLP 応答**（途中切断、長さ VarInt が不正、JSON でない）: idle は例外で落ちず「SLP 失敗＝0 人扱い」になること → Task 5 の `test_slp_truncated_and_garbage_responses_raise_slperror`。
5. **Discord Webhook の障害（429・5xx・接続不可）**: notify は終了コード 1 を返し、送信履歴を更新しない（次の失敗時に再送を試みる）こと → Task 7 の `test_send_failure_does_not_record_and_returns_1`。

---

## ファイル構成

| パス | 責務 |
|---|---|
| `pyproject.toml` | uv 用のプロジェクト定義（dev 依存 `pytest`・`boto3`、pytest の `pythonpath`） |
| `host/lib/common.py` | 設定読み込み、厳格な JSON 解析、allow/stop の検証、要求ファイルの原子的書き込み |
| `host/lib/poll.py` | SQS 受信 → 検証 → 要求ファイル配置 → 削除、失敗カウンタ、キー期限警告 |
| `host/lib/apply.py` | 要求ファイルの安全な読み取り・検証・隔離、nft バッチ、状態（`last-allow`・`seen.json`）、systemctl |
| `host/lib/idle.py` | SLP クライアント、無人判定（純粋関数）、stop 要求の配置 |
| `host/lib/notify.py` | Discord 通知と 60 分抑止 |
| `host/lib/nftgen.py` | `config.json` から nft ルールファイルを生成 |
| `host/lib/fwcheck.py` + `host/lib/fwcheck` | 稼働中のテーブルと期待構造の完全一致検証（`fwcheck` は起動用の 3 行スクリプト） |
| `host/lib/routermac.py` | デフォルトゲートウェイの MAC 検出と `config.json` への補完 |
| `host/lib/mc-stop` | FIFO に `stop` を書き終了を待つシェルスクリプト |
| `host/systemd/*` | 設計書 5.4〜5.7・6.2・7.1・7.2 のユニット |
| `host/tmpfiles.d/craftlatch.conf`、`host/config.json.example` | tmpfiles 定義、設定の雛形 |
| `host/install.sh`、`host/scripts/craftlatch-rotate-key` | 配置・事前検証・更新案内、キーローテーション |
| `src/lambda/craftlatch_auth/{handler,oidc,cookies,pages,settings}.py` | Lambda（ルーティング、OIDC、Cookie、HTML、環境変数） |
| `infra/bootstrap/*.tf` | OIDC プロバイダー、GHA ロール 2 つ、Permissions Boundary |
| `infra/terraform/*.tf` | Cognito、API、Lambda、SQS、IAM |
| `.github/workflows/{ci,terraform-plan,terraform-apply}.yml`、`.github/scripts/tf-summary.sh` | CI、plan、apply、plan 要約 |
| `tests/host/*`、`tests/lambda/*` | 単体テスト |
| `README.md`、`CLAUDE.md`、`docs/{setup,operations,user-guide}.md` | ドキュメント（設計書 12 章） |

---

## フェーズ 0: 事前検証

### Task 0: 本番サーバーでの事前検証（手動・管理者の sudo が必要）

設計書 13 章の U1・U3・U6 と、fwcheck のテスト用フィクスチャの取得。サーバーへは `ssh -p 3340 -i /workspace/.ssh/id_ed25519 agent@192.168.33.4` で接続する。**sudo を伴うコマンドは管理者に実行を依頼し、結果を受け取る**（エージェントは sudo のパスワードを持たない）。既存の Minecraft には触れない。

**Files:**
- Create: `docs/superpowers/plans/2026-11-01-craftlatch-spike.md`（結果の記録）
- Create: `host/lib/fwcheck-expected.json`、`tests/host/fixtures/craftlatch-probe.nft`、`tests/host/fixtures/craftlatch-probe-bad.nft`

**どの確認も期待どおりでなければ、その時点で作業を止め、結果を管理者に報告して計画（関連するすべてのタスク）を改訂してから再開する。** 分岐のまま実装を続けない。

- [ ] **Step 1: 検証用のルールファイルを用意する** — 設計書 6.1 のルール全文（`<ルーターのMAC>` は `aa:bb:cc:dd:ee:ff`）を `tests/host/fixtures/craftlatch-probe.nft` としてリポジトリに作る（Task 6 で生成器の出力と一致することを検査する）。同じ内容で `iif "lo" accept` の直後に `tcp dport 25565 accept` を 1 行挿入した `tests/host/fixtures/craftlatch-probe-bad.nft` も作る（Task 15 の fwcheck 試験用）。両方をサーバーの再起動でも消えない `~/craftlatch-probe/` に置く（`ssh -p 3340 -i /workspace/.ssh/id_ed25519 agent@192.168.33.4 mkdir -p craftlatch-probe && scp -P 3340 -i /workspace/.ssh/id_ed25519 tests/host/fixtures/craftlatch-probe*.nft agent@192.168.33.4:craftlatch-probe/`）。

- [ ] **Step 2: U1 — `ipv6.disable=1` で `table inet` を読み込めるか（別のネットワーク名前空間で実施し、本番のルールに影響させない）**

管理者に依頼: `sudo unshare --net sh -c 'nft -f /home/agent/craftlatch-probe/craftlatch-probe.nft && nft -j list table inet craftlatch' > /home/agent/craftlatch-probe/nft_list_table.json; echo rc=$?`

Expected: `rc=0` で、`/home/agent/craftlatch-probe/nft_list_table.json` に set `allow4` とチェイン `input` の全ルールが含まれる。失敗した場合は作業を止める（`table ip` への変更は、`nftgen`・`apply` の nft バッチ・`fwcheck`・各種確認コマンドに及ぶため、計画の改訂が必要）。

- [ ] **Step 3: Step 2 の JSON を `host/lib/fwcheck-expected.json` として取得する**（`scp -P 3340 -i /workspace/.ssh/id_ed25519 agent@192.168.33.4:craftlatch-probe/nft_list_table.json host/lib/fwcheck-expected.json`。完全なルールセットの、nftables 1.0.9 による出力であることを確認する）

- [ ] **Step 4: U6 — Paper が FIFO の stdin からコマンドを受け付けるか（プラグインなしの一時サーバーで確認。jar は agent ユーザーでも読めるため sudo 不要）**

Run（サーバー上、agent ユーザー、1 行）: `D=$(mktemp -d /tmp/craftlatch-probe.XXXXXX) && cd "$D" && printf 'eula=true\n' > eula.txt && printf 'server-ip=127.0.0.1\nserver-port=25599\nonline-mode=true\n' > server.properties && mkfifo console && (java -Xmx512M -jar /media/ssd/paper/paper-1.21.7-17.jar --nogui <> console > probe.log 2>&1 & echo $! > pid) && timeout 300 sh -c 'until grep -q "Done (" probe.log; do sleep 5; done' && timeout 5 sh -c 'printf "stop\n" > console' && timeout 120 sh -c 'while kill -0 "$(cat pid)" 2>/dev/null; do sleep 2; done'; grep -E "Stopping server|Saving worlds" probe.log; kill "$(cat pid)" 2>/dev/null; cd / && rm -rf "$D"`

`server-ip=127.0.0.1` で localhost のみに待ち受け、外部に公開しない。`<>` で FIFO を読み書き両用で開くため、書き手がいなくてもブロックせず EOF にもならない（systemd のソケットと同じ状態）。起動待ち 300 秒・終了待ち 120 秒の上限を設け、最後に必ずプロセスを止めて作業ディレクトリを削除する。プラグイン込みの実構成での FIFO 停止は Task 15 Step 4 で別に確認する。

Expected: `Stopping server` と `Saving worlds` が出力される。

- [ ] **Step 5: U3 — 停止ジョブ実行中に `systemctl start --no-block` した場合の挙動（ダミーユニットで確認）**

管理者に依頼（各 1 行）: `printf '[Service]\nExecStart=/bin/sleep infinity\nExecStop=/bin/sleep 10\n' | sudo tee /etc/systemd/system/craftlatch-stopprobe.service` → `sudo systemctl daemon-reload && sudo systemctl start craftlatch-stopprobe` → `sudo systemctl stop --no-block craftlatch-stopprobe && sleep 1 && sudo systemctl start --no-block craftlatch-stopprobe && sleep 15 && systemctl is-active craftlatch-stopprobe` → 片付け `sudo systemctl stop craftlatch-stopprobe; sudo rm /etc/systemd/system/craftlatch-stopprobe.service; sudo systemctl daemon-reload`

Expected: `active`（停止完了後に再起動された）。`inactive` の場合は作業を止める（apply の待機方式・ユニットのタイムアウト・待機後の要求期限の再検証を一体で設計し直す必要があるため）。

- [ ] **Step 6: 結果を `docs/superpowers/plans/2026-11-01-craftlatch-spike.md` に記録し、コミットする**

```bash
git add docs/superpowers/plans/2026-11-01-craftlatch-spike.md host/lib/fwcheck-expected.json tests/host/fixtures/craftlatch-probe.nft tests/host/fixtures/craftlatch-probe-bad.nft
git commit -m "docs: 事前検証（U1・U3・U6）の結果を記録"
```

---

## フェーズ 1: ホスト側コード（ローカルで TDD）

### Task 1: プロジェクトの土台

**Files:**
- Create: `pyproject.toml`、`tests/conftest.py`、`tests/host/__init__.py`（空）、`tests/lambda/__init__.py`（空）
- Modify: `.gitignore`（`.venv/`、`__pycache__/`、`.pytest_cache/`、`*.zip`、`.terraform/`、`*.tfstate*`、`tfplan*` を追加）

**Interfaces:**
- Produces: `uv run pytest` で `host/lib` と `src/lambda` を import できる環境

- [ ] **Step 1: `pyproject.toml` を作成する** — `[project] name="craftlatch" requires-python=">=3.12"`、`[dependency-groups] dev=["pytest>=8", "boto3"]`、`[tool.pytest.ini_options] pythonpath=["host/lib", "src/lambda"] testpaths=["tests"]`。`[tool.uv] package=false`。

- [ ] **Step 2: `uv python install 3.12 && uv sync` を実行する**

Expected: `.venv` が作成される。

- [ ] **Step 3: `uv run pytest` を実行する**

Expected: `no tests ran`（終了コード 5）で、import エラーがない。

- [ ] **Step 4: コミット**

```bash
git add pyproject.toml uv.lock .gitignore tests/
git commit -m "chore: uv と pytest のプロジェクト土台を追加"
```

### Task 2: `common.py`（設定・検証・要求ファイル）

**Files:**
- Create: `host/lib/common.py`、`host/config.json.example`
- Test: `tests/host/test_common.py`

**Interfaces:**
- Produces:
  - `class ValidationError(ValueError)`
  - `@dataclass(frozen=True) class TrustedSource: iface: str; cidr: str; exclude_ether_saddr: str | None`
  - `@dataclass(frozen=True) class Config: region: str; queue_url: str; java_port: int; bedrock_port: int; grant_timeout_sec: int; idle_stop_sec: int; slp_timeout_sec: int; key_warn_days: int; trusted_sources: tuple[TrustedSource, ...]`
  - `load_config(path: Path = Path("/etc/craftlatch/config.json")) -> Config`（欠落・不正は `ValidationError`）
  - `parse_json_strict(data: bytes, max_bytes: int = 1024) -> dict`
  - `@dataclass(frozen=True) class AllowGrant: ip: str; iat: int; exp: int; id: str; user: str`
  - `@dataclass(frozen=True) class StopRequest: reason: str; invocation: str; iat: int; exp: int`
  - `validate_sqs_message(obj: dict, now: int) -> AllowGrant`（キー `v,type,ip,iat,exp,id,user`）
  - `parse_request(data: bytes, now: int) -> AllowGrant | StopRequest`（キー `v,action,...`）
  - `allow_to_request(g: AllowGrant) -> dict`、`stop_to_request(s: StopRequest) -> dict`
  - `write_request(directory: Path, payload: dict, now: int) -> Path`（`.tmp-<uuid>` → `fsync` → `rename` で `<now>-<uuid>.req`、0644）
  - `atomic_write_text(path: Path, text: str, mode: int) -> None`
  - 定数 `REQUESTS_DIR = Path("/run/craftlatch/requests")`、`STATE_DIR = Path("/run/craftlatch/state")`、`QUARANTINE_DIR = Path("/run/craftlatch/quarantine")`

- [ ] **Step 1: 失敗するテストを書く**（`now = 1793460000` で固定）

```python
def test_valid_sqs_message():
    g = validate_sqs_message({"v":1,"type":"allow","ip":"8.8.8.8","iat":now,"exp":now+600,"id":U,"user":"alice"}, now)
    assert g == AllowGrant("8.8.8.8", now, now+600, U, "alice")

@pytest.mark.parametrize("patch", [
    {"v": True}, {"v": 2}, {"iat": True}, {"exp": now + 601}, {"exp": now},          # bool・TTL超過・期限切れ
    {"iat": now + 61}, {"ip": "10.0.0.5"}, {"ip": "::1"}, {"ip": "203.000.113.5"},    # 未来・非グローバル・IPv6・非正規形
    {"id": U.upper()}, {"user": "アリス"}, {"user": "a"*129}, {"extra": 1},            # 非正規 UUID・規則外・未知キー
])
def test_invalid_sqs_message_rejected(patch): ...   # ValidationError

def test_parse_json_strict_rejects_nan_size_and_non_object():
    for data in (b'{"a": NaN}', b'[1]', b'{' + b' '*1024 + b'}'):
        with pytest.raises(ValidationError): parse_json_strict(data)

def test_stop_request_ttl_limit_120():
    ok = {"v":1,"action":"stop","reason":"idle","invocation":"a"*32,"iat":now,"exp":now+120}
    assert isinstance(parse_request(json.dumps(ok).encode(), now), StopRequest)
    with pytest.raises(ValidationError): parse_request(json.dumps({**ok, "exp": now+121}).encode(), now)
    with pytest.raises(ValidationError): parse_request(json.dumps({**ok, "reason": "manual"}).encode(), now)

def test_write_request_is_atomic_and_0644(tmp_path):
    p = write_request(tmp_path, allow_to_request(g), now)
    assert p.name.startswith(f"{now}-") and p.suffix == ".req"
    assert oct(p.stat().st_mode & 0o777) == "0o644"
    assert not list(tmp_path.glob(".tmp-*"))

def test_load_config_rejects_missing_and_invalid(tmp_path):   # Review Focus 2
    with pytest.raises(ValidationError): load_config(tmp_path / "none.json")
    (tmp_path / "c.json").write_text('{"region": "ap-northeast-1"}')
    with pytest.raises(ValidationError): load_config(tmp_path / "c.json")
    # 雛形（host/config.json.example）そのまま = queue_url・MAC がプレースホルダー "<...>" → ValidationError
    with pytest.raises(ValidationError): load_config(Path("host/config.json.example"))
```

- [ ] **Step 2: `uv run pytest tests/host/test_common.py -v` を実行し、import エラーで失敗することを確認する**

- [ ] **Step 3: `host/lib/common.py` を実装する** — 検証規則は設計書 5.3 の表のとおり。`json.loads(..., parse_constant=...)` で `NaN`・`Infinity` を拒否し、数値は `type(x) is int`。IP は `ipaddress.IPv4Address(ip)` かつ `is_global` かつ `str(...) == ip`。`host/config.json.example` は設計書 5.2 の JSON（`queue_url` と MAC はプレースホルダー）。

- [ ] **Step 4: `uv run pytest tests/host/test_common.py -v` を実行し、すべて PASS することを確認する**

- [ ] **Step 5: コミット**

```bash
git add host/lib/common.py host/config.json.example tests/host/test_common.py
git commit -m "feat(host): 設定読み込みと要求の検証を追加"
```

### Task 3: `poll.py`（SQS 受信）

**Files:**
- Create: `host/lib/poll.py`
- Test: `tests/host/test_poll.py`

**Interfaces:**
- Consumes: Task 2 の `load_config`、`validate_sqs_message`、`allow_to_request`、`write_request`、`parse_json_strict`
- Produces:
  - `make_sqs_client(region: str)`（`botocore.config.Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 1})`）
  - `@dataclass class PollResult: received: int; written: int; deleted: int; invalid: int; failure: str | None`（`failure` は AWS 例外の種類名、または全体期限超過の `"deadline"`。どちらも失敗カウンタを加算する）
  - `run_poll(sqs, queue_url: str, requests_dir: Path, now_fn: Callable[[], int], monotonic_fn: Callable[[], float], deadline_s: float = 35.0) -> PollResult`
  - `update_failures(state_dir: Path, failure: str | None) -> int`（成功で 0 に戻し、失敗で加算した回数を返す。ファイル `poll-failures`）
  - `key_age_warning(rotated_file: Path, today: date, warn_days: int) -> str | None`
  - `main(argv: list[str] | None = None) -> int`（失敗回数 ≥5 で 1、それ以外 0。設定不正は 1）

- [ ] **Step 1: 失敗するテストを書く**（SQS は `botocore.stub.Stubber` または `receive_message`・`delete_message` を持つ偽オブジェクトで置き換える）

```python
def test_valid_message_written_then_deleted(tmp_path): ...
    # receive 1 件（有効）→ 要求ファイル 1 件、delete が ReceiptHandle で 1 回呼ばれ、順序は「書き込み → 削除」
def test_invalid_message_deleted_without_file(tmp_path): ...       # invalid == 1、ファイル 0 件、delete 1 回
def test_write_failure_keeps_message(tmp_path): ...                # 書き込み不可ディレクトリ → delete 0 回
def test_receive_loops_max_5_times(tmp_path): ...                  # 毎回 10 件返す偽 SQS → receive 呼び出しは 5 回
def test_deadline_stops_new_calls_and_counts_as_failure(tmp_path):
    # monotonic_fn は呼び出し順に 0.0（起動時）→ 10.0 → 36.0 を返す。偽 SQS は毎回 10 件返す
    # → 36.0 を見た後は receive/delete を呼ばず、result.failure == "deadline"、main 経由で poll-failures が 1 加算される
def test_receive_args(tmp_path): ...                               # MaxNumberOfMessages=10, WaitTimeSeconds=1
def test_failures_counter_and_exit_code(tmp_path):
    for i in range(1, 6): assert update_failures(tmp_path, "EndpointConnectionError") == i
    assert update_failures(tmp_path, None) == 0
def test_key_age_warning():
    assert key_age_warning(f_with("2025-11-01"), date(2026, 10, 1), 335) is None   # 334 日
    assert "335" in key_age_warning(f_with("2025-11-01"), date(2026, 10, 2), 335)
```

- [ ] **Step 2: `uv run pytest tests/host/test_poll.py -v` を実行し、失敗することを確認する**

- [ ] **Step 3: `host/lib/poll.py` を実装する** — 本文は 1024 バイト超なら無効扱い。AWS 例外（`botocore.exceptions.BotoCoreError`・`ClientError`）は `failure` に種類名を、期限超過は `"deadline"` を入れて打ち切る。`main` は `--import-check` を受け取ったら `import-ok` を出力して 0 を返す（Task 8 の起動確認用。poll 以外の各スクリプトの `main` も同じ）。ログは `print("event=poll received=.. written=.. deleted=.. invalid=.. result=ok|error", flush=True)` の key=value 1 行。`main` はキー期限警告 → `run_poll` → `update_failures` → 終了コードの順。

- [ ] **Step 4: `uv run pytest tests/host/test_poll.py -v` を実行し、PASS を確認する**

- [ ] **Step 5: コミット**

```bash
git add host/lib/poll.py tests/host/test_poll.py
git commit -m "feat(host): SQS を受信して要求ファイルを置く poll を追加"
```

### Task 4: `apply.py`（root での適用）

**Files:**
- Create: `host/lib/apply.py`
- Test: `tests/host/test_apply.py`

**Interfaces:**
- Consumes: Task 2 の `parse_request`、`AllowGrant`、`StopRequest`、`atomic_write_text`、ディレクトリ定数
- Produces:
  - `Runner = Callable[[list[str], str | None], subprocess.CompletedProcess]`（第 2 引数は stdin 文字列）
  - `read_request_file(path: Path, expected_uid: int) -> bytes`（`O_RDONLY|O_NOFOLLOW|O_NONBLOCK`、`fstat` で通常ファイル・uid・≤1024、最大 1025 バイト読んで再確認。不正は `ValidationError`）
  - `collect(requests_dir: Path, quarantine_dir: Path, expected_uid: int, now: int) -> tuple[list[tuple[Path, AllowGrant]], list[tuple[Path, StopRequest]], bool]`（第 3 要素は「隔離・削除に失敗した」。最大 100 件。60 秒より古い `.tmp-*` は削除）
  - `build_nft_batch(ips: list[str]) -> str`
  - `class StateStore(state_dir: Path)`: `last_allow() -> int`、`set_last_allow(now: int)`、`seen() -> dict[str, int]`、`mark_seen(ids_exp: dict[str, int], now: int)`（期限切れを除去）
  - `class Systemd(runner: Runner)`: `start_no_block() -> bool`、`stop_no_block() -> bool`、`invocation_id() -> str`、`active_state() -> str`
  - `apply_batch(allows, stops, store: StateStore, systemd: Systemd, runner: Runner, now: int) -> bool`（失敗があれば False）
  - `main() -> int`

- [ ] **Step 1: 失敗するテストを書く**（`runner` は呼び出しを記録する偽物。uid は `os.getuid()` を使う）

```python
def test_fifo_does_not_block(tmp_path):
    os.mkfifo(tmp_path / "x.req")
    with pytest.raises(ValidationError): read_request_file(tmp_path / "x.req", os.getuid())  # 即座に返る
def test_symlink_and_oversize_and_wrong_owner_quarantined(tmp_path): ...
def test_collect_removes_stale_tmp_files(tmp_path):            # Review Focus 3
    old = tmp_path / "req" / ".tmp-a"; ...; os.utime(old, (now-61, now-61))
    collect(...); assert not old.exists() and fresh_tmp.exists()
def test_bad_file_does_not_stop_others(tmp_path): ...          # 不正 1 件 + 正常 1 件 → 正常分は返る
def test_build_nft_batch():
    assert build_nft_batch(["8.8.8.8"]) == (
        "add element inet craftlatch allow4 { 8.8.8.8 }\n"
        "delete element inet craftlatch allow4 { 8.8.8.8 }\n"
        "add element inet craftlatch allow4 { 8.8.8.8 timeout 10m }\n")
def test_allow_order_and_seen_recorded_last(tmp_path):
    # 呼び出し順: nft -f -（stdin=バッチ）→ last-allow 更新 → systemctl start --no-block minecraft.service → seen 記録
def test_duplicate_id_skipped(tmp_path): ...                   # seen にある id は nft も start も呼ばない
def test_allow_wins_over_stop_in_same_batch(tmp_path): ...     # stop は呼ばれない
@pytest.mark.parametrize("case", ["expired", "invocation_mismatch", "iat_not_after_last_allow", "inactive"])
def test_stale_stop_discarded(tmp_path, case): ...             # systemctl stop を呼ばない
def test_valid_stop_calls_stop_no_block(tmp_path): ...
def test_partial_success_keeps_allow_returns_false(tmp_path): ...  # nft 成功・start 失敗 → False、nft の取り消しはしない
@pytest.mark.parametrize("crash_at", ["before_nft", "after_nft", "after_start"])
def test_crash_recovery_is_at_least_once(tmp_path, crash_at):
    # runner／StateStore に例外を注入して apply を中断 → 要求ファイルは残る → 2 回目の apply で
    # before_nft: nft 1 回・start 1 回 / after_nft: nft 再適用・start 1 回 / after_start: nft・start がもう 1 回ずつ（設計書 5.5 の表）
    # いずれも 2 回目の終了後に seen に id があり、要求ファイルが消えている
def test_poll_to_apply_integration(tmp_path):
    # Task 3 の run_poll（偽 SQS、有効メッセージ 1 件）で要求ファイルを作り、そのディレクトリに対して collect → apply_batch
    # → nft バッチに IP が入り、systemctl start が呼ばれる
```

- [ ] **Step 2: `uv run pytest tests/host/test_apply.py -v` を実行し、失敗することを確認する**

- [ ] **Step 3: `host/lib/apply.py` を実装する** — `systemctl` は `["systemctl", "start", "--no-block", "minecraft.service"]`、`["systemctl", "show", "-p", "InvocationID", "--value", "minecraft.service"]`、`["systemctl", "show", "-p", "ActiveState", "--value", "minecraft.service"]`。nft は `["nft", "-f", "-"]` に stdin でバッチを渡す。`main` は `pwd.getpwnam("craftlatch").pw_uid` を使い、隔離・削除の失敗か `apply_batch` の失敗で終了コード 1。停止ジョブ中の起動は systemd が停止完了後に処理する前提（Task 0 Step 5 で確認済みであること）。ログは設計書 5.5 の形式。

- [ ] **Step 4: `uv run pytest tests/host/test_apply.py -v` を実行し、PASS を確認する**

- [ ] **Step 5: コミット**

```bash
git add host/lib/apply.py tests/host/test_apply.py
git commit -m "feat(host): 要求を検証して nft と systemd に適用する apply を追加"
```

### Task 5: `idle.py`（無人検知）

**Files:**
- Create: `host/lib/idle.py`
- Test: `tests/host/test_idle.py`

**Interfaces:**
- Consumes: Task 2 の `StopRequest`、`stop_to_request`、`write_request`、`atomic_write_text`、`load_config`
- Produces:
  - `class SlpError(Exception)`
  - `slp_player_count(host: str, port: int, timeout: float, max_bytes: int = 65536) -> int`
  - `@dataclass class IdleState: invocation: str; empty_since: int | None; probe_ok_since_empty: bool; last_stop_request: int | None`
  - `decide(state: IdleState, now: int, players: int | None, invocation: str, last_allow: int, idle_stop_sec: int = 900, rerequest_sec: int = 300) -> tuple[IdleState, StopRequest | None]`（`players=None` は SLP 失敗）
  - `main() -> int`

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_slp_against_local_fake_server(): ...                   # socket で Status 応答 {"players":{"online":2}} → 2
def test_slp_truncated_and_garbage_responses_raise_slperror():   # Review Focus 4
    # 途中切断 / VarInt が 5 バイト超 / 長さ 65537 / JSON でない / online が true / online が -1 → SlpError
def test_slp_timeout_raises(): ...                               # 応答しないサーバー → 3 秒以内に SlpError
def test_reset_on_new_invocation():
    s, _ = decide(IdleState("old", 100, True, 200), now, 0, "new", 0); assert s.empty_since == now
def test_stop_after_900s_with_reason_idle():
    _, req = decide(IdleState("i", now-900, True, None), now, 0, "i", 0)
    assert req == StopRequest("idle", "i", now, now+120)
def test_probe_failure_reason_when_never_ok(): ...               # probe_ok_since_empty False → reason "probe-failure"
def test_last_allow_defers_stop():
    _, req = decide(IdleState("i", now-1000, True, None), now, 0, "i", last_allow=now-60); assert req is None
def test_rerequest_after_300s():
    _, a = decide(IdleState("i", now-2000, True, now-299), now, 0, "i", 0); assert a is None
    _, b = decide(IdleState("i", now-2000, True, now-300), now, 0, "i", 0); assert b is not None
def test_players_reset_all():
    s, _ = decide(IdleState("i", now-500, True, now-10), now, 1, "i", 0)
    assert (s.empty_since, s.probe_ok_since_empty, s.last_stop_request) == (None, False, None)
```

- [ ] **Step 2: `uv run pytest tests/host/test_idle.py -v` を実行し、失敗することを確認する**

- [ ] **Step 3: `host/lib/idle.py` を実装する** — SLP は Handshake（パケット ID 0x00、プロトコル番号 VarInt 767、ホスト文字列、ポート、next state 1）→ Status Request（0x00）→ 応答の長さ VarInt・パケット ID・文字列長 VarInt・JSON。`json.loads(..., parse_constant=...)` で `NaN` を拒否し、`players.online` は `type(x) is int and 0 <= x <= 10000`。`main` は `systemctl show -p InvocationID --value minecraft.service`、`/run/craftlatch/state/last-allow`（無ければ 0）、状態ファイル `/var/lib/craftlatch/idle.json` を使う。

- [ ] **Step 4: `uv run pytest tests/host/test_idle.py -v` を実行し、PASS を確認する**

- [ ] **Step 5: コミット**

```bash
git add host/lib/idle.py tests/host/test_idle.py
git commit -m "feat(host): SLP による無人検知と stop 要求を追加"
```

### Task 6: `nftgen.py`・`fwcheck.py`・`routermac.py`

**Files:**
- Create: `host/lib/nftgen.py`、`host/lib/fwcheck.py`、`host/lib/fwcheck`（`#!/usr/bin/python3 -Es` + `import fwcheck, sys; sys.exit(fwcheck.main())`、0755）、`host/lib/routermac.py`
- Test: `tests/host/test_nftgen.py`、`tests/host/test_fwcheck.py`、`tests/host/test_routermac.py`

**Interfaces:**
- Consumes: Task 2 の `Config`、`TrustedSource`、`load_config`、`atomic_write_text`；Task 0 の `host/lib/fwcheck-expected.json`（テストの `fixture_json` はこのファイルを読む）と `tests/host/fixtures/craftlatch-probe.nft`
- Produces:
  - `render_ruleset(cfg: Config) -> str`（設計書 6.1 の全文、`table inet craftlatch`）
  - `nftgen.main(argv: list[str] | None = None) -> int`（`nftgen.py --output <path>`。`load_config()` で完成した設定を検証し、ルールを `<path>.tmp` に書いてから `rename` で置き換える。検証失敗時は既存ファイルに触れず 1 を返す）
  - `normalize(nft_json: dict) -> list`（`handle`・カウンタ値・set の要素・`expires` を除去し、メタ情報 `metainfo` を除く）
  - `expected_json(cfg: Config, template_path: Path = Path("/usr/local/lib/craftlatch/fwcheck-expected.json")) -> dict`（**実行時に名前空間を作らない**。`minecraft.service` の `RestrictNamespaces=yes` と衝突させないため。Task 0 で実機から取得したフィクスチャ JSON を雛形として `install.sh` が配置し、その中のルーター MAC `aa:bb:cc:dd:ee:ff` を `cfg` の `exclude_ether_saddr` に置き換えたものを期待構造とする。`cfg.trusted_sources` の iface・cidr の並びが雛形と一致しない場合は `ValidationError`（`trusted_sources` を変えたら雛形を取り直す。手順は `docs/operations.md`）。fwcheck が実行する外部コマンドは `nft -j list table inet craftlatch` だけ）
  - `REQUIRED_RULE_SHAPES`（生成器と独立した最低限の検査。正規化後の live に、`lo` の accept、TCP/25565 と UDP/19132 の drop が末尾 2 本にあること、`allow4` が `dynamic`・`timeout` フラグを持つことを確認する。生成器と期待値が同じ誤りを共有しても検出できるようにする）
  - `fwcheck.main(argv: list[str] | None = None) -> int`（live と expected の `normalize` が一致すれば 0、不一致・設定不正・コマンド失敗は 1）
  - `default_gateway(ip_route_output: str) -> str`、`neigh_mac(ip_neigh_output: str) -> str | None`（`REACHABLE`/`STALE`/`DELAY`/`PERMANENT` のみ。`INCOMPLETE`/`FAILED` は None）、`set_router_mac(config_path: Path, mac: str, overwrite: bool) -> bool`（`load_config` を使わず JSON を直接読み書きする。補完前は他の項目が未入力でもよいため。`atomic_write_text` で 0644 書き込み）、`routermac.main(argv) -> int`（`--yes`、`--refresh`）

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_render_contains_rules_in_order(cfg):
    lines = [l.strip() for l in render_ruleset(cfg).splitlines()]
    i = lines.index('iif "lo" accept')
    assert lines[i+1] == 'iifname "enp3s0" ether saddr != aa:bb:cc:dd:ee:ff ip saddr 192.168.33.0/24 meta l4proto { tcp, udp } th dport { 25565, 19132 } counter accept'
    assert lines[i+2].startswith('iifname "wg0" ip saddr 10.0.0.0/24')
    assert lines[-4:-2] == ["tcp dport 25565 counter drop", "udp dport 19132 counter drop"]
def test_render_rejects_bad_iface_or_mac(cfg): ...      # iface に空白・引用符、MAC 形式違い → ValidationError
def test_normalize_strips_volatile(fixture_json): ...   # handle・packets・bytes・elem が消える
def test_fixture_matches_itself(fixture_json): ...      # normalize(fixture) 同士は一致、REQUIRED_RULE_SHAPES も満たす
def test_generator_matches_probe_source():
    # MAC=aa:bb:cc:dd:ee:ff の設定で render_ruleset した結果が tests/host/fixtures/craftlatch-probe.nft と
    # （空白の差を除いて）一致すること。実機 JSON（nft_list_table.json）はこのファイルを読み込んだ出力なので、
    # 生成器 → 実機の期待構造までが一本につながる
@pytest.mark.parametrize("mutation", ["prepend_accept", "swap_rules", "drop_rule", "remove_timeout_flag", "change_set_size"])
def test_compare_detects_mutations(fixture_json, mutation): ...       # フィクスチャを書き換えた live → 不一致で 1
def test_main_fails_closed_on_bad_config(tmp_path): ...  # Review Focus 2 → 1
def test_main_fails_on_runner_error(): ...               # nft 失敗 → 1
def test_expected_json_substitutes_mac_and_checks_sources(fixture_json, tmp_path):
    # 雛形の aa:bb:cc:dd:ee:ff が設定の MAC に置き換わり、雛形内に aa:bb:cc:dd:ee:ff が残らない
    # trusted_sources の cidr を 192.168.34.0/24 に変えた設定 → ValidationError
def test_fwcheck_runs_only_nft_list(fixture_json): ...   # runner に渡されるコマンドは ["nft", "-j", "list", "table", "inet", "craftlatch"] の 1 回だけ（unshare を呼ばない）
def test_neigh_parsing():
    assert neigh_mac("192.168.33.1 dev enp3s0 lladdr aa:bb:cc:dd:ee:ff REACHABLE") == "aa:bb:cc:dd:ee:ff"
    assert neigh_mac("192.168.33.1 dev enp3s0 INCOMPLETE") is None
def test_set_router_mac_does_not_overwrite(tmp_path): ...
```

- [ ] **Step 2: `uv run pytest tests/host/test_nftgen.py tests/host/test_fwcheck.py tests/host/test_routermac.py -v` を実行し、失敗することを確認する**

- [ ] **Step 3: 3 つのモジュールと `fwcheck` 起動スクリプトを実装する** — iface は `^[A-Za-z0-9_.-]{1,15}$`、MAC は `^[0-9a-f]{2}(:[0-9a-f]{2}){5}$`、cidr は `ipaddress.IPv4Network(strict=True)`。`routermac.main` は `ip route show default` と `ip neigh show <gw>`（なければ `ping -c 1 -W 2 <gw>` 後に再取得）を実行し、`--yes` がなければ `input()` で `y` を確認する。

- [ ] **Step 4: 同じテストを実行し、PASS を確認する**

- [ ] **Step 5: コミット**

```bash
git add host/lib/nftgen.py host/lib/fwcheck.py host/lib/fwcheck host/lib/routermac.py tests/host/test_nftgen.py tests/host/test_fwcheck.py tests/host/test_routermac.py
git commit -m "feat(host): nft ルール生成・構造検証・ルーター MAC 補完を追加"
```

### Task 7: `notify.py` と `mc-stop`

**Files:**
- Create: `host/lib/notify.py`、`host/lib/mc-stop`（0755）
- Test: `tests/host/test_notify.py`、`tests/host/test_mc_stop.py`

**Interfaces:**
- Produces:
  - `should_send(state_dir: Path, unit: str, now: int, window: int = 3600) -> bool`、`record_sent(state_dir: Path, unit: str, now: int)`
  - `build_payload(hostname: str, unit: str, now: int) -> dict`（`{"content": "[CraftLatch] <hostname>: <unit> が失敗しました (<ISO8601 JST>)。確認: journalctl -u <unit> -n 50"}`）
  - `post_webhook(url: str, payload: dict, timeout: float = 10.0) -> None`（失敗は例外）
  - `notify.main(argv: list[str]) -> int`（ユニット名 `^[A-Za-z0-9@._-]{1,128}$`、Webhook URL は `$CREDENTIALS_DIRECTORY/webhook`、状態は `$STATE_DIRECTORY`）
  - `mc-stop <PID>`（環境変数 `MC_CONSOLE` で FIFO のパスを上書き可能、既定 `/run/minecraft.console`。待機上限は `MC_STOP_WAIT`、既定 170。FIFO が無い・5 秒以内に書けない場合は待機せず 1 で終了し、systemd の SIGTERM に任せる）

- [ ] **Step 1: 失敗するテストを書く**

```python
def test_rate_limit(tmp_path):
    assert should_send(tmp_path, "craftlatch-poll.service", now)
    record_sent(tmp_path, "craftlatch-poll.service", now)
    assert not should_send(tmp_path, "craftlatch-poll.service", now + 3599)
    assert should_send(tmp_path, "craftlatch-poll.service", now + 3600)
def test_payload_has_no_log_body(): ...                      # content に "journalctl -u" を含み、IP 形式の文字列を含まない
def test_send_failure_does_not_record_and_returns_1(tmp_path, monkeypatch):   # Review Focus 5
    # post_webhook が HTTPError(429) を送出 → main は 1、record されない
def test_invalid_unit_name_returns_1(): ...
def test_mc_stop_writes_stop_and_waits(tmp_path):
    # mkfifo → 「1 行読んで 'stop' なら終了する」子プロセスを起動 → mc-stop <子PID>（MC_CONSOLE=fifo）が 0 で終わり、子プロセスが終了している
def test_mc_stop_empty_pid_is_noop(): ...                    # 引数なし → 0
def test_mc_stop_missing_fifo_fails_fast(tmp_path): ...      # MC_CONSOLE=存在しないパス → 6 秒以内に 0 以外で終了
def test_mc_stop_uses_default_path_when_unset(tmp_path): ... # MC_CONSOLE 未設定・/run/minecraft.console が無い環境 → 0 以外、stderr に /run/minecraft.console を含む
```

- [ ] **Step 2: `uv run pytest tests/host/test_notify.py tests/host/test_mc_stop.py -v` を実行し、失敗することを確認する**

- [ ] **Step 3: `notify.py` と `mc-stop` を実装する** — `mc-stop` は `#!/bin/sh`、`set -u`、`console="${MC_CONSOLE:-/run/minecraft.console}"`、FIFO でなければエラー出力して 1、`timeout 5 sh -c 'printf "stop\n" > "$1"' sh "$console"`（パスは位置引数で子シェルに渡す）が失敗したら 1、成功したら `kill -0 "$pid"` を 1 秒ごとに最大 `MC_STOP_WAIT` 回確認して 0。

- [ ] **Step 4: 同じテストを実行し、PASS を確認する**

- [ ] **Step 5: コミット**

```bash
git add host/lib/notify.py host/lib/mc-stop tests/host/test_notify.py tests/host/test_mc_stop.py
git commit -m "feat(host): Discord 通知と FIFO 経由の停止スクリプトを追加"
```

### Task 8: systemd ユニット・tmpfiles・`install.sh`・ローテーションスクリプト

**Files:**
- Create: `host/systemd/craftlatch-firewall.service`、`craftlatch-poll.service`、`craftlatch-poll.timer`、`craftlatch-apply.path`、`craftlatch-apply.service`、`craftlatch-idle.service`、`craftlatch-idle.timer`、`craftlatch-notify@.service`、`minecraft.socket`、`minecraft.service`
- Create: `host/tmpfiles.d/craftlatch.conf`、`host/install.sh`（0755）、`host/scripts/craftlatch-rotate-key`（0755）
- Test: `tests/host/test_units.py`

**Interfaces:**
- Consumes: Task 2〜7 の各スクリプト（配置先 `/usr/local/lib/craftlatch/`）、`routermac.main`、`nftgen.render_ruleset`
- Produces: `sudo host/install.sh [--yes] [--refresh-router-mac]`、`sudo host/scripts/craftlatch-rotate-key [--finish <OLD_KEY_ID>]`

- [ ] **Step 1: 失敗するテストを書く**（ユニットファイルを `configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=",))` で読み、`optionxform = str` で大文字小文字を保持して設計書の値を検査する。ユニットの構文自体は Task 15 で `systemd-analyze verify` により確認する）

```python
def test_python_units_use_E_s_not_I(): ...       # ExecStart が "/usr/bin/python3 -E -s /usr/local/lib/craftlatch/" で始まる
def test_onfailure_on_required_units(): ...      # poll・apply・apply.path・idle・firewall・minecraft に OnFailure=craftlatch-notify@%n.service
def test_minecraft_unit_values():
    # ExecStartPre=+/usr/local/lib/craftlatch/fwcheck、ExecStart の jar が /media/ssd/paper/paper.jar・-Xms4G -Xmx4G、
    # MemoryHigh=5G、MemoryMax=5632M、CPUQuota=300%、Restart=no、[Install] なし、RequiresMountsFor=/media/ssd/paper
def test_firewall_unit_has_no_execstop(): ...
def test_poll_timeouts(): ...                     # TimeoutStartSec=50s、TimeoutStopSec=10s
def test_apply_service_hardening(): ...          # CapabilityBoundingSet=CAP_NET_ADMIN、IPAddressDeny=any、PrivateNetwork なし
def test_tmpfiles_entries():
    assert "d /run/craftlatch/requests 1770 root craftlatch -" in text
@pytest.mark.parametrize("script", ["poll.py", "apply.py", "idle.py", "notify.py", "nftgen.py", "routermac.py", "fwcheck"])
def test_scripts_import_common_under_E_s(script, tmp_path):
    # host/lib を tmp_path/lib に丸ごとコピー（配置後と同じ構成）し、cwd を別ディレクトリにして
    # `sys.executable -E -s tmp_path/lib/<script> --import-check` を実行 → 終了コード 0 かつ stdout が "import-ok"
```

- [ ] **Step 2: `uv run pytest tests/host/test_units.py -v` を実行し、失敗することを確認する**

- [ ] **Step 3: ユニットと tmpfiles を作成する** — 内容は設計書 5.4〜5.7・6.2・7.1・7.2 のユニット全文と 5.2 の tmpfiles をそのまま使う（`minecraft.service` の `ExecStartPre` は `fwcheck`）。

- [ ] **Step 4: `uv run pytest tests/host/test_units.py -v` を実行し、PASS を確認する**

- [ ] **Step 5: `host/install.sh` を作成する** — `#!/usr/bin/env bash`、`set -euo pipefail`。設計書 4.7「Ubuntu 側の配置」の表どおり: 事前検証（root、`minecraft` ユーザー、`python3 -c 'import boto3'`、`nft`、`systemctl --version` ≥248、`/media/ssd/paper`）→ `craftlatch` ユーザー作成（なければ）→ `install -o root -g root -m 0755/0644` で配置 → `config.json` がなければ雛形をコピー（0644）→ `python3 -E -s /usr/local/lib/craftlatch/routermac.py [--yes] [--refresh]` → `python3 -E -s /usr/local/lib/craftlatch/nftgen.py --output /etc/craftlatch/craftlatch.nft`（設定が未完成で失敗した場合は「`/etc/craftlatch/config.json` の `queue_url` を入力して再実行」と表示して終了コード 1。キュー URL は Terraform の出力 `queue_url` を管理者が手元で確認して入力する）→ `systemd-tmpfiles --create /etc/tmpfiles.d/craftlatch.conf` → `systemctl daemon-reload` → 変更内容に応じた順序付きの案内を表示。`aws-credentials`・`discord-webhook`・既存の `config.json` は上書きしない。テスト用に配置先の接頭辞を環境変数 `CRAFTLATCH_ROOT`（既定は空）で変えられるようにする。

- [ ] **Step 6a: キーローテーションの失敗するテストを書く**（`tests/host/test_rotate_key.py`）

```python
def test_rotate_key_stages(tmp_path):
    # tmp_path/bin に偽の aws・systemctl（呼び出しを記録し、失敗させる段階を環境変数 FAKE_FAIL で指定）を置き、
    # AWS_CLI=tmp_path/bin/aws・SYSTEMCTL=tmp_path/bin/systemctl・CRAFTLATCH_ETC=tmp_path/etc・CRAFTLATCH_STATE=tmp_path/state で実行:
    # (a) 新キーの検証失敗 → 現行 aws-credentials は不変・新キーを delete・timer 再開・終了コード 1
    # (b) 切り替え後の受信失敗 → .old を戻す・新キーを delete・終了コード 1
    # (c) 確定後の旧キー削除失敗 → 新キーは削除しない・終了コード 1・`--finish <OLD>` で残りだけ実行して 0
    # (d) 新キー検証の呼び出しが、管理者の AWS_ACCESS_KEY_ID（テストで設定）を引き継がず、
    #     AWS_SHARED_CREDENTIALS_FILE=<CRAFTLATCH_ETC>/aws-credentials.new で行われたこと（偽 aws が環境を記録）
```

Run: `uv run pytest tests/host/test_rotate_key.py -v` → Expected: FAIL（スクリプトが無い）

- [ ] **Step 6b: `host/scripts/craftlatch-rotate-key` を作成する** — 設計書 9.1 の手順 1〜7（確定境界・段階別の復旧・`--finish`）。外部コマンドは `AWS_CLI`（既定 `aws`）・`SYSTEMCTL`（既定 `systemctl`）、ディレクトリは `CRAFTLATCH_ETC`（既定 `/etc/craftlatch`）・`CRAFTLATCH_STATE`（既定 `/var/lib/craftlatch`）で変えられるようにする。新キーの検証は、起動時に `aws_bin=$(command -v "$AWS_CLI")` で絶対パスを解決してから `env -i PATH=/usr/bin:/bin AWS_SHARED_CREDENTIALS_FILE="$CRAFTLATCH_ETC/aws-credentials.new" AWS_CONFIG_FILE=/dev/null AWS_PROFILE=default AWS_DEFAULT_REGION=ap-northeast-1 "$aws_bin" sts get-caller-identity` で行う。

Run: `uv run pytest tests/host/test_rotate_key.py -v` → Expected: PASS

- [ ] **Step 7: シェルスクリプトの構文を確認する**

Run: `bash -n host/install.sh && bash -n host/scripts/craftlatch-rotate-key && sh -n host/lib/mc-stop`
Expected: 出力なし（shellcheck は Task 12 の CI で実行する）

- [ ] **Step 8: コミット**

```bash
git add host/systemd host/tmpfiles.d host/install.sh host/scripts tests/host/test_units.py tests/host/test_rotate_key.py
git commit -m "feat(host): systemd ユニット・install.sh・キーローテーションを追加"
```

---

## フェーズ 2: Lambda

### Task 9: Lambda `craftlatch-auth`

**Files:**
- Create: `src/lambda/craftlatch_auth/__init__.py`（空）、`settings.py`、`cookies.py`、`oidc.py`、`pages.py`、`handler.py`
- Test: `tests/lambda/test_handler.py`、`tests/lambda/test_oidc.py`

**Interfaces:**
- Produces:
  - `settings.load() -> Settings`（環境変数 `COGNITO_DOMAIN, CLIENT_ID, ISSUER, REDIRECT_URI, QUEUE_URL, ALLOWED_GROUP, MAX_AUTH_AGE, GRANT_TTL`）
  - `cookies.encode(state, nonce, verifier, t) -> str`、`cookies.decode(cookie_header_values: list[str]) -> dict | None`（名前 `__Host-craftlatch`、属性 `HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=600`）
  - `oidc.new_pkce() -> tuple[str, str]`（verifier, S256 challenge）、`oidc.authorize_url(s, state, nonce, challenge) -> str`（`prompt=login`、`scope=openid`）、`oidc.exchange_code(s, code, verifier, urlopen=urllib.request.urlopen) -> dict`、`oidc.decode_claims(id_token: str) -> dict`、`oidc.validate_claims(claims, s, nonce, now) -> str`（`cognito:username` を返す。不正は `AuthError(kind)`、kind は `relogin|forbidden|account|temporary`）
  - `pages.render(kind: str, ip: str | None = None) -> dict`（`ok|relogin|forbidden|not_ipv4|account|temporary`。ヘッダーは設計書 4.3 のレスポンス形式どおり、Cookie 削除を含む）
  - `handler.lambda_handler(event, context) -> dict`（`GET /login`・`GET /callback`）

- [ ] **Step 1: 失敗するテストを書く**（`urlopen` と SQS クライアントは依存注入またはモジュール属性の monkeypatch で偽物にする。ID トークンは署名なしの JWT 形式を組み立てる）

```python
def test_login_redirect_and_cookie(): ...        # 302、location に prompt=login・code_challenge_method=S256・scope=openid、cookies に __Host-craftlatch
def test_callback_success_sends_message():
    # SendMessage の本文 == {"v":1,"type":"allow","ip":"8.8.8.8","iat":now,"exp":now+600,"id":<uuid>,"user":"alice"}
    # レスポンス 200・content-type text/html; charset=utf-8・本文に 8.8.8.8・Cookie 削除
def test_callback_state_mismatch_returns_relogin_page(): ...   # Review Focus 1：200 か 400 の HTML、SQS 未送信
@pytest.mark.parametrize("claim_patch,kind", [
    ({"auth_time": now-121}, "relogin"), ({"auth_time": now+61}, "relogin"), ({"nonce": "x"}, "relogin"),
    ({"aud": "other"}, "relogin"), ({"token_use": "access"}, "relogin"),
    ({"cognito:groups": ["other"]}, "forbidden"), ({"cognito:groups": "minecraft"}, "forbidden"),
    ({"cognito:username": "アリス"}, "account"),
])
def test_claim_failures(claim_patch, kind): ...  # SQS 未送信
def test_non_ipv4_or_private_source(): ...       # sourceIp "2001:db8::1" / "10.0.0.5" → not_ipv4 ページ、SQS 未送信
def test_token_exchange_error_temporary(): ...   # urlopen が例外 → temporary
def test_ignores_x_forwarded_for(): ...          # ヘッダーに別 IP があっても requestContext.http.sourceIp を使う
def test_unknown_route_404(): ...
def test_zip_layout_importable(tmp_path):
    # Task 11 の archive_file と同じ規則（source_dir = src/lambda、__pycache__ と *.pyc を除外）で zipfile を作り、
    # 展開先を sys.path にして importlib.import_module("craftlatch_auth.handler").lambda_handler が存在すること、
    # ZIP 内のパスが "craftlatch_auth/handler.py" であること（ZIP 直下に handler.py が無いこと）
def test_security_headers_present(): ...         # CSP default-src 'none'; style-src 'unsafe-inline'、no-store、no-referrer、nosniff
```

- [ ] **Step 2: `uv run pytest tests/lambda -v` を実行し、失敗することを確認する**

- [ ] **Step 3: 各モジュールを実装する** — state 比較は `hmac.compare_digest`。トークン交換は `application/x-www-form-urlencoded` の POST・タイムアウト 5 秒。署名検証はしない（設計書 4.3 の理由）。ログは `event=grant result=... user=... ip=... id=...`、トークン・コード・Cookie は出力しない。

- [ ] **Step 4: `uv run pytest tests/lambda -v` を実行し、PASS を確認する**

- [ ] **Step 5: コミット**

```bash
git add src/lambda tests/lambda
git commit -m "feat(lambda): Cognito OIDC コールバックと SQS 送信を追加"
```

---

## フェーズ 3: AWS インフラと CI

Terraform はコンテナ内に無く、レジストリへの通信もコンテナのファイアウォールで遮断されるため、`fmt`・`validate` は Task 12 の CI で実行する。各リソースの引数名は実装時に Context7（`/hashicorp/terraform-provider-aws`）で確認し、特に `user_pool_tier`・`ip_address_type`（設計書 U8）を確認する。

### Task 10: `infra/bootstrap`

**Files:**
- Create: `infra/bootstrap/versions.tf`、`backend.tf`（部分設定、`key` は init 時に `bootstrap/terraform.tfstate`）、`variables.tf`（`region`、`github_repository = "Knu334-Inc/CraftLatch"`）、`oidc.tf`、`roles.tf`、`boundary.tf`、`outputs.tf`（ロール ARN。sensitive にはしないが CI では使わない）

**Interfaces:**
- Produces: IAM ロール `craftlatch-gha-plan`・`craftlatch-gha-apply`、ポリシー `craftlatch-boundary`（Task 11 の `permissions_boundary` に ARN を渡す。Task 11 では `data "aws_iam_policy" "boundary" { name = "craftlatch-boundary" }` で参照）

- [ ] **Step 1: OIDC プロバイダーと信頼ポリシーを書く** — `token.actions.githubusercontent.com`、条件は `StringEquals` で `aud = sts.amazonaws.com`、plan は `sub = repo:Knu334-Inc/CraftLatch:pull_request`、apply は `sub = repo:Knu334-Inc/CraftLatch:environment:production`。

- [ ] **Step 2: state バケット権限を書く** — 設計書 4.7「state バケットの権限」: オブジェクトは `craftlatch/terraform.tfstate` と `.tflock` のみ、`ListBucket` は `StringLike s3:prefix = craftlatch/*`、`bootstrap/` とバケット設定変更は不許可。バケット名は変数 `state_bucket`（`terraform.tfvars` はコミットしない。`.gitignore` に `*.tfvars` を追加）。

- [ ] **Step 3: plan ロール・apply ロールの権限と Boundary を書く** — 設計書 4.7 の表のステートメントをそのまま（作成は対象 ARN + `iam:PermissionsBoundary`、付与系は 2 ARN 限定、`iam:Attach*` なし、`PassRole` は `iam:PassedToService = lambda.amazonaws.com`、Boundary・GHA ロール・OIDC プロバイダーへの書き込みを明示 Deny）。Boundary は設計書 4.5 末尾の範囲。

- [ ] **Step 4: コミットする**（`.terraform.lock.hcl` は Task 12 Step 5 で生成してコミットする）

```bash
git add infra/bootstrap .gitignore
git commit -m "feat(infra): GitHub OIDC と GHA 用ロール・Boundary の bootstrap を追加"
```

### Task 11: `infra/terraform`（アプリケーション本体）

**Files:**
- Create: `infra/terraform/versions.tf`、`backend.tf`（部分設定、`key = "craftlatch/terraform.tfstate"`、`use_lockfile = true`）、`variables.tf`（`region`、`project`。いずれも既定値あり。CI から入力する変数は無い）、`cognito.tf`（ドメインのプレフィックスは `random_id`（`hashicorp/random`、`byte_length = 4`）で生成した `craftlatch-<hex>`。設計書 4.6 の変数 `cognito_domain_prefix` は、CI から値を渡す経路を増やさないためこの方式に置き換える）、`api.tf`、`lambda.tf`、`sqs.tf`、`iam.tf`、`outputs.tf`

**Interfaces:**
- Consumes: Task 9 の `src/lambda/craftlatch_auth/`（`archive_file` は `source_dir = "${path.module}/../../src/lambda"`・`excludes` に `__pycache__` と `*.pyc` を指定し、ZIP 内に `craftlatch_auth/` ディレクトリを保つ。ハンドラー `craftlatch_auth.handler.lambda_handler`。Task 9 の `test_zip_layout_importable` と同じ規則）、Task 10 の Boundary 名
- Produces: 出力 `login_url`・`user_pool_id`・`queue_url`・`host_user_arn`（ログに出さないため CI では参照しない）

- [ ] **Step 1: `sqs.tf` と `iam.tf` を書く** — 設計書 4.4（Standard、保持 600、可視性 60、SSE-SQS、DLQ なし、TLS 以外拒否のキューポリシー）・4.5（Lambda ロールと `craftlatch-host` ユーザー、インラインポリシー、`permissions_boundary`。アクセスキーは作らない）。

- [ ] **Step 2: `cognito.tf` を書く** — 設計書 4.1 の表（Essentials、管理者のみ作成、MFA OFF、最小 14 文字、管理者リセットのみ、存在エラー秘匿、グループ `minecraft`、プレフィックスドメイン、Managed Login ブランディング既定）と、アプリクライアント（シークレットなし、`code` のみ、`openid`、コールバック 1 件、ID/アクセス 5 分、リフレッシュ 60 分）。

- [ ] **Step 3: `lambda.tf` と `api.tf` を書く** — Lambda（Python 3.13、arm64、128MB、10 秒、環境変数は設計書 4.3 の表、ロググループ保持 30 日）、HTTP API（IPv4、`GET /login`・`GET /callback` のみ、ペイロード 2.0、`$default` ステージ自動デプロイ、burst 5・rate 1）、Lambda の実行許可。アカウント ID は `data "aws_caller_identity"`。

- [ ] **Step 4: コミット**（`fmt`・`validate` は Task 12 の CI で、lock ファイルは Task 12 Step 5 で生成）

```bash
git add infra/terraform
git commit -m "feat(infra): Cognito・API Gateway・Lambda・SQS・IAM を追加"
```

### Task 12: GitHub Actions

**Files:**
- Create: `.github/workflows/ci.yml`、`.github/workflows/terraform-plan.yml`、`.github/workflows/terraform-apply.yml`、`.github/scripts/tf-summary.sh`（0755）

**Interfaces:**
- Consumes: Secret `AWS_PLAN_ROLE_ARN`・`AWS_APPLY_ROLE_ARN`・`TF_STATE_BUCKET`、Variable `AWS_REGION`
- Produces: `tf-summary.sh <tfplan>` が `terraform show -json` から「アドレス・操作種別」の Markdown 表と件数だけを `$GITHUB_STEP_SUMMARY` に出す

- [ ] **Step 1: `ci.yml` を書く** — すべての PR と main への push。`astral-sh/setup-uv` → `uv sync` → `uv run pytest`；`shellcheck host/install.sh host/scripts/craftlatch-rotate-key host/lib/mc-stop .github/scripts/tf-summary.sh`（ubuntu ランナーに同梱）；`hashicorp/setup-terraform`（`terraform_wrapper: false`）→ `infra/terraform` と `infra/bootstrap` で `terraform fmt -check -recursive` と `terraform init -backend=false -input=false -lockfile=readonly && terraform validate`。`permissions: contents: read`。

- [ ] **Step 2: `terraform-plan.yml` を書く** — トリガーは `infra/terraform/**`・`src/lambda/**` を変更した PR。ジョブ条件 `if: github.event.pull_request.head.repo.full_name == github.repository`。`permissions: id-token: write, contents: read`。`configure-aws-credentials`（plan ロール）→ `terraform init -input=false -lockfile=readonly -backend-config="bucket=${{ secrets.TF_STATE_BUCKET }}" > "$RUNNER_TEMP/init.log" 2>&1` → `terraform plan -input=false -no-color -out="$RUNNER_TEMP/tfplan" > "$RUNNER_TEMP/plan.log" 2>&1` → `tf-summary.sh`。失敗時はコマンド名と終了コードのみ表示。アカウント ID を `::add-mask::`。artifact・cache は使わない。`setup-terraform` は `terraform_wrapper: false`（ラッパーが stdout をステップ出力に保存するのを防ぐ）。最後に `if: always()` のステップで `$RUNNER_TEMP` の `*.log`・`tfplan*`・`*.json` を削除する。

- [ ] **Step 3: `terraform-apply.yml` を書く** — トリガーは main への push（同じパス）と `workflow_dispatch`。`environment: production`、`concurrency: { group: terraform-apply, cancel-in-progress: false }`。plan と同じ出力抑止で `plan -out` → `git fetch origin main` して `github.sha` と `origin/main` の先頭を比較し、不一致なら apply せず成功終了してサマリーに表示 → `terraform apply -input=false "$RUNNER_TEMP/tfplan"`。サマリーにコミット SHA を表示。

- [ ] **Step 4: 外部 Action を SHA で固定する** — `actions/checkout`、`astral-sh/setup-uv`、`hashicorp/setup-terraform`、`aws-actions/configure-aws-credentials` の最新安定版タグのコミット SHA を `gh api repos/<owner>/<repo>/git/ref/tags/<tag>` で調べ、`uses: <repo>@<sha> # <tag>` の形で書く。Terraform のバージョンも固定する（1.10 以上の最新安定版）。

- [ ] **Step 5: コミット**

```bash
git add .github
git commit -m "ci: テスト・Terraform plan/apply のワークフローを追加"
```

- [ ] **Step 5b: lock ファイルを生成してコミットする（管理者が Terraform の入った手元の端末で実行）** — コンテナからはレジストリに接続できないため管理者に依頼する。`infra/bootstrap` と `infra/terraform` のそれぞれで `terraform init -backend=false && terraform providers lock -platform=linux_amd64 -platform=darwin_arm64` を実行し、生成された `.terraform.lock.hcl` 2 つをコミットする（CI の `-lockfile=readonly` と初回 apply より前に必要）。

```bash
git add infra/bootstrap/.terraform.lock.hcl infra/terraform/.terraform.lock.hcl
git commit -m "chore(infra): Terraform プロバイダーの lock ファイルを追加"
```

- [ ] **Step 6: ブランチを push して PR を作成し、`ci` が成功することを確認する**（push・PR 作成は管理者の了承を得てから行う）

Run: `gh pr checks --watch`
Expected: `ci` が pass。`terraform-plan` は AWS 側の bootstrap（Task 14）前なので失敗してよい（その旨を PR に記載）。Terraform の `fmt`・`validate` で失敗した場合は Task 10・11 を修正して再 push する。

---

## フェーズ 4: 構築と結合試験（管理者と共同）

### Task 13: ドキュメント（setup・operations・user-guide の初版）

Task 14・15 の作業手順書になるため、構築より先に初版を作る（最終整備は Task 16）。

**Files:**
- Create: `docs/setup.md`、`docs/operations.md`、`docs/user-guide.md`

- [ ] **Step 1: `docs/setup.md` を書く** — 設計書 12 章の内容: ① state バケット作成（設計書 4.6 の 3 コマンド）→ ② `infra/bootstrap` の init/plan/apply と lock ファイルのコミット → ③ GitHub の Secret・Variable・Environment（許可ブランチ main、承認者）・ブランチ保護・Actions 設定（設計書 4.7 の検証項目）→ ④ PR を main にマージし、起動した apply を Environment `production` で承認（初回 apply）→ ⑤ Cognito ユーザー作成（設計書 4.1 の 2 コマンド）→ ⑥ Ubuntu: 事前準備・移行（設計書 7.6）・アクセスキーと Webhook の配置・`install.sh`・ユニットの enable → ⑦ 検証項目（`wg show wg0 allowed-ips`、外部回線からの `nc -vz -w 5 <グローバルIP> 25565`、LAN からの接続でカウンタが増えること）。アカウント ID などは `<ACCOUNT_ID>` のプレースホルダーにする。

- [ ] **Step 2: `docs/operations.md` を書く** — 設計書 9 章（日常運用、ローテーション、漏えい対応）、8 章の障害ケースごとの確認コマンド、ルール変更・ルーター交換（`install.sh --refresh-router-mac`）、Paper 更新、apply.path の復旧（`reset-failed`）。

- [ ] **Step 3: `docs/user-guide.md` を書く** — 設計書 6.6 の制約を利用者向けの言葉で。ログイン URL は「管理者から共有」とし記載しない。LAN からは `192.168.33.4` を指定。

- [ ] **Step 4: コミット**

```bash
git add docs/setup.md docs/operations.md docs/user-guide.md
git commit -m "docs: 構築・運用・利用者向けドキュメントの初版を追加"
```

### Task 14: AWS 側の構築（管理者が実行、エージェントは手順の案内と結果確認）

- [ ] **Step 1: 管理者が `docs/setup.md` の ①〜③ を実施する**（AWS 管理者の認証情報が必要なため、エージェントは実行しない。lock ファイルは Task 12 Step 5b でコミット済みであること）
- [ ] **Step 2: 公開ログの抑止を確認する** — Task 12 Step 6 の PR ブランチ（ワークフローを含む）から一時ブランチを作り、`infra/terraform` に構文は正しいが plan で失敗する変更（存在しない IAM ポリシー名の data ソース等）を入れて main 向けの PR を作る（`pull_request` イベントでは PR 側のワークフロー定義が使われるため、main にワークフローが無くても動く）。`terraform-plan` のログとジョブサマリーに Terraform の出力本文・URL・アカウント ID が出ず、「失敗したコマンド名と終了コード」だけが出ることを確認してから PR を閉じる。
- [ ] **Step 3: Task 12 Step 6 の PR を main にマージし、初回 apply を承認する（管理者）** — bootstrap と GitHub 側の保護設定（Step 1）、ログ抑止の確認（Step 2）、PR の `ci` 成功がそろってからマージする（それより前にマージすると、main への push で起動した apply がロールを引き受けられず失敗する）。マージで起動した `terraform-apply` を初回 apply として扱い、Environment `production` で承認する。`workflow_dispatch` は使わない。

Run: `gh run list --workflow terraform-apply.yml --limit 1`
Expected: `completed success`。サマリーに作成リソースの一覧があり、URL やアカウント ID が出ていない。

- [ ] **Step 4: U11 — apply ロールの権限昇格防止を検証する（管理者が実行）** — `aws iam simulate-principal-policy --policy-source-arn <apply ロール ARN> --action-names <アクション> --resource-arns <ARN> [--context-entries <条件>] --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output text` で次の表をすべて確認し、結果を記録する。期待「allowed」は `EvalDecision` が `allowed`、「denied」は `explicitDeny` または `implicitDeny`。条件付きのケースは `--context-entries "ContextKeyName=iam:PermissionsBoundary,ContextKeyValues=<Boundary の ARN>,ContextKeyType=string"`（別 Boundary のケースは別の ARN、条件なしのケースは指定しない）、`--context-entries "ContextKeyName=iam:PassedToService,ContextKeyValues=lambda.amazonaws.com,ContextKeyType=string"`、`--context-entries "ContextKeyName=s3:prefix,ContextKeyValues=craftlatch/,ContextKeyType=string"` を付ける。

| アクション | 対象 | 期待 |
|---|---|---|
| `iam:CreateRole` | `role/craftlatch-auth-role`（Boundary 条件あり） | allowed |
| `iam:CreateRole` | `role/craftlatch-auth-role`（Boundary 条件なし・別 Boundary） | denied |
| `iam:CreateRole`・`iam:PutRolePolicy` | `role/other-role` | denied |
| `iam:PutRolePolicy`・`iam:UpdateAssumeRolePolicy`・`iam:DeleteRolePermissionsBoundary` | `role/craftlatch-gha-plan`・`role/craftlatch-gha-apply` | denied |
| `iam:AttachRolePolicy` | `role/craftlatch-auth-role` | denied |
| `iam:CreatePolicyVersion`・`iam:SetDefaultPolicyVersion` | `policy/craftlatch-boundary` | denied |
| `iam:UpdateOpenIDConnectProviderThumbprint` | OIDC プロバイダー | denied |
| `iam:PassRole` | `role/craftlatch-auth-role`（`iam:PassedToService=lambda.amazonaws.com`） | allowed |
| `iam:PassRole` | `role/craftlatch-gha-apply` | denied |
| `s3:GetObject`・`s3:PutObject` | `<バケット>/bootstrap/terraform.tfstate` | denied |
| `s3:PutBucketPolicy` | `<バケット>` | denied |

- [ ] **Step 5: 未検証事項 U8・U10・U11 の結果を `docs/superpowers/plans/2026-11-01-craftlatch-spike.md` に追記してコミットする**（U8: 引数名、U10: もう一度 `workflow_dispatch` で apply して Lambda の差分が出ないか、U11: Step 3 の apply が権限不足なく通ったことと Step 4 の表）

### Task 15: Ubuntu 側の構築と結合試験（管理者が sudo を実行）

- [ ] **Step 1: 管理者が `docs/setup.md` の ⑥ を実施する**（バックアップ取得、`whiteman.jar` 廃止、`python3-boto3` 導入、`minecraft` ユーザーへの移行、`server.properties` 変更、`paper.jar` リンク、アクセスキー・Webhook 配置、`sudo host/install.sh`、ユニット enable）

- [ ] **Step 1b: ユニットの構文を確認する**

Run（管理者）: `sudo systemd-analyze verify /etc/systemd/system/craftlatch-*.service /etc/systemd/system/craftlatch-*.timer /etc/systemd/system/craftlatch-apply.path /etc/systemd/system/minecraft.service /etc/systemd/system/minecraft.socket`
Expected: エラーなし（警告は内容を確認して記録）

- [ ] **Step 2: fail-closed を確認する**（Minecraft が待ち受けている状態で確認しないと、接続失敗がファイアウォールによるものか判別できない）

Run（管理者）: `sudo nft list table inet craftlatch` と `systemctl status craftlatch-firewall.service`
Expected: 設計書 6.1 のルールが並び、`active (exited)`。

続けて、LAN から認証して Minecraft を起動し（`ss -tlnp | grep 25565` で待ち受けを確認）、**認証していない外部回線**から `nc -vz -w 5 <グローバルIP> 25565` と Bedrock 版クライアントでの接続を試す。
Expected: どちらも接続できず、`sudo nft list chain inet craftlatch input` の `tcp dport 25565 counter drop` と `udp dport 19132 counter drop` のカウンタが試行の前後で増える。また、この起動で `minecraft.service` の `ExecStartPre`（fwcheck）が `RestrictNamespaces=yes` 等の制限下で成功していること（`journalctl -u minecraft -b | grep -i fwcheck` にエラーが無く、`systemctl status minecraft` が active）を確認する。

- [ ] **Step 3: 正常系と期限の延長を確認する** — 外部回線の端末でログイン → 1〜2 分で `sudo nft list set inet craftlatch allow4` に IP が現れる → Java 版で接続し、12 分以上プレイを続けても切断されず、その間 `expires` が 10 分近くに戻り続けることを数回確認する → Bedrock 版でも同じことを確認する（UDP の期限延長）→ 切断後 10 分で set から消える。

- [ ] **Step 4: 実構成での FIFO 停止とワールド保存を確認する** — 15 分無人で停止し、`journalctl -u minecraft` に `Stopping server` と `Saving worlds` が出て、プロセスが SIGTERM 前に終了していること（Geyser・DriveBackupV2・Multiverse を含む実構成での U6）。U4（SIGTERM 時の保存）は確認しない場合は未検証のまま残す。

- [ ] **Step 5: U12 を確認する** — DriveBackupV2 の設定ファイル（`plugins/DriveBackupV2/config.yml`）でバックアップ間隔と参加・退出時の設定を確認して記録する。アップロード中に `sudo systemctl stop minecraft.service` した場合の挙動（中断・完了待ち・`TimeoutStopSec=180` 内に収まるか）を 1 回試して記録する。

- [ ] **Step 6: 異常系を確認する**
  - 停止処理中の認証（U3 の実機確認）。
  - 再起動による要求消失: 管理者が `sudo systemctl stop craftlatch-apply.path` で適用を止めた状態でログインし、`/run/craftlatch/requests/` に要求ファイルが置かれ SQS から削除されたことを確認 → OS を再起動 → 許可リストが空で Minecraft が自動起動しないこと → もう一度ログインすると正常に許可・起動されること。
  - fwcheck: 本番のテーブルは変更せず、別のネットワーク名前空間で確認する。ルーターの MAC を実際の値にした不正版を `sed 's/aa:bb:cc:dd:ee:ff/<実際のMAC>/' /home/agent/craftlatch-probe/craftlatch-probe-bad.nft > /home/agent/craftlatch-probe/bad-real.nft` で作り、`sudo unshare --net sh -c 'nft -f /home/agent/craftlatch-probe/bad-real.nft; echo load=$?; /usr/local/lib/craftlatch/fwcheck; echo rc=$?'` → Expected: `load=0`（構文は正しく読み込めている）かつ `rc=1`。正常系は `sudo unshare --net sh -c 'nft -f /etc/craftlatch/craftlatch.nft; echo load=$?; /usr/local/lib/craftlatch/fwcheck; echo rc=$?'` → Expected: `load=0` かつ `rc=0`。
  - 通知: `sudo systemctl start craftlatch-notify@test.service` で Discord に通知が届くこと。

- [ ] **Step 7: LAN からの接続を確認する（U13）** — LAN の端末から `192.168.33.4` に接続でき、LAN ルールのカウンタが増える。

- [ ] **Step 8: 結果を `docs/superpowers/plans/2026-11-01-craftlatch-spike.md` に追記してコミットする**

---

## フェーズ 5: 仕上げ

### Task 16: ドキュメント整備（設計書 12 章）

**Files:**
- Modify: `README.md`（テンプレートの説明を置き換え）、`CLAUDE.md`（`AGENTS.md` はシンボリックリンクのまま）、`docs/setup.md`・`docs/operations.md`・`docs/user-guide.md`（Task 14・15 で判明した差分を反映）

- [ ] **Step 1: `README.md` を書き換える** — 概要、設計書 3 章の Mermaid 全体構成図、ディレクトリ構成、各文書へのリンク。DevContainer テンプレートの説明は「開発環境」節に短く残す。
- [ ] **Step 2: `CLAUDE.md` を書く** — プロジェクト概要、ディレクトリの役割、`uv run pytest`、CI での Terraform 検証、守るべき方針（Global Constraints の要点）、サーバーへの SSH は `-p 3340`・sudo は管理者が実行。
- [ ] **Step 3: Task 14・15 の結果（未検証事項の結論、手順の修正点）を 3 つの文書と設計書 13 章に反映する**
- [ ] **Step 4: リンク切れ・プレースホルダーの残り・公開すべきでない値の混入を確認する**

Run: `grep -rnE '[0-9]{12}|amazonaws\.com/[0-9]|@gmail\.com|execute-api\.[a-z0-9-]+\.amazonaws\.com' README.md CLAUDE.md docs/`
Expected: 出力なし（プレースホルダー `<ACCOUNT_ID>` 等のみ）

- [ ] **Step 5: コミット**

```bash
git add README.md CLAUDE.md docs/
git commit -m "docs: README・CLAUDE.md と各種ドキュメントを整備"
```
