# 運用手順（管理者向け）

構築済みの CraftLatch を運用するための手順書です。初期構築は `docs/setup.md` を参照してください。設計の理由は `docs/superpowers/specs/2026-11-01-craftlatch-design.md` にあります。

このリポジトリは公開です。アカウント ID・メールアドレス・API/キューの URL・Cognito のドメインはどこにも書かないでください。この文書では `<ACCOUNT_ID>`、`<BUCKET>`、`<POOL_ID>`、`<NAME>` などのプレースホルダーを使います。

## 1. 日常運用

| 作業 | コマンド |
|---|---|
| サーバー状態 | `systemctl status minecraft.service` |
| サーバーログ | `journalctl -u minecraft -f` |
| 認証処理のログ | `journalctl -t craftlatch-poll -t craftlatch-apply -t craftlatch-idle --since today` |
| 許可中の IP | `sudo nft list set inet craftlatch allow4` |
| ルールとカウンタ | `sudo nft list table inet craftlatch` |
| ファイアウォールの状態 | `systemctl status craftlatch-firewall.service` |
| コンソールコマンド | `echo 'list' \| sudo -u minecraft tee /run/minecraft.console` |
| 手動停止 | `sudo systemctl stop minecraft.service` |
| 特定 IP の即時遮断 | `sudo nft delete element inet craftlatch allow4 { 203.0.113.5 }` |

- `counter drop` のカウンタが増えていれば遮断が効いています。`craftlatch-firewall.service` が `active (exited)` なら読み込み成功です。
- 認証していない回線（例: スマホのテザリング）からの遮断確認: `nc -vz -w 5 <グローバルIP> 25565`。認証前はタイムアウトし、認証の 1〜2 分後に接続できれば正常です。
- ホワイトリスト登録: Java 版は `whitelist add <名前>`、Bedrock 版は `fwhitelist add <ゲーマータグ>`（コンソールコマンドとして上の `tee` 経由で送ります）。

### Cognito ユーザーの追加・削除

`<POOL_ID>` はリポジトリのルートで `terraform -chdir=infra/bootstrap init -backend-config="bucket=<BUCKET>" -backend-config="key=bootstrap/terraform.tfstate"` の後に `terraform -chdir=infra/bootstrap output -raw user_pool_id` を実行して取得します。

追加（一時パスワードは別の経路で本人に伝え、初回ログイン時に変更させます。14 文字以上）:

```
aws cognito-idp admin-create-user --user-pool-id <POOL_ID> --username <NAME> --temporary-password '<TEMP_PASSWORD>' --message-action SUPPRESS
aws cognito-idp admin-add-user-to-group --user-pool-id <POOL_ID> --username <NAME> --group-name minecraft
```

- ユーザー名は大文字小文字を区別せず、`^[A-Za-z0-9._@+-]{1,128}$` に合うものだけを作成します。合わない名前は Lambda が SQS に送らず、エラーページ（アカウント設定の問題）になります。
- Minecraft のホワイトリストにも別途登録します（Cognito は IP を許可する仕組みで、参加者を絞るのはホワイトリストです）。

削除（無効化しただけでは、すでに登録された IP の許可は期限まで残ります）:

```
aws cognito-idp admin-user-global-sign-out --user-pool-id <POOL_ID> --username <NAME>
aws cognito-idp admin-disable-user --user-pool-id <POOL_ID> --username <NAME>
aws cognito-idp admin-delete-user --user-pool-id <POOL_ID> --username <NAME>
```

即時に遮断する場合は、上の `nft delete element` と、ホワイトリストからの削除（`whitelist remove <名前>`、Bedrock は `fwhitelist remove <ゲーマータグ>`）を行います。

## 2. 障害時の確認（設計書 8 章）

まず全体の状況を見ます。

```
systemctl --failed
journalctl -t craftlatch-poll -t craftlatch-apply -t craftlatch-idle --since today
```

障害の通知は Discord に届きます（`craftlatch-notify@.service`）。

