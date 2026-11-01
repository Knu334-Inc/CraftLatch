# 初期構築手順（管理者向け）

CraftLatch を最初から構築する手順書です。上から順に実行してください。設計の理由は `docs/superpowers/specs/2026-11-01-craftlatch-design.md` にあります。日常の運用は `docs/operations.md`、プレイヤー向けの案内は `docs/user-guide.md` を参照してください。

このリポジトリは公開です。アカウント ID・メールアドレス・API の URL・キューの URL・Cognito のドメインは、リポジトリにもコミットメッセージにも PR にも書かないでください。この文書では `<ACCOUNT_ID>`、`<BUCKET>`、`<POOL_ID>` などのプレースホルダーで表します。実際の値は手元のシェルとサーバー上の `/etc/craftlatch/` にだけ置きます。

## 前提と注意

- 手元の PC に AWS CLI と Terraform（1.10 以上。CI は 1.17.1 を使用）があり、管理者用の AWS 認証情報（`AWS_PROFILE` 等）で操作できること。
- **この AWS アカウントは CraftLatch 専用であることを前提とします。** apply 用ロール（`craftlatch-gha-apply`）の権限は名前またはタグ `Project=CraftLatch` で絞っていますが、サービスによっては絞り込みが完全ではありません。同じアカウントに他の用途のリソースがあると、タグのない Cognito ユーザープールなどを apply ロールが操作できる残存リスクがあります。他の用途と共用しないでください。
- Cognito のユーザー名は大文字小文字を区別しません（作成後は変更できません）。ユーザー名は正規表現 `^[A-Za-z0-9._@+-]{1,128}$` に合うものだけを作成してください。合わない名前は Cognito では作れても、Lambda とホスト側が拒否し、完了ページが出ないか許可されません。
- Ubuntu サーバーの前提: systemd 248 以上、`/media/ssd/paper` に Paper がある、ネットワークインターフェース `enp3s0`（LAN）と `wg0`（WireGuard）がある。
- 「未検証事項」と書いた項目は、実機で初めて確認する事項です。結果を記録し、対処は Task 14 で決めます。

## ① state バケットの作成

Terraform の state を置く S3 バケットを、CLI で 1 回だけ作ります（Terraform 自身では作れないため）。管理者用の認証情報で実行します。

