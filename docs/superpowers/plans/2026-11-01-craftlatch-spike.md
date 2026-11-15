# CraftLatch 事前検証の記録

実装計画（`2026-11-01-craftlatch.md`）Task 0 の結果。未検証事項の番号は設計書 13 章による。Task 14・15 の結果もこの文書に追記する。

## 環境

- Ubuntu 24.04.5 LTS、カーネル `6.8.0-147-generic`（`ipv6.disable=1`）、systemd 255
- nftables v1.0.9（Old Doc Yak #3）
- OpenJDK 21.0.12.1、Paper `paper-1.21.7-17.jar`
- 実施日: 2026-11-01

## 結果一覧

| # | 事項 | 結果 |
|---|---|---|
| U1 | `ipv6.disable=1` の環境で `table inet` を読み込めるか | **成功**。`table inet` のまま進める |
| U6 | Paper が FIFO から入力した `stop` を受け付けるか | **成功** |
| U3 | 停止処理の途中に `systemctl start --no-block` した場合、停止の完了後に起動されるか | **成功**（停止完了の 13ms 後に起動） |

いずれも期待どおりだったため、計画の改訂は不要。

## U1: `table inet` の読み込み

本番のルールに影響しないよう、別のネットワーク名前空間で実施した（管理者が sudo で実行）。

```
sudo unshare --net sh -c 'nft -f <HOME>/craftlatch-probe/craftlatch-probe.nft && nft -j list table inet craftlatch' > <HOME>/craftlatch-probe/nft_list_table.json; echo rc=$?
rc=0
```

- 読み込んだルールは `tests/host/fixtures/craftlatch-probe.nft`（設計書 6.1 の全文。ルーターの MAC は `aa:bb:cc:dd:ee:ff`）。
- 出力した JSON を `host/lib/fwcheck-expected.json` として取り込んだ（3,477 バイト）。fwcheck の期待構造の雛形として使う（計画 Task 6）。
- JSON の内容: set `allow4`（`type ipv4_addr`、`flags ["timeout","dynamic"]`、`timeout 600`、`size 256`）と、チェイン `input`（`filter`、`hook input`、`prio -10`、`policy accept`）。ルールは 7 本で、設計書 6.1 と同じ順序。`update @allow4 { ip saddr timeout 600 }` も含む。
- U1 の確認に使ったのは `nft -f` での読み込みで、設計書 13 章にある `nft -c`（構文チェックだけ）より強い確認になる。

## U6: FIFO 経由の停止

作業用ユーザーでプラグインなしの一時サーバーを起動して確認した。待ち受けは `127.0.0.1:25599` だけにし、作業ディレクトリは `/tmp` に作った。FIFO は `<>`（読み書き両用）で stdin に接続した。

```
[05:43:31 INFO]: Done (29.687s)! For help, type "help"
[05:43:32 INFO]: Stopping server
[05:43:32 INFO]: Saving worlds
[05:43:33 INFO]: All RegionFile I/O tasks to complete
[05:43:33 INFO]: [MoonriseCommon] Awaiting termination of worker pool for up to 60s...
[05:43:33 INFO]: [MoonriseCommon] Awaiting termination of I/O pool for up to 60s...
```

- `stop` を書き込んでから約 1 秒で停止処理が始まり、ワールドを保存してから、終了待ちの上限 120 秒以内にプロセスが自分で終了した（後片付けの `kill` の対象は残っていなかった）。
- プラグイン（Geyser・DriveBackupV2・Multiverse）を含む実構成での確認は、Task 15 Step 4 で行う。

## U3: 停止中の起動要求

ダミーのユニット（`ExecStart=/bin/sleep infinity`、`ExecStop=/bin/sleep 10`）で確認した（管理者が sudo で実行。最後にユニットは削除済み）。

```
state=active
11月 01 05:46:28.732232 ubuntu systemd[1]: Started craftlatch-stopprobe.service.
11月 01 05:46:28.748810 ubuntu systemd[1]: Stopping craftlatch-stopprobe.service...
11月 01 05:46:38.751133 ubuntu systemd[1]: craftlatch-stopprobe.service: Deactivated successfully.
11月 01 05:46:38.764112 ubuntu systemd[1]: Started craftlatch-stopprobe.service.
```