| # | ケース | 確認コマンド | 補足 |
|---|---|---|---|
| 1 | AWS に接続できない | `journalctl -t craftlatch-poll --since today`、`cat /var/lib/craftlatch/poll-failures` | 5 回以上連続すると通知。1 分ごとに再試行。許可は増えない。メッセージは最大 10 分 SQS に残り、期限内なら復旧後に処理、超過なら破棄（利用者は再認証） |
| 2 | SQS が空 | `journalctl -t craftlatch-poll --since today` | 正常終了。対処不要 |
| 3 | 重複配信 | `journalctl -t craftlatch-apply --since today`、`ls /run/craftlatch/state/` | apply が `seen.json` の `id` で除去する。期限の再延長や停止後の再起動は起きない |
| 4 | 受信後の処理途中で失敗 | `journalctl -t craftlatch-apply -t craftlatch-poll --since today`、`sudo ls /run/craftlatch/requests/ /run/craftlatch/quarantine/` | 配置失敗時は 60 秒後に再配信。apply の nft 失敗時は要求を削除して通知（利用者は再認証）。起動ジョブの受付失敗時は許可が 10 分で失効するまで残る |
| 5 | メッセージの遅延 | `journalctl -t craftlatch-poll --since today` | 認証から 10 分を超えたものは破棄。利用者は再認証 |
| 6 | Ubuntu 再起動 | `systemctl status craftlatch-firewall.service`、`sudo nft list set inet craftlatch allow4`、`systemctl status craftlatch-poll.timer craftlatch-apply.path` | 許可リストは空、Minecraft は自動起動しない（認証で起動）。`/run` は消えるため、再起動をまたぐ要求は失われる |
| 7 | Minecraft の起動失敗 | `systemctl status minecraft.service`、`journalctl -u minecraft -n 100` | 10 分に 3 回失敗で起動を諦める（`StartLimitBurst=3`）。復旧するには `sudo systemctl reset-failed minecraft.service` の後に起動。許可 IP は残るが 10 分で失効 |
| 8 | 起動に数分かかる | `journalctl -u minecraft -f` | 起動中の接続試行は RST になる。完了後に再接続 |
| 9 | 複数ユーザーの同時認証 | `journalctl -t craftlatch-apply --since today` | apply が 1 回でまとめて処理（1 回最大 100 件） |
| 10 | 期限切れ直前の通信 | `sudo nft list set inet craftlatch allow4` | 期限後のパケットは破棄され、再認証が必要。境界は安全側 |
| 11 | 停止処理中に新しい認証 | `journalctl -t craftlatch-apply -t craftlatch-idle --since today`、`systemctl status minecraft.service` | apply が `start --no-block` を投入し、停止完了後に起動される想定（実機確認事項 U3）。起動しないままなら `sudo systemctl start minecraft.service` |
| 12 | プレイヤー不在検知の失敗 | `journalctl -t craftlatch-idle --since today`、`systemctl status craftlatch-idle.service craftlatch-idle.timer` | SLP 失敗は 0 人と同じ扱い。15 分失敗が続けば停止。無人が続く限り 5 分ごとに再要求。idle 自体の失敗は通知 |
| 13 | アクセスキーの失効・誤設定 | `journalctl -t craftlatch-poll --since today`、`cat /var/lib/craftlatch/poll-failures`、`aws iam list-access-keys --user-name craftlatch-host` | `AccessDenied` が 5 回以上続くと通知。許可は増えない。キーの再発行は 3 章 |
| 14 | Cognito / API Gateway / Lambda の障害 | AWS Health Dashboard、`aws logs tail /aws/lambda/craftlatch-auth --since 1h` | 新規認証不可。接続中のセッションは影響なし。ログには URL 等を残さない設計 |
| 15 | 時計のずれ | `timedatectl status` | `systemd-timesyncd` で同期されていること。`iat > now + 60` のメッセージは破棄される |
| 16 | 認証の連打 | API のスロットリング（1 rps、burst 5） | 起動済みなら無操作 |
| 17 | `/media/ssd` が未マウント | `findmnt /media/ssd`、`systemctl status minecraft.service` | `RequiresMountsFor=` により起動しない（起動失敗として通知） |

### apply.path の復旧

`craftlatch-apply.service` が失敗し続けると `craftlatch-apply.path` が止まり、要求ファイルが処理されなくなることがあります。状態を見て復旧します。

