# CraftLatch 詳細設計書 — Minecraft サーバー Cognito 認証ゲート

- 作成日: 2026-11-01
- 対象リポジトリ: `Knu334-Inc/CraftLatch`
- ステータス: 設計レビュー待ち（実装コードの作成・AWS/Ubuntu 環境の変更は本書の範囲外）

---

## 1. 目的とスコープ

自宅 Ubuntu サーバーで稼働する Minecraft サーバー（Paper + Geyser + Floodgate）について、Amazon Cognito による認証に成功したグローバル IPv4 アドレスからのみ Java 版（TCP/25565）・Bedrock 版（UDP/19132）へ到達できるようにする。あわせて認証をトリガーにサーバーを自動起動し、無人状態が続いたら正常停止する。

重視する順序: セキュリティ ＞ AWS 利用料金 ＞ リソース消費 ＞ 運用負荷 ＞ 実装のシンプルさ。

### 1.1 確定した前提

| 項目 | 内容 |
|---|---|
| サーバー | Paper（Java Edition）+ Geyser-Spigot + Floodgate。`/media/ssd/paper/` 配下（`/etc/fstab` による常時マウント） |
| 公開ポート | ルーターで IPv4 の TCP/25565 と UDP/19132 をポートフォワード |
| IPv6 | Ubuntu では無効 |
| UFW | 無効。ホストのフィルタは本設計の nftables テーブルのみ |
| 既存起動方式 | sudoers ユーザーで `minecraft.service` を起動中 → 専用ユーザーへ移行する |
| TPM 2.0 | なし |
| AWS リージョン | ap-northeast-1（仮定） |
| 利用規模 | 数名、認証 1 日 5 回程度 |
| 実装言語 | Python（Lambda・Ubuntu とも） |
| IaC | Terraform（state は S3 バックエンド） |
| 障害通知 | Discord Webhook |

### 1.2 要件の解釈（合意済み）

- 「通信のたびに 10 分延長」により、**接続中は失効しない**。失効は実質「最後のパケットから 10 分後」（切断後 10 分）。プレイ中の強制再認証は行わない。
- 「失効後は再度 Cognito 認証が必要」は、**毎回 ID/パスワード入力を強制**する（`prompt=login` + `auth_time` 検証）。
- MFA は使用しない。

---

## 2. 現行構成案のレビュー結果

| # | 指摘 | 内容 | 対応 |
|---|---|---|---|
| P1 | ipset の単純マッチでは期限が延長されない | `-m set --match-set` は照合のみ。延長には `-j SET --add-set --exist` が別途必要。Ubuntu の iptables は nft バックエンドで ipset と二系統になる | nftables ネイティブ set の `update @set { ip saddr timeout 10m }` を採用 |
| P2 | 接続中は失効しない | Minecraft プロトコルの KeepAlive（約 15 秒）やゲームパケットで常時延長される。OS の TCP KeepAlive（既定無効/2 時間）は無関係 | 要件解釈として明記（1.2） |
| P3 | ESTABLISHED による迂回 | 一般的なルールセットは ESTABLISHED を先に許可する | 専用チェインで ct state に関係なく未許可送信元を drop |
| P4 | 認証 IP と接続 IP のファミリー不一致 | ブラウザは IPv6 を優先し、Java クライアントは IPv4 を優先しがち。Lambda Function URL はデュアルスタック | 入口は IPv4 のみの API Gateway HTTP API（既定 IPv4）を採用。Function URL は不採用 |
| P5 | 遅延した古い許可イベント | 数時間後に処理すると、その IP が既に他人のものである可能性 | メッセージに `exp` を持たせ、SQS 保持期間も 600 秒に短縮 |
| P6 | DLQ が実質無意味 | Standard キューでは DLQ 移動後も元の投入時刻で期限切れになる | DLQ なし。不正メッセージはログ出力後に削除 |
| P7 | SIGTERM での保存が保証されない | バニラ/派生サーバーで SIGTERM 時に保存されない報告あり（公式記述なし） | FIFO 経由で stdin に `stop` を送る `ExecStop` |
| P8 | sudo と `NoNewPrivileges` の両立不可 | 受信プロセスを堅牢化すると sudo が使えない | 要求ファイル + path unit + root の apply service に分離 |
| P9 | Cognito のセッションで再認証が素通りになる | Managed Login のセッションクッキーで入力なし成功 | `prompt=login` + Lambda 側で `auth_time` を検証 |
| P10 | 同一グローバル IP の共有者も到達できる | CGNAT・公衆 Wi-Fi・同居人 | ホワイトリスト + `online-mode` + Floodgate を本当のアカウント認可とする |
| P11 | 「プラグイン原則不使用」と実環境の相違 | 実際は Paper + Geyser + Floodgate。Bedrock は UDP/19132 で、Geyser は直接接続のため TCP 接続として見えない | UDP/19132 も同じ許可リストで制御。人数取得は SLP（Paper の `players.online` に Bedrock も含まれる） |
| P12 | 既存サーバーが sudoers ユーザーで稼働 | Paper/Geyser の RCE がホスト全体の侵害に直結 | 専用ユーザー `minecraft`（nologin、sudo 不可）へ移行 |
| P13 | ブラウザのプライバシー機能による IP ずれ | iCloud プライベートリレー・ブラウザの IP 保護・VPN 使用時は中継 IP が登録される | 利用者向け案内に記載し、完了ページに登録 IP を表示 |

### 2.1 技術選定の結果

| 領域 | 採用 | 退けた案と理由 |
|---|---|---|
| 認証 | Cognito User Pool（Essentials）+ Managed Login | Lite は Managed Login と `prompt` 非対応。Plus は無料枠なし |
| 入口 | API Gateway HTTP API（IPv4） | Lambda Function URL: デュアルスタックで P4 が発生。REST API: 高価で機能過剰。ALB: 月額固定費 |
| 処理 | Lambda（Python 3.13、arm64、標準ライブラリ + 同梱 boto3） | Java + SnapStart: 体感速度は人の入力・ポーリング待ち・MC 起動時間が支配的で効果がない。ビルドが重く、スナップショット復元時に乱数の一意性の落とし穴がある。TypeScript: Ubuntu 側に Node.js と npm 依存が増える |
| 受け渡し | SQS Standard（DLQ なし） | FIFO: 処理が冪等なので不要 |
| AWS 認証情報 | IAM ユーザーのアクセスキー（受信・削除のみ） | IAM Roles Anywhere: TPM がない環境では秘密鍵がファイルとなり、アクセスキーと比べて安全性の差が小さい一方、CA・トラストアンカー・プロファイル・署名ヘルパー・証明書更新の運用が増える |
| 特権分離 | 要求ファイル + path unit + root の apply service | sudo + ヘルパー: `NoNewPrivileges` 不可、setuid が攻撃面になる。capability + polkit: `CAP_NET_ADMIN` をネットワーク入力処理プロセスに与えることになる |
| ファイアウォール | nftables 専用テーブル | iptables + ipset: P1 |
| 人数取得 | localhost への Server List Ping | RCON: bind 先を絞れずパスワード管理が必要。`/proc/net/tcp`: Bedrock 版プレイヤーを数えられない |
| 停止 | systemd の FIFO ソケットで stdin に `stop` | SIGTERM のみ: P7 |
| 定期実行 | systemd timer + oneshot | 常駐の長時間ポーリング: 常駐プロセスが増える |
| 通知 | systemd `OnFailure=` → Discord Webhook | SNS: Ubuntu のキーに `sns:Publish` の権限が増える |

---

## 3. 全体構成