- 停止ジョブの実行中に投入した `start --no-block` は、停止が完了した後（13ms 後）に実行された。
- apply の「`start --no-block` を投入し、停止の完了後に systemd が起動する」という前提（設計書 8 章 #11、計画 Task 4）が成り立つ。

## 作成したファイル

| パス | 用途 |
|---|---|
| `tests/host/fixtures/craftlatch-probe.nft` | 設計書 6.1 のルール全文（MAC `aa:bb:cc:dd:ee:ff`）。Task 6 で生成器の出力と一致するかを検査する |
| `tests/host/fixtures/craftlatch-probe-bad.nft` | 上のファイルの `iif "lo" accept` の直後に `tcp dport 25565 accept` を 1 行足したもの。Task 15 の fwcheck の異常系試験に使う |
| `host/lib/fwcheck-expected.json` | U1 で出力した `nft -j` の JSON。fwcheck の期待構造の雛形 |

サーバーの `~/craftlatch-probe/` にも、2 つの `.nft` と `nft_list_table.json` を残してある（Task 15 で使う）。

## Task 14・15 の結果（2026-11-01〜11-15）

設計書 13.1 にも結果をまとめた。

### Task 14（AWS）

- bootstrap は管理者が CloudShell から apply した。既存の GitHub の OIDC プロバイダーを使う。
- GitHub のリポジトリが不変 ID 付きの `sub`（`repo:<owner>@<owner_id>/<repo>@<repo_id>`）を使うため、ロールの信頼ポリシーをその形式にした（変数 `github_oidc_subject_prefix`）。
- 初回 apply のステージ作成は、`apigateway:TagResource`（`Project=CraftLatch` の条件付き）と、API Gateway のサービスリンクロールの作成を許可して成功した（U11）。
- 2 回目の plan は No changes（U10）。Managed Login でのログインに成功した（`ALLOW_REFRESH_TOKEN_AUTH` だけで通る）。

### Task 15（サーバー）

| 項目 | 結果 |
|---|---|
| アクセスキーと Webhook の配置 | 管理者の MFA がパスキーで CLI からロールを引き受けられないため、CloudShell で発行してサーバーの `read -rs` に貼る方式にした（`docs/setup.md` ⑥） |
| `/usr/local` の所有者 | 存在しない UID/GID 500 の所有でグループ書き込み可だった（882 件）。root が実行する fwcheck を差し替えられうるため、root:root に直した |
| ユニットの構文 | `systemd-analyze verify` は rc=0（IPv6 無効による警告だけ） |
| 未認証の回線からの遮断（TCP） | 成功。drop のカウンタが増え、`nc` はタイムアウト |
| 認証した回線からの接続 | 回線 B で成功。回線 A は大規模 NAT で、ログインした IP とゲームの通信の IP が違い、接続できなかった（既知の制約として文書化） |
| 統合版での外部接続 | 最初は失敗（TCP/19132 のシグナリングと STUN の返事）。ルールを直して成功（PR #5） |
| 期限の延長と失効 | 遊んでいる間は切れず、遊び終えて 10 分後に許可リストから消えた |
| 無人停止 | 無人で 15 分たつと自動で止まった。停止時の保存はログで確認 |
| 障害の通知 | Discord に届いた |

### 運用中に起きたこと

- サーバーを Ubuntu 26.04.1（カーネル 7.0、nftables 1.1.6）に更新した。
  - nftables 1.1.6 では `nft -j list` の並びが chain → set になり、fwcheck の雛形を、別の名前空間で出力した JSON で取り直した。
  - Paper を Minecraft 26.x に更新したところ、Java 25 以上が必要になった。Java 21 に固定するドロップインがあったため起動に失敗し、Java 25 のドロップインに置き換えた。
- サーバーの既存の iptables（INPUT の既定が DROP）に、25565 の新しい接続を 1 時間に 1 回へ制限するルールがあった。CraftLatch で許可しても接続できなかったため、回数の制限をなくした（`docs/setup.md` ⑥ の事前準備）。