```
systemctl status craftlatch-apply.path craftlatch-apply.service
sudo systemctl reset-failed craftlatch-apply.service craftlatch-apply.path && sudo systemctl restart craftlatch-apply.path
```

### Minecraft が起動しない（fwcheck の失敗）

`minecraft.service` は起動前に `fwcheck` でルールの構造を検証します。次を確認します。

```
journalctl -u minecraft -n 50
sudo nft -j list table inet craftlatch
sudo systemctl status craftlatch-firewall.service
```

- ルールが読み込まれていない、または余分・欠落・順序違いのルールがあると、fwcheck が失敗して起動しません（fail-closed）。
- `config.json` の `trusted_sources`・ポート・許可期限を変えたのに雛形を取り直していない場合も失敗します（4 章）。
- ルールを読み込み直す場合は `sudo systemctl restart craftlatch-firewall.service`（Minecraft の停止と許可リストの消去を伴います）。

## 3. アクセスキーのローテーション（365 日ごと、手動）

`craftlatch-host` のアクセスキーを入れ替えます。335 日経過すると poll が警告を出します。サーバー再構築、ディスクの交換・廃棄、バックアップ流出の疑い、管理者 PC の不審があるときは、期限に関係なく更新します。

スクリプトは root で動かしますが、AWS の操作には**管理者の一時的な認証情報**を使います。`sudo` は環境変数を落とし、`HOME` も `/root` に変えるため、`AWS_PROFILE` と `HOME` の両方を引き継ぎます（`HOME` を引き継がないと、aws CLI が `/root/.aws` を探してプロファイルや SSO のキャッシュが見つかりません）。リポジトリのルートで実行します。

```
sudo --preserve-env=AWS_PROFILE,HOME host/scripts/craftlatch-rotate-key
```

`--preserve-env` が許可されていない環境では、設定ファイルの場所を明示して渡します。

```
sudo env HOME=$HOME AWS_PROFILE=<PROFILE> AWS_CONFIG_FILE=$HOME/.aws/config AWS_SHARED_CREDENTIALS_FILE=$HOME/.aws/credentials host/scripts/craftlatch-rotate-key
```

**本番サーバーで確認すること:** 実際にこの方法で管理者のプロファイルが `sudo` 下で使えるか（SSO を使う場合は、キャッシュが読まれてトークンが有効か）を、最初に `sudo --preserve-env=AWS_PROFILE,HOME aws sts get-caller-identity` で確認してからローテーションを実行してください。ローテーション後は、サーバー上の管理者用の一時認証情報を削除します。

スクリプトは新しいキーの発行と単独での検証、poll の停止と実行待ち、切り替え、新しいキーでの受信確認までを行い、確認できた時点で新しいキーを確定します。その後、`aws-credentials.rotated` の更新、`craftlatch-poll.timer` の再開、古いキーの無効化と削除を行います。秘密の値は表示しません。確定前に失敗した場合は、元の状態に戻して新しいキーを削除し、終了コード 1 で終了します。

確定後の整理（古いキーの無効化・削除など）で失敗した場合、新しいキーは維持されます。表示された作業名の原因を取り除いてから、古いキーの ID を指定して残りの整理だけを再実行します。

```
sudo --preserve-env=AWS_PROFILE,HOME host/scripts/craftlatch-rotate-key --finish <OLD_KEY_ID>
```

- `--finish` は何度実行しても同じ結果になります。古いキーがすでに削除済みの場合は完了済みとして扱います。
- **`--finish` は現用のキー（`/etc/craftlatch/aws-credentials` のキー ID）を指定すると、IAM を呼ぶ前に拒否して何もしません。** 誤って現用のキーを無効化しないための保護です。
- キー ID は `^[A-Z0-9]{16,128}$` の形式でなければなりません。

## 4. ルールの変更（`trusted_sources`・ポート・許可期限）