```
[ブラウザ] ──HTTPS(IPv4のみ)──> [API Gateway HTTP API: craftlatch-api]
     │                                   │
     │                                   ▼
     │                         [Lambda: craftlatch-auth]
     │                           /login    : state・nonce・PKCE を生成し Cookie に保存 → authorize へ 302
     └──> [Cognito Managed Login]          /callback : state 照合、トークン交換、クレーム検証、sourceIp 取得
          (craftlatch-users, prompt=login)   │
                                             ▼ SendMessage
                                   [SQS Standard: craftlatch-grants]
                                             ▲ ReceiveMessage / DeleteMessage（IAM ユーザー craftlatch-host）
─────────────── 自宅 Ubuntu ──────────────────┼────────────────────────────────
 craftlatch-poll.timer (毎分) → craftlatch-poll.service [craftlatch / 特権なし]
                                   │ 検証済み要求を配置
                                   ▼
                        /run/craftlatch/requests/*.req
                                   ▲                         │ craftlatch-apply.path が検知
 craftlatch-idle.timer (MC 稼働中のみ毎分)                    ▼
   → craftlatch-idle.service [craftlatch]          craftlatch-apply.service [root / CAP_NET_ADMIN のみ]
     SLP で人数取得、15 分無人なら stop 要求          ├─ nft: allow4 に IP を登録（timeout 10m）
                                                     └─ systemctl start|stop --no-block minecraft.service
 nftables table inet craftlatch:
   TCP/25565・UDP/19132 → allow4 に登録済みなら update(10m) して accept / それ以外は drop
 minecraft.service [minecraft]: stdin = minecraft.socket (FIFO)、ExecStop で "stop" を書き込む
 各ユニットの OnFailure= → craftlatch-notify@.service → Discord Webhook
```

### 3.1 正常時のシーケンス

1. 利用者が `https://<api-id>.execute-api.ap-northeast-1.amazonaws.com/login` を開く。
2. Lambda が state・nonce・code_verifier を生成し、`__Host-craftlatch` Cookie に保存して Cognito の authorize エンドポイントへ 302 リダイレクトする（`prompt=login`、`code_challenge_method=S256`）。
3. 利用者がユーザー名とパスワードを入力し、`/callback?code=...&state=...` に戻る。
4. Lambda が state を照合し、トークン交換と ID トークンのクレーム検証を行い、`requestContext.http.sourceIp` を取得・検証して SQS に許可イベントを送信する。完了ページに登録 IP を表示する。
5. 最大 60 秒以内に poll service がメッセージを受信・検証し、要求ファイルを置いてから SQS から削除する。
6. path unit が apply service を起動し、apply が要求を再検証して nft の set に IP を登録し、`minecraft.service` を起動する（起動済みなら何もしない）。
7. 以降、許可 IP からのパケットが届くたびに期限が 10 分延長される。最後のパケットから 10 分で自動失効する。
8. Minecraft 稼働中は idle service が毎分人数を取得し、15 分連続で 0 人なら stop 要求を置き、apply が正常停止させる。

### 3.2 命名規則

接頭辞 `craftlatch-`（GitHub リモート `Knu334-Inc/CraftLatch` に由来）。AWS タグは `Project=CraftLatch`。

| 対象 | 名前 |
|---|---|
| User Pool / アプリクライアント / グループ | `craftlatch-users` / `craftlatch-web` / `minecraft` |
| Cognito ドメイン | `craftlatch-<ランダム文字列>` |
| HTTP API / Lambda / Lambda ロール | `craftlatch-api` / `craftlatch-auth` / `craftlatch-auth-role` |
| SQS | `craftlatch-grants` |
| Ubuntu 用 IAM ユーザー | `craftlatch-host` |
| Terraform state バケット | `craftlatch-tfstate-<account_id>` |
| systemd ユニット | `craftlatch-firewall` / `craftlatch-poll` / `craftlatch-apply` / `craftlatch-idle` / `craftlatch-notify@` / `minecraft` |
| nftables | `table inet craftlatch`、set `allow4` |
| Linux ユーザー | `minecraft`（サーバー本体）、`craftlatch`（受信・無人検知） |

### 3.3 リポジトリ構成（実装時）

```
infra/terraform/          versions.tf backend.tf variables.tf cognito.tf api.tf lambda.tf sqs.tf iam.tf budgets.tf outputs.tf
src/lambda/craftlatch_auth/handler.py
host/lib/                 common.py poll.py apply.py idle.py notify.py mc-stop
host/systemd/             *.service *.timer *.path *.socket
host/nftables/craftlatch.nft
host/tmpfiles.d/craftlatch.conf
host/scripts/craftlatch-rotate-key
tests/                    Lambda と host/lib の単体テスト
```

---

## 4. AWS 側の設計

### 4.1 Cognito User Pool `craftlatch-users`

| 項目 | 設定 | 理由 |
|---|---|---|
| プラン | Essentials | Managed Login と `prompt=login` に必要。10,000 MAU まで無料 |
| サインイン識別子 | ユーザー名 | メール属性は不要 |
| サインアップ | 管理者のみ（`AllowAdminCreateUserOnly=true`） | 第三者が自分でアカウントを作れないようにする |
| MFA | OFF | 合意事項 |
| パスワードポリシー | 最小 14 文字（文字種の条件は任意） | MFA がないため長さで補う |
| アカウント復旧 | 管理者リセットのみ | メール送信の設定が不要。メールアカウント侵害で突破されない |
| ユーザー存在エラーの秘匿 | 有効 | ユーザー名の列挙を防ぐ |
| グループ | `minecraft` | 所属しないユーザーは認証に成功しても拒否 |
| ドメイン | Cognito プレフィックスドメイン | 無料。独自ドメインは使わない |
| Managed Login ブランディング | 既定スタイル（`aws_cognito_managed_login_branding`） | Managed Login の利用に必要 |

アプリクライアント `craftlatch-web`:

- パブリッククライアント（クライアントシークレットなし）+ PKCE。シークレットの保管が不要で、トークン取得には認可コードと code_verifier の両方が必要。
- 許可フロー: `code` のみ。スコープ: `openid` のみ。
- コールバック URL: `https://<api-id>.execute-api.ap-northeast-1.amazonaws.com/callback` の 1 件のみ。ログアウト URL なし。
- ID・アクセストークン有効期間: 5 分（最短）。リフレッシュトークン: 60 分（最短）。リフレッシュトークンは Lambda で使わずに破棄する。

ユーザー追加手順（管理者）:

```
aws cognito-idp admin-create-user --user-pool-id <POOL_ID> --username <NAME> --temporary-password '<TEMP_PASSWORD>' --message-action SUPPRESS
aws cognito-idp admin-add-user-to-group --user-pool-id <POOL_ID> --username <NAME> --group-name minecraft
```

一時パスワードは別の経路で本人に伝え、初回ログイン時に変更させる。

### 4.2 API Gateway HTTP API `craftlatch-api`

- IP アドレスタイプ: IPv4（既定）。**デュアルスタックにしてはならない**（P4）。
- ルート: `GET /login`、`GET /callback` のみ。`$default` ルートは作らない（他のパスは 404）。
- 統合: Lambda プロキシ統合、ペイロード形式 2.0。
- ステージ `$default`、自動デプロイ有効、スロットリング: burst 5、rate 1 リクエスト/秒。
- アクセスログ: 無効（Lambda のログで足りる）。

### 4.3 Lambda `craftlatch-auth`

| 項目 | 値 |
|---|---|
| ランタイム | Python 3.13、arm64 |
| メモリ / タイムアウト | 128 MB / 10 秒 |
| 依存 | 標準ライブラリ + ランタイム同梱の boto3 のみ（ZIP を `archive_file` で作成） |
| ログ | CloudWatch Logs、保持 30 日 |

環境変数:

| 名前 | 例 |
|---|---|
| `COGNITO_DOMAIN` | `craftlatch-xxxx.auth.ap-northeast-1.amazoncognito.com` |
| `CLIENT_ID` | アプリクライアント ID |
| `ISSUER` | `https://cognito-idp.ap-northeast-1.amazonaws.com/<POOL_ID>` |
| `REDIRECT_URI` | `https://<api-id>.execute-api.ap-northeast-1.amazonaws.com/callback` |
| `QUEUE_URL` | `craftlatch-grants` の URL |
| `ALLOWED_GROUP` | `minecraft` |
| `MAX_AUTH_AGE` | `300` |
| `GRANT_TTL` | `600` |

#### `/login`

1. `secrets.token_urlsafe(32)` で state・nonce・code_verifier を生成する。
2. Cookie `__Host-craftlatch` に `{"s":state,"n":nonce,"v":verifier,"t":発行時刻}` を base64url で格納する。属性: `HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=600`。
   - Cookie に署名は付けない。改ざんしても影響は本人の認証フローにとどまり、`__Host-` 接頭辞により他オリジンからは設定できないため。
3. `https://<COGNITO_DOMAIN>/oauth2/authorize?response_type=code&client_id=...&redirect_uri=...&scope=openid&state=...&nonce=...&code_challenge=<S256>&code_challenge_method=S256&prompt=login` へ 302 リダイレクトする。

#### `/callback`

1. Cookie がなければ「セッションが期限切れです。最初からやり直してください」を返す。`error` パラメータがあればエラーページを返す。
2. `state` パラメータと Cookie の state を `hmac.compare_digest` で比較する。
3. `https://<COGNITO_DOMAIN>/oauth2/token` に `grant_type=authorization_code`、`client_id`、`code`、`redirect_uri`、`code_verifier` を POST する（`urllib.request`、タイムアウト 5 秒）。
4. ID トークンを検証する。
   - 署名検証は行わない。OIDC Core 3.1.3.7 (6) により、トークンエンドポイントから TLS で直接受け取った ID トークンは TLS のサーバー検証で代替できる。代替案（Node.js + `aws-jwt-verify` による署名検証）は依存が増えるため不採用。
   - `iss == ISSUER`、`aud == CLIENT_ID`、`token_use == "id"`、`exp > now`、`nonce == Cookie の nonce`、`now - auth_time <= MAX_AUTH_AGE`、`ALLOWED_GROUP in cognito:groups` をすべて確認する。
5. `requestContext.http.sourceIp` を `ipaddress.ip_address()` で解析し、`IPv4Address` かつ `is_global` であることを確認する。`X-Forwarded-For` 等のヘッダーは使わない。
6. SQS に送信する: `{"v":1,"type":"allow","ip":"203.0.113.5","iat":<now>,"exp":<now+GRANT_TTL>,"id":"<uuid4>","user":"<cognito:username>"}`
7. 完了ページ（登録 IP、「最大 1 分 + サーバー起動時間をお待ちください」）を返し、Cookie を削除する。

#### レスポンス形式

すべて `statusCode` と `content-type` を明示して返す（省略すると `application/json` 扱いになる）。HTML は JavaScript を含まない最小構成とする。

```json
{"statusCode": 200, "headers": {"content-type": "text/html; charset=utf-8", "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'", "cache-control": "no-store", "referrer-policy": "no-referrer", "x-content-type-options": "nosniff"}, "cookies": ["__Host-craftlatch=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Lax"], "body": "<!doctype html>..."}
```

エラー時も同じ形式とし、画面には大まかな理由のみ表示し、詳細は CloudWatch Logs に残す。表示する理由の区分は「期限切れ・処理済み（再ログインしてください）」「権限がありません」「IPv4 以外からのアクセスです」「一時的なエラー（再試行してください）」の 4 種類。

#### ログ

`event=grant result=ok user=alice ip=203.0.113.5 id=...` のような key=value の 1 行形式。トークン・認可コード・Cookie の値は出力しない。

### 4.4 SQS `craftlatch-grants`

| 属性 | 値 | 理由 |
|---|---|---|
| 種類 | Standard | 処理が冪等なので FIFO は不要 |
| MessageRetentionPeriod | 600 秒 | 許可イベントの有効期間と揃え、古いイベントを自動削除する |
| VisibilityTimeout | 60 秒 | 受信から削除までは数秒 |
| ReceiveMessageWaitTimeSeconds | 0（クライアントが 1 を指定） | |
| 暗号化 | SSE-SQS | 無料 |
| DLQ | なし | P6 |

キューポリシー: TLS を使わないアクセス（`aws:SecureTransport = false`）を Deny する文のみ。送受信の主体の制限は IAM ポリシーで行う。

### 4.5 IAM

`craftlatch-auth-role`（Lambda 実行ロール）:

- 信頼: `lambda.amazonaws.com`
- 許可: 自分のロググループへの `logs:CreateLogStream`・`logs:PutLogEvents`、`craftlatch-grants` への `sqs:SendMessage`

`craftlatch-host`（IAM ユーザー、コンソールログイン不可）:

```json
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["sqs:ReceiveMessage","sqs:DeleteMessage"],"Resource":"arn:aws:sqs:ap-northeast-1:<ACCOUNT_ID>:craftlatch-grants"}]}
```

- アクセスキーは Terraform では作らない（state にシークレットが残るため）。発行は CLI で行う: `aws iam create-access-key --user-name craftlatch-host`
- このキーが漏えいしても、許可イベントの偽造（`SendMessage`）はできない。被害は「利用者名と IP の閲覧」と「許可イベントの削除による妨害」に限られる。

### 4.6 AWS Budgets

月額 $1 のコスト予算を作成し、実績が 100% を超えたら管理者のメールに通知する（Terraform の `aws_budgets_budget`）。

### 4.7 Terraform

- Terraform 1.10 以上（S3 ネイティブロック `use_lockfile` のため）。AWS プロバイダーは実装時点の最新メジャーに固定する。
- ルートモジュール 1 つ。モジュール分割はしない。
- 変数: `region`、`account_id`、`project`（既定 `craftlatch`）、`budget_email`、`cognito_domain_prefix`。
- 出力: ログイン URL、User Pool ID、キュー URL、`craftlatch-host` の ARN。
- 実装時に確認する事項: Cognito のプラン指定（`user_pool_tier`）と HTTP API の `ip_address_type` の引数名・対応プロバイダーバージョン。

state バケットの事前作成（Terraform 実行前に 1 回だけ）:

```
aws s3api create-bucket --bucket craftlatch-tfstate-<ACCOUNT_ID> --region ap-northeast-1 --create-bucket-configuration LocationConstraint=ap-northeast-1
aws s3api put-bucket-versioning --bucket craftlatch-tfstate-<ACCOUNT_ID> --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket craftlatch-tfstate-<ACCOUNT_ID> --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

既定の暗号化は SSE-S3（新規バケットの既定）。`backend.tf`:

```hcl
terraform {
  backend "s3" {
    bucket       = "craftlatch-tfstate-<ACCOUNT_ID>"
    key          = "craftlatch/terraform.tfstate"
    region       = "ap-northeast-1"
    encrypt      = true
    use_lockfile = true
  }
}
```

state に秘密情報は含めない（アクセスキー・Discord Webhook URL は Terraform 管理外）。

---

## 5. Ubuntu 側の設計

### 5.1 ユーザー

```
sudo useradd --system --home-dir /media/ssd/paper --shell /usr/sbin/nologin minecraft
sudo useradd --system --no-create-home --shell /usr/sbin/nologin craftlatch
```

どちらもログイン不可・sudo 不可。

### 5.2 ディレクトリと権限

| パス | 所有者:グループ | 権限 | 内容 |
|---|---|---|---|
| `/media/ssd/paper/` | minecraft:minecraft | 0750 | サーバー本体・ワールド・plugins・設定。Paper が `cache/`・`libraries/`・`plugins/` に書き込むため全体を minecraft 所有とする（jar のみ root 所有にしても、ディレクトリ所有者は置換できるため保護にならない） |
| `/media/ssd/paper/paper.jar` | minecraft:minecraft | シンボリックリンク | 実体 `paper-<ver>-<build>.jar` へのリンク |
| `/usr/local/lib/craftlatch/` | root:root | 0755（各ファイル 0755/0644） | `common.py` `poll.py` `apply.py` `idle.py` `notify.py` `mc-stop` |
| `/etc/craftlatch/config.json` | root:root | 0644 | キュー URL、リージョン、ポート、タイムアウト値（秘密情報なし） |
| `/etc/craftlatch/aws-credentials` | root:root | 0400 | アクセスキー（AWS 共有クレデンシャル形式、プロファイル `default`） |
| `/etc/craftlatch/aws-credentials.rotated` | root:root | 0644 | キー更新日（`YYYY-MM-DD`） |
| `/etc/craftlatch/discord-webhook` | root:root | 0400 | Discord Webhook URL |
| `/etc/craftlatch/craftlatch.nft` | root:root | 0644 | nftables ルール |
| `/run/craftlatch/` | root:root | 0755 | |
| `/run/craftlatch/requests/` | root:craftlatch | 1770 | 要求ファイル |
| `/run/craftlatch/quarantine/` | root:root | 0700 | 不正な要求ファイルの隔離先 |
| `/var/lib/craftlatch/` | craftlatch:craftlatch | 0700 | idle の状態ファイル（`StateDirectory=`） |
| `/var/lib/private/craftlatch-notify/` | systemd 管理 | — | notify の送信履歴（`DynamicUser=` + `StateDirectory=`） |

`/etc/tmpfiles.d/craftlatch.conf`:

```
d /run/craftlatch 0755 root root -
d /run/craftlatch/requests 1770 root craftlatch -
d /run/craftlatch/quarantine 0700 root root -
```

`requests/` を root 所有 1770 にする理由: apply service は `CAP_DAC_OVERRIDE` を持たない root として動く。ディレクトリ所有者であれば中のファイルを一覧・削除でき、ファイルを 0644 で作れば読める。`craftlatch` はグループ書き込み権限でファイルを置ける。その他のローカルユーザーは入れない。`/run` は tmpfs のため、再起動で未処理の要求は消える（fail-closed）。

`/etc/craftlatch/config.json` の例:

```json
{"region": "ap-northeast-1", "queue_url": "https://sqs.ap-northeast-1.amazonaws.com/<ACCOUNT_ID>/craftlatch-grants", "java_port": 25565, "bedrock_port": 19132, "grant_timeout_sec": 600, "idle_stop_sec": 900, "slp_timeout_sec": 3, "key_warn_days": 335}
```

### 5.3 要求ファイルの仕様

- ファイル名: `<epoch>-<uuid>.req`。`.tmp-<uuid>` で書き込み、`fsync` 後に `rename` で改名する（path unit は `*.req` のみに反応する）。
- 権限: 0644、所有者 `craftlatch`。
- 内容: 1 行の JSON、1024 バイト以下。
  - `{"v":1,"action":"allow","ip":"203.0.113.5","exp":1793460600,"id":"<uuid>","user":"alice"}`
  - `{"v":1,"action":"stop","reason":"idle"}` または `"reason":"probe-failure"`

### 5.4 `craftlatch-poll.timer` / `craftlatch-poll.service`

```ini
# craftlatch-poll.timer
[Unit]
Description=CraftLatch SQS poll timer

[Timer]
OnCalendar=minutely
AccuracySec=1s
Persistent=false

[Install]
WantedBy=timers.target
```

```ini
# craftlatch-poll.service
[Unit]
Description=CraftLatch SQS poll
After=network-online.target
Wants=network-online.target
OnFailure=craftlatch-notify@%n.service

[Service]
Type=oneshot
User=craftlatch
Group=craftlatch
ExecStart=/usr/bin/python3 -I /usr/local/lib/craftlatch/poll.py
LoadCredential=aws:/etc/craftlatch/aws-credentials
Environment=AWS_SHARED_CREDENTIALS_FILE=%d/aws
Environment=AWS_CONFIG_FILE=/dev/null
StateDirectory=craftlatch
TimeoutStartSec=50s
SyslogIdentifier=craftlatch-poll
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/run/craftlatch/requests
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
CapabilityBoundingSet=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
SystemCallFilter=@system-service
SystemCallArchitectures=native
MemoryMax=128M
CPUQuota=50%
```

前回の実行が終わっていなければ timer は次の実行を起動しないため、多重実行は起きない。

`poll.py` の処理:

1. `aws-credentials.rotated` の日付から `key_warn_days`（335 日）を超えていれば journal に警告を出す。
2. `ReceiveMessage(MaxNumberOfMessages=10, WaitTimeSeconds=1)` を呼ぶ。メッセージが返る間は繰り返す（最大 5 回）。
3. 各メッセージについて、本文が 1024 バイト以下の JSON で、`v == 1`、`type == "allow"`、`ip` が `IPv4Address` かつ `is_global`、`now < exp`、`exp - iat <= 900`、`iat <= now + 60`、`id` が UUID 形式、`user` が `^[A-Za-z0-9._@+-]{1,128}$` であることを確認する。
4. 有効なら要求ファイルを置き、その後 `DeleteMessage` する。無効ならログ出力後に `DeleteMessage` する。
5. 要求ファイルの配置に失敗した場合は削除しない（60 秒後に再配信される）。
6. AWS への接続エラー・認証エラーは、連続失敗回数を `/var/lib/craftlatch/poll-failures` に記録する。5 回連続に達したときのみ終了コード 1 で終了し（`OnFailure=` 発火）、それ未満は警告ログのみで終了コード 0 とする。成功時にカウンタを 0 に戻す。

依存パッケージ: `python3-boto3`（Ubuntu 公式 apt パッケージ。セキュリティ更新は apt で受ける）。

### 5.5 `craftlatch-apply.path` / `craftlatch-apply.service`

```ini
# craftlatch-apply.path
[Unit]
Description=CraftLatch request watcher

