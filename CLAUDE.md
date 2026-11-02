# CraftLatch

Cognito でログインした人のグローバル IPv4 アドレスだけを、自宅の Minecraft サーバー（Paper + Geyser + Floodgate）に通す仕組み。概要と構成図は `README.md`、設計とその理由は `docs/superpowers/specs/2026-11-01-craftlatch-design.md`（長いので必要な章だけ読む）。

<!-- AGENTS.md はこのファイルへのシンボリックリンク。編集はこのファイルに対して行う。 -->

## コマンド

- 環境の用意: `uv sync`（コンテナに `python3` はない。Python は uv が用意する）
- テスト全体: `uv run pytest`
- 1 ファイルだけ: `uv run pytest tests/host/test_apply.py`
- shellcheck と terraform はコンテナに入っていない。検証は CI（`.github/workflows/ci.yml`）が行う。手元で使うなら CI と同じ版（terraform 1.17.1）を入れ、`terraform fmt -check -recursive infra/` と `terraform -chdir=<dir> init -backend=false && terraform -chdir=<dir> validate` を `infra/terraform` と `infra/bootstrap` の両方で実行する
- shellcheck の対象: `host/install.sh` `host/scripts/craftlatch-rotate-key` `host/lib/mc-stop` `.github/scripts/tf-summary.sh`

## 構成

- `src/lambda/craftlatch_auth/`: AWS Lambda（`/login`・`/callback`）。Python 3.13・arm64
- `host/`: Ubuntu サーバーに `host/install.sh` で配置するもの。`host/lib/` が本体、`host/systemd/` がユニット
- `infra/bootstrap/`: GitHub Actions 用 IAM ロール・Permissions Boundary・Cognito User Pool。**管理者が手元で apply する**
- `infra/terraform/`: それ以外の AWS リソース。PR で plan、`main` へのマージ後に GitHub Actions が apply する（`production` 環境の承認が必要）
- `docs/setup.md`・`docs/operations.md`・`docs/user-guide.md`: 手順と利用者向け案内。設計書は「なぜそう決めたか」の記録で、変わりうる手順は docs 側に書く

## 公開リポジトリ

- AWS アカウント ID・メールアドレス・API Gateway や SQS の URL・Cognito のドメインを、コード・ドキュメント・コミットメッセージ・PR・ログ出力に書かない。ドキュメントでは `<ACCOUNT_ID>` `<BUCKET>` `<POOL_ID>` などのプレースホルダーを使う
- 秘密情報（アクセスキー・Discord Webhook URL）はリポジトリにも Terraform state にも置かない。サーバーの `/etc/craftlatch/` にだけ置く
- ドキュメントやコミットの前に混入を確認する: `grep -rnE '[0-9]{12}|amazonaws\.com/[0-9]|@gmail\.com|execute-api\.[a-z0-9-]+\.amazonaws\.com' README.md CLAUDE.md docs/`

## コードの約束

- ホストの Python は Python 3.12 で動くこと。依存は標準ライブラリと apt の `python3-boto3` だけ
- ホストの Python は `python3 -E -s` で起動する。`-I` は使わない（同じディレクトリの `common.py` を import できなくなる）
- Lambda は標準ライブラリとランタイム同梱の boto3 だけ。外部パッケージを追加しない
- 外部コマンドは `subprocess.run([...], shell=False, timeout=10)` で呼ぶ
- 要求ファイルの検証は poll・apply・idle で共通の `host/lib/common.py` にまとめる。apply は poll が検証済みでも必ず再検証する
- 許可期限 600 秒・無人停止 900 秒・`MAX_AUTH_AGE=120` などの値は設計書 5 章・4.3 で決めたもの。変える場合は設計書と docs も合わせて直す
- ポートは Java TCP/25565、Bedrock UDP/19132。LAN（`enp3s0`、ルーターの MAC は除外）と WireGuard（`wg0`）からの接続は認証なしで許可する

## インフラの約束

- AWS リソースの名前は `craftlatch-` で始め、タグ `Project=CraftLatch` を付ける。GitHub Actions のロールの権限はこの接頭辞とタグで絞っているため、外れると apply が AccessDenied になる
- `infra/terraform` で新しい種類のリソースや API を使うときは、apply・plan ロールの権限（`infra/bootstrap/roles.tf`）も足す。bootstrap の apply は管理者が行う
- AWS アカウントは他のシステムと共有している。ロールの権限を `*` で広げない
- GitHub Actions の外部 Action はコミット SHA で固定し、末尾のコメントに版を書く
- Terraform 本体とプロバイダーは版を固定し、`.terraform.lock.hcl` をコミットする
- `infra/terraform` と `infra/bootstrap` を手元で扱うときは `TF_DATA_DIR` をルートごとに分ける（共通にすると別のルートの state を読む）

## 作業の範囲

- 本番サーバーへの SSH・sudo、AWS の変更操作、`git push` は、指示されたときだけ行う。sudo が必要な作業は手順を示し、管理者に実行してもらう
## 書き方

- 応答・ドキュメント・コードのコメント・コミットメッセージは日本語
- コミットメッセージは Conventional Commits 形式で、件名は日本語（例: `fix(infra): apply ロールにステージのタグ付き作成を許可する`）。scope は `host`・`infra`・`lambda` など
- docs のコマンド例は 1 行で書く（行継続の `\` を使わない）