`config.json` の `trusted_sources`・`java_port`・`bedrock_port`・`grant_timeout_sec` を変えると、nft ルールが変わります。fwcheck の期待構造は `host/lib/fwcheck-expected.json`（インストール先は `/usr/local/lib/craftlatch/fwcheck-expected.json`）という雛形から組み立てるため、**雛形を取り直さないと fwcheck が設定と雛形の不一致で失敗し、Minecraft は起動しません**（fail-closed）。**順序が重要です。** 本番の `/etc/craftlatch/config.json` を先に編集すると、`install.sh` で新しい雛形を配置するまでの間、fwcheck が新しい設定と古い雛形を比較して失敗し、認証による Minecraft の起動がすべて失敗します（`StartLimitBurst` により 3 回で起動を諦めます）。そのため、本番の設定は**最後に**変更します。

1. リポジトリのルートで、チェックアウトの外に作業用ディレクトリを作る（`queue_url` にアカウント ID が入る写しを、誤ってコミットしないため）。表示されたパスを以降の `<WORK>` に使う。

```
mktemp -d
```

2. 現在の設定の写しを作り、ルーターの MAC だけをプレースホルダー `aa:bb:cc:dd:ee:ff` に置き換える（雛形に本物の MAC を残さないため）。

```
cp /etc/craftlatch/config.json <WORK>/config-template.json && sed -E -i 's/("exclude_ether_saddr": *")[^"]*"/\1aa:bb:cc:dd:ee:ff"/' <WORK>/config-template.json
```

3. 写し（`<WORK>/config-template.json`）の `trusted_sources`・ポート・許可期限を、変更後の値に編集する。この時点では本番の設定は変えない。

4. リポジトリのルートで、写しからルールファイルを生成する（`nftgen.py` は既定の設定パスしか読まないため、写しを読むようにモジュールを直接呼ぶ）。出力は MAC が `aa:bb:cc:dd:ee:ff` のルールになる。

```
python3 -E -s -c 'import sys; sys.path.insert(0, "host/lib"); from pathlib import Path; from common import load_config; from nftgen import render_ruleset; sys.stdout.write(render_ruleset(load_config(Path(sys.argv[1]))))' <WORK>/config-template.json > <WORK>/template.nft
```

5. 使い捨てのネットワーク名前空間に読み込み、JSON を取り出す。本番のルールには影響しない。

```
sudo unshare --net sh -c 'nft -f <WORK>/template.nft && nft -j list table inet craftlatch' > <WORK>/nft_list_table.json
```

6. リポジトリの雛形を置き換える。

```
cp <WORK>/nft_list_table.json host/lib/fwcheck-expected.json
```

7. `git diff` で内容（MAC が `aa:bb:cc:dd:ee:ff`、ポート、許可期限）を確認する。`<WORK>` の一時ファイルを削除する。`queue_url` を含む写しは絶対にコミットしない。
8. ブランチを作って雛形をコミットし、PR を作成して main にマージする（main は PR 経由のマージが必須です）。
9. サーバー上のチェックアウトで `git pull` する。
10. **ここで初めて**本番の設定を変更する（手順 11 と続けて行う）。`sudoedit /etc/craftlatch/config.json` で、手順 3 と同じ変更を加える（本物のルーター MAC は変えない）。
11. **手順 10 と 11 は続けて実行する**（間に時間を空けると、その間は fwcheck が新しい設定と古い雛形の不一致で失敗する）。`sudo host/install.sh` を実行する。雛形と新しい nft ルールが配置され、更新時の案内が表示される（5 章）。`config.json` は上書きされない。
12. 案内どおりに `sudo systemctl restart craftlatch-firewall.service` を実行する（Minecraft の停止と許可リストの消去を伴う。プレイヤーがいない時間に行う）。

## 5. `install.sh` の更新時の案内

リポジトリを更新（`git pull`）したら `sudo host/install.sh` を再実行します。稼働中のユニットは自動では再起動されません。変更の種類に応じて、実行すべきコマンドが順序付きで表示されます。