[Path]
PathExistsGlob=/run/craftlatch/requests/*.req
Unit=craftlatch-apply.service

[Install]
WantedBy=paths.target
```

```ini
# craftlatch-apply.service
[Unit]
Description=CraftLatch apply requests
Requires=craftlatch-firewall.service
After=craftlatch-firewall.service
OnFailure=craftlatch-notify@%n.service

[Service]
Type=oneshot
User=root
ExecStart=/usr/bin/python3 -I /usr/local/lib/craftlatch/apply.py
SyslogIdentifier=craftlatch-apply
CapabilityBoundingSet=CAP_NET_ADMIN
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/run/craftlatch
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
IPAddressDeny=any
RestrictAddressFamilies=AF_UNIX AF_NETLINK
SystemCallFilter=@system-service
SystemCallArchitectures=native
```

- `PrivateNetwork=` は使わない（ネットワーク名前空間が分離され、nft が別の名前空間を操作してしまうため）。
- `systemctl` は D-Bus 経由で PID 1 に依頼する。uid 0 であれば capability なしで許可される。

`apply.py` の処理（1 回の実行で、その時点の要求をすべてまとめて処理する。最大 100 件）:

1. `requests/` 内の `*.req` を列挙する。各ファイルを `O_RDONLY | O_NOFOLLOW` で開き、`fstat` で通常ファイル・所有者 uid が `craftlatch`・サイズ 1024 バイト以下であることを確認し、JSON を検証する（`action` が `allow` なら poll と同じ検証、`stop` なら `reason` が既定値のいずれか）。不合格のファイルは `quarantine/` へ移動する（隔離先は最大 100 件。超えたら古いものから削除）。
2. allow 要求がある場合:
   - IP ごとに `add element inet craftlatch allow4 { <ip> }` → `delete element inet craftlatch allow4 { <ip> }` → `add element inet craftlatch allow4 { <ip> timeout 10m }` を生成し、すべてを 1 回のトランザクションとして `nft -f -` の標準入力に渡す。最初の `add` は未登録時に `delete` が失敗しないようにするため。トランザクションなので接続中の通信は途切れない。IP 文字列は `ipaddress.IPv4Address` で解析し直したものだけを使う。
   - `systemctl start --no-block minecraft.service` を実行する（起動済みなら何もしない）。
   - 同じ回に stop 要求があっても無視する（allow 優先）。
3. stop 要求のみの場合: `systemctl stop --no-block minecraft.service` を実行する。
4. 処理したファイルは成否にかかわらず削除する（失敗時は隔離）。必ず片付けることで、path unit が起動を繰り返す事態を防ぐ。
5. 外部コマンドは `subprocess.run([...], shell=False, timeout=10)` で呼ぶ。
6. ログ: `event=allow ip=... user=... id=... result=ok`、`event=stop reason=idle result=ok`。
7. nft または systemctl が失敗した場合は終了コード 1 で終了する（`OnFailure=` 発火）。

### 5.6 `craftlatch-idle.timer` / `craftlatch-idle.service`

```ini
# craftlatch-idle.timer
[Unit]
Description=CraftLatch idle check timer
PartOf=minecraft.service
After=minecraft.service

[Timer]
OnCalendar=minutely
AccuracySec=1s

[Install]
WantedBy=minecraft.service
```

`systemctl enable craftlatch-idle.timer` で `minecraft.service.wants/` にリンクされ、Minecraft の起動とともに有効化される。`PartOf=` により停止も連動する。Minecraft 停止中は一切動かない。

```ini
# craftlatch-idle.service
[Unit]
Description=CraftLatch idle check
OnFailure=craftlatch-notify@%n.service

[Service]
Type=oneshot
User=craftlatch
Group=craftlatch
ExecStart=/usr/bin/python3 -I /usr/local/lib/craftlatch/idle.py
StateDirectory=craftlatch
SyslogIdentifier=craftlatch-idle
TimeoutStartSec=30s
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/run/craftlatch/requests
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
CapabilityBoundingSet=
IPAddressAllow=localhost
IPAddressDeny=any
RestrictAddressFamilies=AF_UNIX AF_INET
SystemCallFilter=@system-service
SystemCallArchitectures=native
MemoryMax=64M
```

`idle.py` の処理:

1. `systemctl show -p InvocationID --value minecraft.service` を取得する。
2. 状態ファイル `/var/lib/craftlatch/idle.json`（`{"invocation": "...", "empty_since": <epoch|null>, "stop_requested": false}`）を読み、InvocationID が異なれば初期化する。
3. `127.0.0.1:25565` に Server List Ping（Handshake の next state = 1 → Status Request）を送る。
   - 接続・送受信のタイムアウト 3 秒。応答の長さ（VarInt）が 65,536 バイトを超えたら打ち切る。
   - 解析は標準 `json` のみで、`players.online` が 0 以上の整数であることだけを確認して取り出す。その他のフィールドは無視する。
4. 人数が 1 以上なら `empty_since = null`。0 または SLP 失敗なら、`empty_since` が null のとき現在時刻を記録する。SLP 失敗時は警告ログを出す。
5. `now - empty_since >= idle_stop_sec`（900 秒）かつ `stop_requested == false` なら stop 要求を置き（全期間 SLP 失敗なら `reason=probe-failure`、それ以外は `idle`）、`stop_requested = true` にする。
6. 起動中（SLP 未応答）の時間も無人として数える。起動から 15 分誰も接続しなければ停止する。

**残存リスク**: SLP 応答は Minecraft サーバー由来の信頼できない入力である。Paper が侵害された場合、細工した応答で idle の解析処理を攻撃される可能性があるが、サイズ上限・タイムアウト・標準 `json` のみ・整数 1 つのみ取り出し、という制限により、成立には CPython の `json` の脆弱性が必要となる。成立した場合でも得られるのは `craftlatch` ユーザーの権限（SQS の受信・削除、要求ファイル配置）である。この程度のため、ユーザーを分けずに受容する。

### 5.7 `craftlatch-notify@.service`

```ini
[Unit]
Description=CraftLatch failure notification for %i

[Service]
Type=oneshot
DynamicUser=yes
ExecStart=/usr/bin/python3 -I /usr/local/lib/craftlatch/notify.py %i
LoadCredential=webhook:/etc/craftlatch/discord-webhook
StateDirectory=craftlatch-notify
SyslogIdentifier=craftlatch-notify
TimeoutStartSec=20s
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
CapabilityBoundingSet=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
SystemCallFilter=@system-service
```

`notify.py` の処理:

- 引数のユニット名を `^[A-Za-z0-9@._-]{1,128}$` で検証する。
- 送信履歴（`$STATE_DIRECTORY/<unit>.last`）を見て、同一ユニットについて 60 分以内に送信済みなら送らない。
- Discord Webhook に `{"content": "[CraftLatch] <hostname>: <unit> が失敗しました (<時刻>)。確認: journalctl -u <unit> -n 50"}` を POST する（`urllib.request`、タイムアウト 10 秒）。
- **ログ本文は送らない**（IP・利用者名を外部サービスに出さないため）。

`OnFailure=` を付けるユニット: `craftlatch-poll`、`craftlatch-apply`、`craftlatch-idle`、`craftlatch-firewall`、`minecraft`。

### 5.8 ログ

すべて journald。`SyslogIdentifier=` をユニットごとに設定し、`journalctl -t craftlatch-apply` などで確認する。形式は key=value の 1 行。

---

## 6. ネットワーク（nftables）設計

### 6.1 `/etc/craftlatch/craftlatch.nft`

```
#!/usr/sbin/nft -f
table inet craftlatch
delete table inet craftlatch
table inet craftlatch {
  set allow4 {
    type ipv4_addr
    flags dynamic, timeout
    timeout 10m
    size 256
  }
  chain input {
    type filter hook input priority filter - 10; policy accept;
    iif "lo" accept
    tcp dport 25565 ip saddr @allow4 update @allow4 { ip saddr timeout 10m } counter accept
    udp dport 19132 ip saddr @allow4 update @allow4 { ip saddr timeout 10m } counter accept
    tcp dport 25565 counter drop
    udp dport 19132 counter drop
  }
}
```

- 先頭 3 行（`table` → `delete table` → 再定義）により、何度読み込んでも同じ結果になる。読み込みのたびに許可リストは空になる（fail-closed）。
- ct state を見ずに drop するため、ESTABLISHED の接続でも未許可（失効後）であれば遮断される。**失効すると接続中のセッションもその時点で切れる。**
- `inet` ファミリーのため、IPv6 が誤って有効化されても 25565・19132 は閉じたまま。
- 25565・19132 以外のポートには関与しない（外部からはルーターで開けていないため到達しない）。ホスト全体のデフォルト拒否ポリシーは本設計の範囲外とし、別途検討を推奨する。
- `update` 文は、パケットごとに要素の期限を設定値に戻す（nftables wiki「Updating sets from the packet path」）。`dynamic` フラグが必要。

### 6.2 `craftlatch-firewall.service`

```ini
[Unit]
Description=CraftLatch nftables gate
DefaultDependencies=no
Before=network-pre.target
Wants=network-pre.target
OnFailure=craftlatch-notify@%n.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/craftlatch/craftlatch.nft

[Install]
WantedBy=sysinit.target
```

- ネットワークが立ち上がる前にルールを読み込む。
- **`ExecStop` は定義しない。** UFW 無効のため、テーブルが消えると全員に開放されるため。
- 読み込みに失敗すると failed になり、依存する `minecraft.service` は起動できない（fail-closed）。
- ルール変更時は `sudo systemctl restart craftlatch-firewall.service` を実行する。`Requires=` により Minecraft も再起動され、許可リストも空になる（メンテナンス作業として扱う）。

### 6.3 fail-closed の保証

| 状況 | 保証の仕組み |
|---|---|
| OS 起動直後 | ファイアウォールがネットワークより先に読み込まれ、許可リストは空。Minecraft は自動起動しない |
| ルール読み込み失敗 | `minecraft.service` の `Requires=` と `ExecStartPre=+/usr/sbin/nft list table inet craftlatch` で起動不可 |
| ファイアウォールユニットの停止操作 | `ExecStop` がないためテーブルは残る。`Requires=` により Minecraft は停止する |
| 処理途中の失敗 | 許可は追加されない方向に倒れる |
| ネットワーク障害 | 新規の許可が届かないだけで、既存の許可は期限どおり失効する |

### 6.4 動作確認方法

```
sudo nft list table inet craftlatch
sudo nft list set inet craftlatch allow4
systemctl status craftlatch-firewall.service
```

- 1 つ目: ルールとカウンタ。`counter drop` の値が増えていれば遮断が効いている。
- 2 つ目: 現在の許可 IP と残り時間（`expires`）。
- 3 つ目: `active (exited)` なら読み込み成功。

外部からの遮断確認（認証していない回線、例: スマホのテザリングから）:

```
nc -vz -w 5 <自宅のグローバルIP> 25565
```

認証前はタイムアウトし、認証の 1〜2 分後に接続できれば正常。

緊急で特定 IP を遮断する場合:

```
sudo nft delete element inet craftlatch allow4 { 203.0.113.5 }
```

### 6.5 IP アドレス単位制御の制約とリスク（利用者向け案内にも記載）

1. **同じグローバル IP を共有する第三者もポートまで到達できる**（CGNAT、公衆 Wi-Fi、同居人）。最後の通信から 10 分間は有効。対策はホワイトリスト・`online-mode`・Floodgate（Xbox 認証）によるアカウント単位の拒否。**Cognito はポートを開ける仕組みであり、参加者を絞るのはホワイトリストである。**
2. **認証した端末と遊ぶ端末で IP が違うと接続できない**（例: スマホのモバイル回線で認証し、自宅 Wi-Fi の PC で遊ぶ）。遊ぶ端末と同じネットワークで認証する。
3. **iCloud プライベートリレー・ブラウザの IP 保護・VPN・プロキシ使用時は中継 IP が登録される。** 認証時はオフにする。完了ページの登録 IP で利用者が確認できる。
4. **途中で IP が変わると接続が切れ、再認証が必要**（モバイル回線での移動、ISP による IP 変更）。
5. **失効はゲームからのログアウトではない。** 通信が続く限り延長され、失効したら通信が遮断される。
6. **Cognito のアカウントを無効化しても即時遮断はされない。** 即時遮断は 6.4 の手動削除とホワイトリストからの削除で行う。

---

## 7. Minecraft サーバー設計

### 7.1 `minecraft.socket`

```ini
[Unit]
Description=Minecraft console FIFO
PartOf=minecraft.service

[Socket]
ListenFIFO=/run/minecraft.console
SocketUser=minecraft
SocketGroup=minecraft
SocketMode=0600
RemoveOnStop=yes
```

- `PartOf=` により Minecraft の停止とともに FIFO も削除され、停止中に FIFO への書き込みで起動される（ソケットアクティベーション）経路を断つ。
- 書き込めるのは minecraft と root のみ（コンソールはオペレーター権限のため）。
- 管理者のコマンド送信: `echo 'say hello' | sudo -u minecraft tee /run/minecraft.console`。出力は `journalctl -u minecraft -f`。

### 7.2 `minecraft.service`（既存を置き換え）

```ini
[Unit]
Description=Minecraft Paper server
Requires=craftlatch-firewall.service minecraft.socket
After=craftlatch-firewall.service minecraft.socket network-online.target
Wants=network-online.target
RequiresMountsFor=/media/ssd/paper
StartLimitIntervalSec=600
StartLimitBurst=3
OnFailure=craftlatch-notify@%n.service

[Service]
Type=simple
User=minecraft
Group=minecraft
WorkingDirectory=/media/ssd/paper
ExecStartPre=+/usr/sbin/nft list table inet craftlatch
ExecStart=/usr/bin/java -Xms4G -Xmx4G -jar /media/ssd/paper/paper.jar --nogui
ExecStop=/usr/local/lib/craftlatch/mc-stop $MAINPID
StandardInput=socket
StandardOutput=journal
StandardError=journal
SyslogIdentifier=minecraft
TimeoutStopSec=180
SuccessExitStatus=0 143
Restart=no
MemoryHigh=5G
MemoryMax=5632M
TasksMax=1024
CPUWeight=80
OOMPolicy=stop
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/media/ssd/paper
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
CapabilityBoundingSet=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
SystemCallArchitectures=native
```

- `[Install]` なし・enable しない。起動は apply service からのみ。
- `Restart=no`: クラッシュ時は次の認証で起動する（無人での再起動ループを防ぐ）。
- メモリ: `MemoryHigh = -Xmx + 1GB`（超過で回収を強制・減速）、`MemoryMax = -Xmx + 1.5GB`（超過で強制終了）。JVM はヒープ以外（Metaspace、スレッドスタック、Netty のダイレクトバッファ等）も使うため余裕を持たせる。**4G は仮の値で、搭載メモリに合わせて調整する。**
- CPU: `CPUWeight=80`（OS より低い優先度）。`CPUQuota=` はコア数確定後に「全コア数 − 1」程度を設定するか判断する。
- `MemoryDenyWriteExecute=yes` は使わない（JIT が書き込み可能かつ実行可能なメモリを必要とする）。
- `SystemCallFilter=@system-service` は JVM・Paper・Geyser での動作を実機確認できてから追加する。
- 外向き通信（Mojang のセッションサーバー、Xbox 認証）が必要なため、IP の外向き制限はしない。

### 7.3 `/usr/local/lib/craftlatch/mc-stop`

1. 引数の PID が空なら終了コード 0 で終了する。
2. `timeout 5` 付きで `/run/minecraft.console` に `stop\n` を書き込む。
3. `kill -0 <PID>` で 1 秒ごとに終了を確認する（最長 170 秒）。
4. 終了しなければそのまま戻り、systemd が SIGTERM → SIGKILL を送る。

Paper は `stop` でプレイヤーを切断し、ワールドを保存してから終了する。SIGTERM 時の保存動作は未検証のため、`stop` コマンドを正規手順とする。

### 7.4 Minecraft 側の設定

`server.properties`:

| 設定 | 値 | 理由 |
|---|---|---|
| `online-mode` | `true` | Mojang アカウント認証を必須にする |
| `white-list` | `true` | ホワイトリストを有効化 |
| `enforce-whitelist` | `true` | リスト外のプレイヤーを接続中でも切断 |
| `enable-rcon` | `false` | 使わない |
| `enable-query` | `false` | 使わない |
| `enable-status` | `true` | idle の SLP に必要 |
| `server-port` | `25565` | |
| `server-ip` | 空 | |

Geyser（`plugins/Geyser-Spigot/config.yml`）: `bedrock.port: 19132`、`bedrock.address: 0.0.0.0`、`remote.auth-type: floodgate`、`use-direct-connection: true`（既定）。

ホワイトリスト登録: Java 版は `whitelist add <名前>`、Bedrock 版は `fwhitelist add <ゲーマータグ>`（Floodgate の接頭辞 `.` 付きの名前で登録される）。

### 7.5 Cognito とホワイトリストの役割分担

| 層 | 仕組み | 守るもの |
|---|---|---|
| ネットワーク | Cognito 認証 + nftables 許可リスト | ポートの露出を認証済み IP に限定する。スキャン・未知の脆弱性攻撃の到達範囲を減らす。サーバー起動のトリガー |
| アカウント（Java） | `online-mode` + ホワイトリスト | 参加できる Mojang アカウントを限定する |
| アカウント（Bedrock） | Floodgate（Xbox 認証）+ ホワイトリスト | 参加できる Xbox アカウントを限定する |

同一 IP の共有（6.5-1）があるため、ホワイトリストは必須とする。

### 7.6 既存環境からの移行手順

```
sudo systemctl disable --now minecraft.service
sudo cp /etc/systemd/system/minecraft.service /etc/systemd/system/minecraft.service.bak
sudo useradd --system --home-dir /media/ssd/paper --shell /usr/sbin/nologin minecraft
sudo chown -R minecraft:minecraft /media/ssd/paper
sudo chmod 0750 /media/ssd/paper
```

この後、ユニット一式・tmpfiles.d・nft ルール・スクリプトを配置し、以下を実行する。

```
sudo systemd-tmpfiles --create /etc/tmpfiles.d/craftlatch.conf
sudo systemctl daemon-reload
sudo systemctl enable --now craftlatch-firewall.service
sudo systemctl enable --now craftlatch-apply.path craftlatch-poll.timer
sudo systemctl enable craftlatch-idle.timer
```

既存ユニットが `/etc/systemd/system/` 以外にある場合は `systemctl cat minecraft.service` で表示されるパスに読み替える。作業前にワールドのバックアップを取得する。

---

## 8. 障害時の挙動

| # | ケース | 挙動 | 未認証アクセス |
|---|---|---|---|
| 1 | AWS に接続できない | poll が警告ログを出し 1 分後に再試行。5 回連続で通知。メッセージは最大 10 分 SQS に残り、復旧後 `exp` 内なら処理、超過なら破棄（利用者は再認証）。Lambda → SQS の送信失敗はエラーページで再試行を案内 | 許可は増えない |
| 2 | SQS が空 | 1 リクエストで正常終了 | — |
| 3 | 重複配信 | add→delete→add は期限を 10 分に戻すだけ。`systemctl start` は起動済みなら無操作 | — |
| 4 | 受信後の処理途中で失敗 | 要求ファイル配置失敗時は削除せず 60 秒後に再配信。配置後の削除失敗は #3 として処理。apply の nft 失敗時は要求を隔離して通知、利用者は再認証 | 許可しない方向に倒れる |
| 5 | メッセージの遅延 | `exp`（認証から 10 分）超過で破棄。期限内なら登録時点から 10 分の許可 | 古い許可は有効にならない |
| 6 | Ubuntu 再起動 | `/run` は消え、許可リストは空、Minecraft は自動起動しない。ファイアウォールはネットワーク前に読み込み。起動後、期限内の SQS メッセージは通常処理 | fail-closed |
| 6' | 停電などの突然の停止 | Paper の自動保存以降の変更は失われうる。UPS とバックアップを推奨（範囲外） | fail-closed |
| 7 | Minecraft の起動失敗 | journal 記録と Discord 通知。10 分に 3 回失敗で起動を諦める。許可 IP は残るが無害で 10 分で失効 | — |
| 8 | 起動に数分かかる | 起動中の接続試行は RST になるが、試行のたびに期限は延長される（サーバー一覧の ping でも延長） | — |
| 9 | 複数ユーザーの同時認証 | apply が 1 回でまとめて処理。上限 256 件 | — |
| 10 | 期限切れ直前の通信 | パケットが 1 つでも届けば延長。期限後のパケット（TCP 再送含む）は破棄され、接続は切れて再認証が必要 | 境界は安全側 |
| 11 | 停止処理中に新しい認証 | apply が `start --no-block` を投入し、systemd は停止完了後に起動する想定（**実機確認事項**）。idle の状態は InvocationID の変化で初期化 | — |
| 12 | プレイヤー不在検知の失敗 | SLP 失敗は 0 人と同じ扱い（警告ログ）。15 分失敗が続けば `probe-failure` で停止。idle service 自体の失敗は通知 | — |
| 13 | アクセスキーの失効・誤設定 | poll が AccessDenied を記録し、5 回連続で通知 | 許可は増えない |
| 14 | Cognito / API Gateway / Lambda の障害 | 新規認証不可。接続中のセッションは影響なし | — |
| 15 | 時計のずれ | systemd-timesyncd を前提とし、`iat > now + 60` のメッセージは破棄 | — |
| 16 | 正規利用者による認証の連打 | API のスロットリング（1 rps、burst 5）。起動済みなら無操作 | — |
| 17 | `/media/ssd` が未マウント | `RequiresMountsFor=` により起動しない（起動失敗として通知） | — |

---

## 9. 運用

### 9.1 アクセスキーのローテーション（365 日ごと、手動）

自動ローテーション（サーバーに自身のキー作成権限を与える方式）は、漏えいしたキーで攻撃者が新しいキーを作れるため採用しない。キーが同じディスク上に置かれ続ける以上、自動化で防げる範囲も限られる。

- 間隔: 365 日。335 日経過で poll が警告を出す。CIS AWS Foundations Benchmark の推奨（90 日）からは外れるが、キーの権限が SQS の受信・削除のみで漏えい時の影響が小さいため。
- 手順: 管理者用スクリプト `craftlatch-rotate-key` を Ubuntu 上で root として実行する。管理者の一時的な認証情報（`AWS_PROFILE` 等）を使い、その権限はサーバーに残さない。スクリプトは次を順に行う。
  1. `aws iam create-access-key --user-name craftlatch-host` で新しいキーを発行する。
  2. `/etc/craftlatch/aws-credentials` を一時ファイル経由で原子的に置き換え、`aws-credentials.rotated` を更新する。
  3. `systemctl start craftlatch-poll.service` を実行し、成功を確認する。
  4. 古いキーを `aws iam update-access-key --user-name craftlatch-host --access-key-id <OLD> --status Inactive` で無効化し、`aws iam delete-access-key --user-name craftlatch-host --access-key-id <OLD>` で削除する。
- 期限に関係なく即時に更新する契機: サーバーの再構築、ディスクの交換・廃棄、バックアップ流出の疑い、管理者 PC の不審、IAM の「アクセスキー最終使用」に不審な使用履歴がある場合。

### 9.2 漏えい時の対応

| 対象 | 対応 |
|---|---|
| `craftlatch-host` のアクセスキー | `aws iam update-access-key --user-name craftlatch-host --access-key-id <KEY_ID> --status Inactive` で即時無効化し、9.1 の手順で再発行 |
| Discord Webhook URL | Discord 側で Webhook を削除・再作成し、`/etc/craftlatch/discord-webhook` を更新 |
| Cognito ユーザーのパスワード | `aws cognito-idp admin-user-global-sign-out --user-pool-id <POOL_ID> --username <NAME>` と `aws cognito-idp admin-disable-user --user-pool-id <POOL_ID> --username <NAME>`。必要に応じ 6.4 の手動遮断とホワイトリスト削除 |

検知の制約: SQS の受信・削除は CloudTrail のデータイベントであり、既定では記録されない（記録は有料オプション）。確認できるのは IAM の「アクセスキー最終使用」と、CloudTrail イベント履歴の管理イベント（90 日・無料）のみ。

### 9.3 日常運用

| 作業 | コマンド |
|---|---|
| サーバー状態 | `systemctl status minecraft.service` |
| サーバーログ | `journalctl -u minecraft -f` |
| 認証処理のログ | `journalctl -t craftlatch-poll -t craftlatch-apply -t craftlatch-idle --since today` |
| 許可中の IP | `sudo nft list set inet craftlatch allow4` |
| コンソールコマンド | `echo 'list' \| sudo -u minecraft tee /run/minecraft.console` |
| 手動停止 | `sudo systemctl stop minecraft.service` |
| Paper の更新 | 新しい jar を配置し `sudo -u minecraft ln -sfn paper-<ver>-<build>.jar /media/ssd/paper/paper.jar` |

---

## 10. コストとリソース消費

### 10.1 月額試算（認証 1 日 5 回、30 日稼働）

| サービス | 月間使用量 | 無料枠 | 無料枠内の費用 | 無料枠超過時の単価（主に us-east-1） |
|---|---|---|---|---|
| SQS | Receive 43,200 + Send 約 150 + Delete 約 150 ≒ 43,500 リクエスト | 毎月 100 万リクエスト（期限なし） | $0 | $0.40/100 万件 → 約 $0.02 |
| Lambda | 約 300〜500 回、128 MB × 約 1 秒 ≒ 40 GB-秒 | 毎月 100 万回・40 万 GB-秒（期限なし） | $0 | $0.20/100 万回 + 実行時間 → $0.001 未満 |
| API Gateway HTTP API | 約 300〜500 リクエスト | 旧来アカウントは 12 か月間 100 万件/月。2025-07-15 以降作成のアカウントはクレジット制 | $0 | 約 $1.00/100 万件 → $0.001 未満 |
| Cognito（Essentials） | MAU = 利用者数（10 人以下想定） | 10,000 MAU（期限なし） | $0 | $0.015/MAU |
| CloudWatch Logs | 1 MB 未満 | 5 GB（期限なし） | $0 | 約 $0.50/GB（取り込み） |
| S3（Terraform state） | 数 KB、数百リクエスト | — | $0.01 未満 | — |
| IAM | — | 無料 | $0 | — |
| AWS Budgets（監視予算 1 件） | — | 監視予算は無料 | $0 | — |
| データ転送 | 数 MB | 毎月 100 GB | $0 | — |
| **合計** | | | **約 $0** | **無料枠なしでも月 $0.05 未満** |

- 東京リージョンの単価は未確認（公式料金ページを直接参照できなかったため us-east-1 の単価で試算）。AWS Pricing Calculator で確認する。
- 2025-07-15 以降に作成したアカウントは、Free プランから Paid プランへの切り替えが一定期間後に必要。Paid プランでも期限のない無料枠（Lambda、SQS、Cognito 等）は適用される。

### 10.2 Ubuntu 側のリソース消費

| 構成要素 | 頻度 | CPU | メモリ |
|---|---|---|---|
| poll（Python + boto3） | 毎分 | 1 回約 0.3〜0.5 秒（1 コアの 1% 未満） | 実行中のみ約 50〜70 MB |
| idle（標準ライブラリのみ） | Minecraft 稼働中のみ毎分 | 数十ミリ秒 | 実行中のみ約 15 MB |
| apply / notify | 必要時のみ | ごくわずか | ごくわずか |
| 常駐プロセス | **なし**（path unit・timer は systemd 本体が監視） | — | — |

boto3 を使わず SigV4 署名を自作すれば負荷はさらに下がるが、署名処理を自前で保守する危険と手間に見合わないため採用しない。

---

## 11. テスト方針

| 対象 | 方法 |
|---|---|
| Lambda のロジック | pytest による単体テスト。state 不一致・nonce 不一致・`auth_time` 超過・グループ外・IPv6/プライベート IP・トークン交換失敗の各ケースを、`urllib` と boto3 をモックして検証 |
| `host/lib` の検証関数 | pytest による単体テスト。メッセージ・要求ファイルの正常系/異常系（サイズ超過、型不正、期限切れ、未来の `iat`、シンボリックリンク、所有者不一致） |
| nft コマンド生成 | 生成されるトランザクション文字列の単体テスト（不正な IP が混入しないこと） |
| SLP クライアント | ローカルのダミーサーバーで、正常応答・巨大応答・タイムアウト・不正 JSON を検証 |
| Terraform | `terraform validate`・`terraform plan` の確認 |
| 実機結合試験 | 6.4 の外部回線からの遮断/許可確認、10 分失効の確認、再起動後の fail-closed 確認、15 分無人停止とワールド保存ログ（`Saving worlds` 等）の確認、停止中認証（8 #11）の確認 |

---

## 12. 未検証事項

| # | 事項 | 確認方法 |
|---|---|---|
| U1 | `ipv6.disable=1` 環境で `table inet` が読み込めるか（不可なら `table ip` に変更） | 実機で `sudo nft -c -f /etc/craftlatch/craftlatch.nft` |
| U2 | 実機のカーネル・nft バージョンで `update` による期限延長が動作するか | 実機で `nft list set` の `expires` の変化を観察 |
| U3 | 停止ジョブ実行中に `systemctl start --no-block` した場合、停止完了後に起動されるか | 実機試験 |
| U4 | Paper の SIGTERM 時の保存動作 | 実機試験（参考情報。正規手順は `stop`） |
| U5 | `SystemCallFilter=@system-service` 下での JVM・Paper・Geyser の動作 | 実機試験後に追加 |
| U6 | Paper が FIFO の stdin からコンソールコマンドを受け付けるか | 実機試験 |
| U7 | 東京リージョンの各サービス単価 | AWS Pricing Calculator |
| U8 | Terraform AWS プロバイダーの `user_pool_tier`・`ip_address_type` の引数名と対応バージョン | プロバイダーのドキュメント |
| U9 | Cognito Managed Login のセッションクッキーの有効期間（`prompt=login` + `auth_time` で要件は満たすため参考情報） | Cognito ドキュメント |

## 13. 確認が必要な事項

1. サーバーの搭載メモリと CPU コア数（`-Xmx`・`MemoryHigh`・`MemoryMax`・`CPUQuota` の決定）
2. Ubuntu と systemd のバージョン（`LoadCredential=` の `%d` には systemd 248 以上が必要）
3. AWS アカウントの作成時期（2025-07-15 以前か以降かで無料枠の形態が異なる）
4. 既存の Paper の jar ファイル名と現在の JVM オプション
5. 利用者への案内方法（ログイン URL と 6.5 の制約の周知）

---

## 参考資料

- [Amazon SQS pricing](https://aws.amazon.com/sqs/pricing/)
- [Amazon Cognito pricing](https://aws.amazon.com/cognito/pricing/)
- [Amazon Cognito now supports OIDC prompt parameter (2025-05)](https://aws.amazon.com/about-aws/whats-new/2025/05/amazon-cognito-oidc-prompt-parameter)
- [AWS Free Tier update (2025-07)](https://aws.amazon.com/blogs/aws/aws-free-tier-update-new-customers-can-get-started-and-explore-aws-with-up-to-200-in-credits)
- [Invoking Lambda function URLs](https://docs.aws.amazon.com/lambda/latest/dg/urls-invocation.html)
- [IP address types for HTTP APIs](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-ip-address-type.html)
- [Create AWS Lambda proxy integrations for HTTP APIs](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-develop-integrations-lambda.html)
- [Setting-up dead-letter queue retention in Amazon SQS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/setting-up-dead-letter-queue-retention.html)
- [What is IAM Roles Anywhere?](https://docs.aws.amazon.com/rolesanywhere/latest/userguide/introduction.html)
- [nftables wiki: Updating sets from the packet path](https://wiki.nftables.org/wiki-nftables/index.php/Updating_sets_from_the_packet_path)
- [OpenID Connect Core 1.0, 3.1.3.7 ID Token Validation](https://openid.net/specs/openid-connect-core-1_0.html#IDTokenValidation)
- [MC-17244（SIGTERM 時の挙動に関する報告）](https://bugs.mojang.com/browse/MC-17244)
