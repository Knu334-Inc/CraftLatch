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
sudo unshare --net sh -c 'nft -f /home/agent/craftlatch-probe/craftlatch-probe.nft && nft -j list table inet craftlatch' > /home/agent/craftlatch-probe/nft_list_table.json; echo rc=$?
rc=0
```

- 読み込んだルールは `tests/host/fixtures/craftlatch-probe.nft`（設計書 6.1 の全文。ルーターの MAC は `aa:bb:cc:dd:ee:ff`）。
- 出力した JSON を `host/lib/fwcheck-expected.json` として取り込んだ（3,477 バイト）。fwcheck の期待構造の雛形として使う（計画 Task 6）。
- JSON の内容: set `allow4`（`type ipv4_addr`、`flags ["timeout","dynamic"]`、`timeout 600`、`size 256`）と、チェイン `input`（`filter`、`hook input`、`prio -10`、`policy accept`）。ルールは 7 本で、設計書 6.1 と同じ順序。`update @allow4 { ip saddr timeout 600 }` も含む。
- U1 の確認に使ったのは `nft -f` での読み込みで、設計書 13 章にある `nft -c`（構文チェックだけ）より強い確認になる。

## U6: FIFO 経由の停止

agent ユーザーでプラグインなしの一時サーバーを起動して確認した。待ち受けは `127.0.0.1:25599` だけにし、作業ディレクトリは `/tmp` に作った。FIFO は `<>`（読み書き両用）で stdin に接続した。

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