| 変更されたもの | 案内される操作 |
|---|---|
| Python スクリプト・雛形・tmpfiles.d | 操作不要（次回の実行から反映） |
| poll のユニット（`craftlatch-poll.service` / `.timer`） | `sudo systemctl restart craftlatch-poll.timer` |
| apply のユニット（`craftlatch-apply.service` / `.path`） | `sudo systemctl restart craftlatch-apply.path` |
| idle のユニット（`craftlatch-idle.service` / `.timer`） | 次回の Minecraft 起動から反映（操作不要） |
| `craftlatch-notify@.service` | 操作不要（次回の通知から反映） |
| `minecraft.service` / `minecraft.socket` | 次回の起動から反映 |
| `craftlatch-firewall.service`、または nft ルール（`craftlatch.nft`） | `sudo systemctl restart craftlatch-firewall.service` |

**警告:** ファイアウォールの再起動は、Minecraft の停止と許可リストの消去を伴います。次の認証で Minecraft が起動します（手動なら `sudo systemctl start minecraft.service`）。プレイヤーがいない時間に行ってください。

- 案内は、変更を検出した時点で `/etc/craftlatch/.install-pending`（0600）に保存されます。`install.sh` が途中で失敗しても、次回の実行で前回の検出結果が引き継がれ、案内を失いません。案内を最後まで表示した時点でこのファイルは削除されます。
- 初回の配置（`craftlatch.nft` がまだ存在しないとき）は、ユニットの `enable` の順序（`setup.md` ⑥）が表示されます。
- `sudo host/install.sh --refresh-router-mac` はルーターの MAC を再検出して `config.json` を更新します（6 章）。`--yes` は確認の入力を省略します。

## 6. ルーターの交換

ルーターを交換すると MAC が変わり、LAN 用のルールの除外条件（`exclude_ether_saddr`）が古くなります。再検出して、ルールを反映します。

```
sudo host/install.sh --refresh-router-mac
sudo systemctl restart craftlatch-firewall.service
```

- MAC は `config.json` に書き込まれ、nft ルールが再生成されます。
- 雛形は MAC をプレースホルダーとして扱うため、**MAC だけの変更では雛形を取り直す必要はありません。**
- ファイアウォールの再起動は Minecraft の停止と許可リストの消去を伴います。

## 7. 漏えい時の対応

| 対象 | 対応 |
|---|---|
| `craftlatch-host` のアクセスキー | 即時無効化 `aws iam update-access-key --user-name craftlatch-host --access-key-id <KEY_ID> --status Inactive`。無効化した直後は poll が `AccessDenied` になるため、続けて 3 章の手順で再発行する（IAM のアクセスキーは 1 ユーザー 2 本までで、無効化したキーも 1 本に数えるため、ローテーションの途中で上限に当たる場合は、無効化済みの漏えいキーを `aws iam delete-access-key --user-name craftlatch-host --access-key-id <KEY_ID>` で先に削除する） |
| Discord Webhook URL | Discord 側で Webhook を削除・再作成し、`/etc/craftlatch/discord-webhook`（0400）を更新する |
| Cognito ユーザーのパスワード | `aws cognito-idp admin-user-global-sign-out --user-pool-id <POOL_ID> --username <NAME>` と `aws cognito-idp admin-disable-user --user-pool-id <POOL_ID> --username <NAME>`。必要に応じて `nft delete element`（1 章）とホワイトリストの削除 |

検知の制約: SQS の受信・削除は CloudTrail のデータイベントで、既定では記録されません。確認できるのは IAM の「アクセスキー最終使用」（`aws iam get-access-key-last-used --access-key-id <KEY_ID>`）と、CloudTrail イベント履歴の管理イベント（90 日・無料）です。

### 脅威の前提: GitHub 管理者アカウントの保護

GitHub Actions の apply ロール（`craftlatch-gha-apply`）、または main ブランチと `production` の承認関門を制御できる者は、許可の偽造が可能です。このロールは `UpdateFunctionCode`（Lambda のコードを差し替えて任意の IP を SQS に送らせる）、キューポリシーの変更、信頼ポリシーの変更の権限を持つためです。

Cognito については、apply ロールは User Pool 配下（アプリクライアントのコールバック URL・ドメイン・グループ・ブランディング）を変更できますが、User Pool 本体（パスワードポリシー・サインアップの設定・削除保護・タグ）の作成・変更・削除・タグ付けは明示的に拒否されています。他のシステムの User Pool をタグ付けで管理下に入れることもできません。User Pool 本体を変更できるのは bootstrap を実行する管理者の認証情報だけです。