```
aws s3api create-bucket --bucket craftlatch-tfstate-<ACCOUNT_ID> --region ap-northeast-1 --create-bucket-configuration LocationConstraint=ap-northeast-1
aws s3api put-bucket-versioning --bucket craftlatch-tfstate-<ACCOUNT_ID> --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket craftlatch-tfstate-<ACCOUNT_ID> --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

以降、バケット名を `<BUCKET>`（`craftlatch-tfstate-<ACCOUNT_ID>`）と書きます。暗号化は新規バケットの既定（SSE-S3）です。

## ② bootstrap（OIDC プロバイダー・GitHub Actions 用ロール・Boundary）

`infra/bootstrap` は GitHub Actions が動く前に必要で、通常の apply ロールからは変更できないため、管理者が手元から実行します。

リポジトリのルートで実行します（`cd` は使いません）。

```
terraform -chdir=infra/bootstrap init -backend-config="bucket=<BUCKET>" -backend-config="key=bootstrap/terraform.tfstate"
terraform -chdir=infra/bootstrap plan -var="state_bucket=<BUCKET>"
terraform -chdir=infra/bootstrap apply -var="state_bucket=<BUCKET>"
```

- `state_bucket` は必須の変数です。アカウント ID を含むため、`-var` で渡すか、コミットされない `terraform.tfvars`（`*.tfvars` は `.gitignore` 済み）に書きます。
- `github_repository` の既定値は `Knu334-Inc/CraftLatch` です。別のリポジトリで使う場合は `-var="github_repository=<OWNER>/<NAME>"` を付けます。
- apply 後、次の出力を控えます（③の Secret に設定します）。

```
terraform -chdir=infra/bootstrap output -raw plan_role_arn
terraform -chdir=infra/bootstrap output -raw apply_role_arn
```

### ロックファイルのコミット

CI は `terraform init -lockfile=readonly` を使うため、`.terraform.lock.hcl` が先にコミットされていないと CI が失敗します。開発機（Apple Silicon の Mac）と CI（Linux x86_64）の両方で使えるよう、両方のルートで、リポジトリのルートから次を実行します。

```
terraform -chdir=infra/bootstrap providers lock -platform=linux_amd64 -platform=darwin_arm64
terraform -chdir=infra/terraform providers lock -platform=linux_amd64 -platform=darwin_arm64
```

`infra/bootstrap/.terraform.lock.hcl` と `infra/terraform/.terraform.lock.hcl` を、**最初の PR の CI が走る前に**コミットします（最初の PR に含めて構いません）。プロバイダーの更新は、`terraform providers lock` をやり直す明示的な PR で行います。

## ③ GitHub の設定

リポジトリの Settings で次を設定します。ここは設計書 4.7 の検証項目です。設定後にチェックリストとして確認してください。

### Secret・Variable

| 名前 | 種類 | 値 |
|---|---|---|
| `AWS_PLAN_ROLE_ARN` | Secret | ②で控えた `plan_role_arn` |
| `AWS_APPLY_ROLE_ARN` | Secret | ②で控えた `apply_role_arn` |
| `TF_STATE_BUCKET` | Secret | `<BUCKET>` |
| `AWS_REGION` | Variable | `ap-northeast-1` |

`production` Environment を使うジョブがあるため、Secret はリポジトリ単位で設定して構いません。

### Environment `production`

- Settings > Environments で `production` を作成する。
- Deployment branches and tags: **Selected branches and tags** にして `main` のみを許可する。
- Required reviewers: 管理者（自分）を追加する。
- 管理者が 1 名のため、**自己承認を許可したまま**にする（Prevent self-review は有効にしない。有効にすると誰も承認できなくなる）。この承認は「内容を自分で確認してから実行する」関門であり、第三者によるレビューではありません。
- apply ロールの信頼ポリシーは `environment:production` のみを条件にしており、main への限定はこの Environment の設定で成立します。**ここを設定しないと main 以外のブランチから apply ロールを使えてしまいます。**

### ブランチ保護（`main`）

- Require a pull request before merging を有効にする。
- Require status checks to pass: **`test`、`shellcheck`、`terraform`** を必須にする（`ci` ワークフローの 3 つのジョブ名）。
- force push と削除を禁止する（Allow force pushes・Allow deletions は無効）。

### Actions の設定

- Settings > Actions > General > Fork pull request workflows: 外部の協力者（outside collaborators）の PR のワークフロー実行は **承認を必須**にする。
- Workflow permissions: **Read repository contents permission**（既定の `GITHUB_TOKEN` を読み取り専用）にする。
- 管理者アカウントには 2 要素認証（パスキー）を設定してください。Environment・ブランチ保護・ワークフローを変更できるのは管理者だけで、この関門は管理者アカウントの侵害には無力です。

### 設定の確認

- Environment `production` の許可ブランチが `main` のみであること。
- 必須の承認者に自分が入っていること。
- ブランチ保護の必須チェックに `test`・`shellcheck`・`terraform` が出ていること（CI が 1 度走るまで候補に出ない場合があります。最初の PR の CI 実行後に追加してください）。
- Variable `AWS_REGION` と 3 つの Secret の名前が一致していること。

## ④ PR を main にマージし、初回 apply を承認する

1. ロックファイルなどを含むブランチから PR を作る。`infra/terraform/**` または `src/lambda/**` を変更する PR では `terraform-plan` ワークフローが走り、ジョブサマリーにリソースの一覧と作成・変更・削除の件数だけが表示されます（Terraform の出力全文は公開ログに出しません）。
2. `ci`（`test`・`shellcheck`・`terraform`）と `terraform-plan` が成功することを確認して、PR を main にマージする。
3. マージ後に `terraform-apply` ワークフローが起動する。Environment `production` の承認待ちになるので、Actions の画面で内容（コミット SHA）を確認して承認する（初回 apply）。
4. 成功したら、ジョブサマリーの作成件数を確認する。失敗した場合、公開ログには失敗したコマンド名と終了コードしか出ません。原因は手元で調べます（次項）。

**手元での確認（plan・output）** は、管理者用の認証情報で、リポジトリのルートから行います。

```
terraform -chdir=infra/terraform init -backend-config="bucket=<BUCKET>"
terraform -chdir=infra/terraform plan
```

### 未検証事項 U11 / 初回実行時の確認

apply ロールと plan ロールに必要な IAM アクションの最小集合は、実機で初めて確認します。特に次の事象は設計時には確認できていません。

- **state がまだ存在しない最初の `terraform plan`（PR）と最初の apply が、S3 の 404（NoSuchKey）ではなく 403 で失敗する可能性があります。** `s3:ListBucket` の許可を `s3:prefix` の条件（`craftlatch/*`）で絞っているため、state オブジェクトが存在しないときに S3 が存在を隠して 403 を返す場合があります。S3 backend の `init` がワークスペース一覧の取得で別の prefix を要求して失敗する可能性も同じ条件に関係します。
- 失敗した場合は、**ジョブの失敗箇所（コマンド名と終了コード）を記録し、対処（IAM ポリシーの修正など）は Task 14 で決めます。** 手順書の回避策はまだありません。これは未検証事項です。
- `iam:PermissionsBoundary` 条件で権限昇格を防げること、`archive_file` が同じ内容から同じ ZIP を作ること（変わると、コード未変更でも plan に Lambda の更新が出続けますが、デプロイの正しさには影響しません）も、初回の plan・apply で確認します。

### `queue_url` を取得する（手元でのみ）

ホストの設定に必要です。**CI のログには決して出力しません。** 管理者が手元の、リポジトリのルートで `terraform output` を実行します。

```
terraform -chdir=infra/terraform init -backend-config="bucket=<BUCKET>"
terraform -chdir=infra/terraform output -raw queue_url
```

値は⑥で `/etc/craftlatch/config.json` に入力します。シェルの履歴やチャットに貼らないでください。ログイン URL と User Pool ID も同じ方法で取得できます（`terraform -chdir=infra/terraform output -raw login_url`、`terraform -chdir=infra/terraform output -raw user_pool_id`）。ログイン URL は利用者に直接共有します（リポジトリには書きません）。

## ⑤ Cognito ユーザーの作成

`<POOL_ID>` は、リポジトリのルートで `terraform -chdir=infra/terraform output -raw user_pool_id` を実行して取得します。

```
aws cognito-idp admin-create-user --user-pool-id <POOL_ID> --username <NAME> --temporary-password '<TEMP_PASSWORD>' --message-action SUPPRESS
aws cognito-idp admin-add-user-to-group --user-pool-id <POOL_ID> --username <NAME> --group-name minecraft
```

- 一時パスワードは別の経路で本人に伝え、初回ログイン時に変更させます（パスワードは 14 文字以上）。
- グループ `minecraft` に入っていないユーザーは、認証に成功しても拒否されます。
- ユーザー名は大文字小文字を区別せず、`^[A-Za-z0-9._@+-]{1,128}$` に合うものだけを作成します。

## ⑥ Ubuntu サーバーへの導入

自宅サーバーは外部からの接続を受け付けないため、GitHub Actions からは配置しません。サーバー上でリポジトリを取得し、`host/install.sh` を実行します。**`install.sh` は root 権限が必要なので `sudo` で、リポジトリのチェックアウトの中から実行します**（`sudo host/install.sh`。スクリプトは自身の位置を基準に `host/lib` などを探します）。

### 事前準備

1. ワールドのバックアップを取る（DriveBackupV2 の手動実行、または `/media/ssd/paper` 全体の複製）。
2. `whiteman.jar` を廃止する。root で常駐し、RCON を使います。`sudo systemctl status <PID>` で所属ユニット（または cron・screen 等）を特定して停止・無効化してください。RCON を無効にするとこのツールは動作しなくなります。
3. 依存パッケージを導入する（nftables は導入済みのはずです）。

```
sudo apt install python3-boto3 nftables
```

4. サーバーに AWS CLI v2 を導入する（初回のアクセスキー発行とキーのローテーションで使います。Ubuntu 24.04 には apt の `awscli` パッケージがないため snap を使います）。

```
sudo snap install aws-cli --classic
```

5. サーバー上の管理者用の AWS 認証情報は、**一時的なもの（SSO や短期間のプロファイル）だけ**を使い、作業が終わったら削除してください（`aws sso logout`、または `~/.aws` の該当プロファイルの削除）。キー作成の権限をサーバーに残さないためです（設計書 9.1）。長期のアクセスキーをサーバーに置かないでください。
6. **本番サーバーで確認すること:** `sudo` 下で管理者のプロファイルが読めるか（`sudo` は `HOME` を `/root` に変えるため）。ローテーションの手順は `docs/operations.md` 3 章を参照してください。

### 既存環境からの移行

```
sudo systemctl disable --now minecraft.service
sudo cp /etc/systemd/system/minecraft.service /etc/systemd/system/minecraft.service.bak
sudo useradd --system --home-dir /media/ssd/paper --shell /usr/sbin/nologin minecraft
sudo chown -R minecraft:minecraft /media/ssd/paper
sudo chmod 0750 /media/ssd/paper
```

続けて、Paper 側の設定を行います。

- `server.properties`: `online-mode=true`、`white-list=true`、`enforce-whitelist=true`（変更）、`enable-rcon=false`（変更。25575 番が全インターフェースで待ち受けているため）、`enable-query=false`、`enable-status=true`（無人停止の判定に必要）、`server-port=25565`、`server-ip` は空。
- Geyser（`plugins/Geyser-Spigot/config.yml`）: `bedrock.port: 19132`、`bedrock.address: 0.0.0.0`、`remote.auth-type: floodgate`。
- `/media/ssd/paper/paper.jar` を実体 jar（例 `paper-1.21.7-17.jar`）へのシンボリックリンクにする。

```
sudo -u minecraft ln -sfn paper-<ver>-<build>.jar /media/ssd/paper/paper.jar
```

- ホワイトリスト登録: Java 版は `whitelist add <名前>`、Bedrock 版は `fwhitelist add <ゲーマータグ>`。ホワイトリストが参加者を絞る本体です（Cognito はポートを開ける仕組みです）。

### アクセスキーと Webhook の配置

秘密情報は Terraform の state にもリポジトリにも置きません。`craftlatch-host` の最初のアクセスキーは、管理者用の認証情報で発行してサーバー上に直接書き出します（表示はされません）。ファイルは先に 0400 で作ってから書き込みます。

先に `sudo -v` で sudo の認証を済ませておきます（パイプの途中でパスワードを求められると、空のファイルになるのを防ぐため）。

```
sudo -v
sudo install -d -m 0755 /etc/craftlatch
sudo install -m 0400 -o root -g root /dev/null /etc/craftlatch/aws-credentials
aws iam create-access-key --user-name craftlatch-host --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text | awk '{printf "[default]\naws_access_key_id = %s\naws_secret_access_key = %s\n", $1, $2}' | sudo tee /etc/craftlatch/aws-credentials > /dev/null
date +%F | sudo tee /etc/craftlatch/aws-credentials.rotated
```

書き込めたかを確認します（空のファイルを検出するため）。1 つ目は新しいキーが一覧にあること、2 つ目は `1` が表示されることを確認します。

```
aws iam list-access-keys --user-name craftlatch-host
sudo grep -c '^aws_access_key_id = AKIA' /etc/craftlatch/aws-credentials
```

`aws-credentials.rotated` は発行日（`YYYY-MM-DD`）で、335 日経過すると poll が警告を出します。Discord Webhook URL も同様に 0400 で置きます。

```
sudo install -m 0400 -o root -g root /dev/null /etc/craftlatch/discord-webhook
read -rs WEBHOOK_URL && printf '%s\n' "$WEBHOOK_URL" | sudo tee /etc/craftlatch/discord-webhook > /dev/null; unset WEBHOOK_URL
```

### `install.sh` の実行

```
sudo host/install.sh
```

- 初回は `/etc/craftlatch/config.json` を雛形（`host/config.json.example`）から作成します。既存の `config.json`・`aws-credentials`・`discord-webhook` は上書きしません。
- ルーターの MAC（`exclude_ether_saddr`）を検出して確認を求めます。内容を確認して `y` を入力してください（確認を省略するなら `--yes`）。自動で取得できない場合は、`config.json` の `exclude_ether_saddr` に手で入力して再実行します。
- 雛形の `queue_url` は仮の値です。④で取得した値を `/etc/craftlatch/config.json` の `queue_url` に入力し、`install.sh` を再実行してください。値が不正だと nft ルールを生成できず、「queue_url を入力して再実行してください」と表示されて終了します（何も壊れません）。`sudoedit /etc/craftlatch/config.json` で編集します。
- 事前検証（root、`minecraft` ユーザー、`python3-boto3`、`nft`、systemd 248 以上、`/media/ssd/paper`）に失敗すると、何も変更せずに終了します。表示された項目を直して再実行します。
- `trusted_sources` の既定値は、`enp3s0` + `192.168.33.0/24`（ルーターの MAC を除外）と `wg0` + `10.0.0.0/24` です。環境が違う場合は、この段階で `config.json` を編集し、**`docs/operations.md` の「ルール変更」に従って fwcheck の雛形を取り直してください**（雛形と一致しないと Minecraft が起動しません）。

### ユニットの有効化

初回の配置では、`install.sh` が次の順序を表示します。上から順に実行します。

```
sudo systemctl enable --now craftlatch-firewall.service
sudo systemctl enable --now craftlatch-apply.path craftlatch-poll.timer
sudo systemctl enable craftlatch-idle.timer
```

`craftlatch-idle.timer` は Minecraft の起動に連動して動きます。`minecraft.service` 自体は自動起動しません（認証されたときに起動します）。

## ⑦ 検証

次をすべて確認し、結果を記録してください。

1. ファイアウォールの読み込み。

```
systemctl status craftlatch-firewall.service
sudo nft list table inet craftlatch
sudo nft list set inet craftlatch allow4
```

`active (exited)` であること。許可リスト（`allow4`）が空であること。`ipv6.disable=1` の環境では `table inet` が読み込めるかも、このとき確認します。

2. WireGuard の許可 IP。

```
sudo wg show wg0 allowed-ips
```

`trusted_sources` の `10.0.0.0/24` に含まれる IP だけが、WireGuard の許可 IP に載っていること。

3. 外部回線からの遮断。認証していない回線（例: スマホのテザリング）から。

```
nc -vz -w 5 <グローバルIP> 25565
```

認証前はタイムアウトすること。

4. 認証の通し確認。ログイン URL で認証し、1〜2 分後に同じ回線から `nc -vz -w 5 <グローバルIP> 25565` が接続でき、Minecraft（`systemctl status minecraft.service`）が起動していること。`sudo nft list set inet craftlatch allow4` にその IP が `expires` 付きで載ること。

5. LAN からの接続（未検証事項 U13）。LAN 内の端末から接続先に `192.168.33.4` を指定して接続し、次のカウンタが増えること。

```
sudo nft list chain inet craftlatch input
```

LAN 用のルール（`iifname "enp3s0"` の行）のカウンタが増えていれば正常です。ヘアピン NAT 経由（グローバル IP やドメイン名を指定）で接続していた場合は、ルーターの MAC の除外条件により遮断されるため、接続先を `192.168.33.4` に変更してもらいます。

6. 数分間プレイヤーがいない状態で、約 15 分後に Minecraft が自動停止すること（`journalctl -t craftlatch-idle --since today`）。
7. 通知は、障害が起きたときに Discord に届きます（`docs/operations.md` の障害ケースの確認と合わせて、必要なら意図的に失敗させて確認してください）。

実機でのみ確認できる項目（U1・U3・U5・U6・U12・U13）の結果は記録しておき、Task 14・15 で対処を決めます。

## つまずいたとき

- `terraform plan` / `apply` が 403 で失敗する: 上記の U11 の項目を参照。記録して Task 14 で対処を決める。
- CI の `terraform` ジョブがロックファイルのエラーで失敗する: ②のロックファイルを両方のルートでコミットしたか確認する。
- apply が `AccessDenied` で失敗する: ③の Environment `production` の設定と、Secret のロール ARN を確認する。
- `install.sh` が事前検証で失敗する: 表示された項目を解消して再実行する（何も変更されていません）。