- 管理者の GitHub アカウントを保護してください（2 要素認証のパスキー、端末の保護）。承認は第三者のレビューではなく、自分で内容を確認する関門です。
- 管理者が保護ルールを変更できるため、管理者アカウントの侵害にはこの関門は無力です。
- 不審な Lambda の更新や IAM の変更がないか、CloudTrail の管理イベント（`UpdateFunctionCode`、`SetQueueAttributes`、`UpdateAssumeRolePolicy`）を定期的に確認すると検知できます。

## 8. Paper・プラグインの更新

Paper:

1. ワールドのバックアップを取る（DriveBackupV2 の手動実行など）。
2. Minecraft を停止する: `sudo systemctl stop minecraft.service`。
3. 新しい jar を `/media/ssd/paper/` に置き、リンクを切り替える。

```
sudo -u minecraft ln -sfn paper-<ver>-<build>.jar /media/ssd/paper/paper.jar
```

4. 次の認証で起動するか、手動で `sudo systemctl start minecraft.service`。起動ログ（`journalctl -u minecraft -f`）で問題がないことを確認する。古い jar は、動作確認の後に整理してよい。

プラグイン（Geyser・Floodgate・Multiverse・DriveBackupV2 など）:

- 停止した状態で `/media/ssd/paper/plugins/` の jar を差し替える。ファイルの所有者は `minecraft` にする。
- Geyser の設定: `bedrock.port: 19132`、`bedrock.address: 0.0.0.0`、`remote.auth-type: floodgate`。ポートを変える場合は 4 章の手順も必要。
- DriveBackupV2 は、サーバーの稼働中にしか動かない。稼働時間が短いと定期バックアップの時刻に一度も稼働しないことがあるため、間隔（例: 稼働中 60 分ごと）やプレイヤーの参加・退出時のバックアップを確認する（未検証事項 U12）。無人停止の際にアップロード中だと `stop` で中断されうる。

## 9. インフラ（Terraform）の変更

通常の変更は PR を作り、`terraform-plan` の結果（リソースの作成・変更・削除の件数）を確認して main にマージし、`production` の承認で apply します。

- 公開ログに出るのは件数・コミット SHA・失敗したコマンド名と終了コードだけです。失敗の原因は、手元で同じコミットに対して `terraform plan` を実行して調べます（リポジトリのルートで `terraform -chdir=infra/terraform init -backend-config="bucket=<BUCKET>"` の後に `terraform -chdir=infra/terraform plan`）。
- `infra/bootstrap`（OIDC プロバイダー・GHA ロール・Boundary・User Pool）は、管理者が手元で適用します（`setup.md` ②。既存の OIDC プロバイダーを使っている場合は毎回 `existing_github_oidc_provider_arn` を渡します。復旧などで手順をやり直すとき、`terraform -chdir=infra/bootstrap state list` に `aws_iam_openid_connect_provider.github[0]` が出るなら、そのプロバイダーは bootstrap 自身のものなので ARN を渡さず、既存のプロバイダーを使う方式に意図して切り替える場合だけ先に `terraform -chdir=infra/bootstrap state rm 'aws_iam_openid_connect_provider.github[0]'` を実行します）。変更の後は、`workflow_dispatch` で apply ワークフローを実行し、ロールを引き受けられて差分がないことを確認します。
- User Pool の設定（パスワードポリシーなど）の変更は、PR ではなく管理者が `infra/bootstrap/cognito.tf` を変更して bootstrap を適用します（apply ロールには User Pool 本体の変更権限がありません）。User Pool には削除保護が付いています。削除が必要な場合だけ、`deletion_protection` を `INACTIVE` にして bootstrap を適用してから削除します（ユーザーもすべて消えます）。
- プロバイダーの更新は、`terraform providers lock -platform=linux_amd64 -platform=darwin_arm64` を両方のルート（`infra/bootstrap`、`infra/terraform`）でやり直し、明示的な PR で行います。
- `queue_url` などの出力は手元の `terraform -chdir=infra/terraform output -raw <名前>` で確認し、CI のログに出力しません。
