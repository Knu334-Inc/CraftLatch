#!/usr/bin/env bash
# CraftLatch のホスト側ファイルを配置する（管理者が root で実行する）。
# 使い方: sudo host/install.sh [--yes] [--refresh-router-mac]
# テスト用: CRAFTLATCH_ROOT を指定すると、配置先をその接頭辞の下に変え、
# useradd・systemd-tmpfiles・systemctl daemon-reload は実行せず表示のみにする。
set -euo pipefail

usage() {
    echo "使い方: $0 [--yes] [--refresh-router-mac]" >&2
}

yes_flag=0
refresh_flag=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --yes) yes_flag=1 ;;
        --refresh-router-mac) refresh_flag=1 ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "不明なオプション: $1" >&2
            usage
            exit 2
            ;;
    esac
    shift
done

host_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root="${CRAFTLATCH_ROOT:-}"
test_mode=0
[ -n "$root" ] && test_mode=1
python3_bin="${PYTHON3:-python3}"

lib_dir="$root/usr/local/lib/craftlatch"
unit_dir="$root/etc/systemd/system"
tmpfiles_dir="$root/etc/tmpfiles.d"
etc_dir="$root/etc/craftlatch"
config_path="$etc_dir/config.json"
nft_path="$etc_dir/craftlatch.nft"

owner_args=()
[ "$test_mode" -eq 0 ] && owner_args=(-o root -g root)

umask 022

# ---- 事前検証（何も変更する前に、失敗をすべて表示する） ----
if [ "$test_mode" -eq 0 ]; then
    failures=()
    [ "$EUID" -eq 0 ] || failures+=("root で実行してください（sudo host/install.sh）")
    getent passwd minecraft >/dev/null 2>&1 \
        || failures+=("minecraft ユーザーがありません（設計書 7.6 の移行手順を先に実施）")
    "$python3_bin" -c 'import boto3' >/dev/null 2>&1 \
        || failures+=("python3-boto3 がありません（sudo apt install python3-boto3）")
    command -v nft >/dev/null 2>&1 \
        || failures+=("nft がありません（sudo apt install nftables）")
    systemd_major=$(systemctl --version 2>/dev/null | awk 'NR==1{print $2}') || systemd_major=""
    case "$systemd_major" in
        '' | *[!0-9]*) failures+=("systemd のバージョンを取得できません") ;;
        *) [ "$systemd_major" -ge 248 ] || failures+=("systemd 248 以上が必要です（現在 $systemd_major）") ;;
    esac
    [ -d /media/ssd/paper ] || failures+=("/media/ssd/paper がありません")
    if [ "${#failures[@]}" -gt 0 ]; then
        echo "事前検証に失敗しました。何も変更していません。" >&2
        printf '  - %s\n' "${failures[@]}" >&2
        exit 1
    fi
else
    echo "[テストモード] CRAFTLATCH_ROOT=$root: 事前検証は行いません"
fi

# ---- craftlatch ユーザー ----
if [ "$test_mode" -eq 1 ]; then
    echo "[テストモード] 実行しない: useradd --system --no-create-home --shell /usr/sbin/nologin craftlatch"
elif ! getent passwd craftlatch >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin craftlatch
fi

# ---- 配置 ----
first_install=0
[ -e "$nft_path" ] || first_install=1

py_changed=0
nft_changed=0
changed_units=()
pending_path="$etc_dir/.install-pending"

# 前回の実行が途中で失敗していた場合、その検出結果を引き継ぐ
add_unit() {
    local u
    for u in ${changed_units[@]+"${changed_units[@]}"}; do
        [ "$u" = "$1" ] && return 0
    done
    changed_units+=("$1")
}

load_pending() {
    local line
    [ -f "$pending_path" ] || return 0
    while IFS= read -r line; do
        case "$line" in
            py) py_changed=1 ;;
            nft) nft_changed=1 ;;
            unit:*) add_unit "${line#unit:}" ;;
        esac
    done <"$pending_path"
}

# 検出結果を配置前に保存する（以降の処理が失敗しても案内を失わないため）
save_pending() {
    local u
    (
        umask 077
        {
            [ "$py_changed" -eq 1 ] && echo py
            [ "$nft_changed" -eq 1 ] && echo nft
            for u in ${changed_units[@]+"${changed_units[@]}"}; do echo "unit:$u"; done
            true
        } >"$pending_path"
    )
    chmod 0600 -- "$pending_path"
}

# 配置対象: モード|元|先|種別(py|unit)
entries=()
for f in "$host_dir"/lib/*.py "$host_dir"/lib/fwcheck-expected.json; do
    entries+=("0644|$f|$lib_dir/$(basename -- "$f")|py")
done
for f in fwcheck mc-stop; do
    entries+=("0755|$host_dir/lib/$f|$lib_dir/$f|py")
done
for f in "$host_dir"/systemd/*; do
    entries+=("0644|$f|$unit_dir/$(basename -- "$f")|unit")
done
entries+=("0644|$host_dir/tmpfiles.d/craftlatch.conf|$tmpfiles_dir/craftlatch.conf|py")

install "${owner_args[@]}" -d -m 0755 -- "$lib_dir" "$unit_dir" "$tmpfiles_dir" "$etc_dir"

load_pending
for e in "${entries[@]}"; do
    IFS='|' read -r _ src dst kind <<<"$e"
    if [ ! -e "$dst" ] || ! cmp -s -- "$src" "$dst"; then
        case "$kind" in
            py) py_changed=1 ;;
            unit) add_unit "$(basename -- "$dst")" ;;
        esac
    fi
done
save_pending

for e in "${entries[@]}"; do
    IFS='|' read -r mode src dst _ <<<"$e"
    install "${owner_args[@]}" -m "$mode" -- "$src" "$dst"
done

# ---- 設定（既存の config.json・aws-credentials・discord-webhook には触れない） ----
if [ ! -e "$config_path" ]; then
    install "${owner_args[@]}" -m 0644 -- "$host_dir/config.json.example" "$config_path"
    echo "config.json を雛形から作成しました: $config_path"
fi

# ---- ルーターの MAC ----
mac_args=(--config "$config_path")
[ "$yes_flag" -eq 1 ] && mac_args+=(--yes)
[ "$refresh_flag" -eq 1 ] && mac_args+=(--refresh)
if ! "$python3_bin" -E -s "$lib_dir/routermac.py" "${mac_args[@]}"; then
    echo "ルーターの MAC を設定できませんでした。$config_path の exclude_ether_saddr を手で入力して再実行してください。" >&2
    exit 1
fi

# ---- nft ルール ----
if [ "$test_mode" -eq 1 ]; then
    echo "[テストモード] 実行しない: python3 -E -s $lib_dir/nftgen.py --output $nft_path"
else
    nft_new="$nft_path.new"
    rm -f -- "$nft_new"
    if ! "$python3_bin" -E -s "$lib_dir/nftgen.py" --output "$nft_new"; then
        rm -f -- "$nft_new"
        echo "nft ルールを生成できませんでした。/etc/craftlatch/config.json の queue_url を入力して再実行してください。" >&2
        echo "（queue_url は Terraform の出力 queue_url を手元で確認して入力します）" >&2
        exit 1
    fi
    if [ -e "$nft_path" ] && cmp -s -- "$nft_new" "$nft_path"; then
        rm -f -- "$nft_new"
    else
        if [ "$first_install" -eq 0 ]; then
            nft_changed=1
            save_pending
        fi
        chmod 0644 -- "$nft_new"
        mv -- "$nft_new" "$nft_path"
    fi
fi

# ---- tmpfiles と daemon-reload ----
if [ "$test_mode" -eq 1 ]; then
    echo "[テストモード] 実行しない: systemd-tmpfiles --create /etc/tmpfiles.d/craftlatch.conf"
    echo "[テストモード] 実行しない: systemctl daemon-reload"
else
    systemd-tmpfiles --create /etc/tmpfiles.d/craftlatch.conf
    systemctl daemon-reload
fi

# ---- 案内（稼働中のユニットは自動では再起動しない） ----
echo
echo "== 次の作業 =="
if [ "$first_install" -eq 1 ]; then
    echo "初回の配置です。/etc/craftlatch/aws-credentials（0400）と discord-webhook（0400）を配置してから、次を順に実行してください。"
    echo "  sudo systemctl enable --now craftlatch-firewall.service"
    echo "  sudo systemctl enable --now craftlatch-apply.path craftlatch-poll.timer"
    echo "  sudo systemctl enable craftlatch-idle.timer"
    rm -f -- "$pending_path"
    exit 0
fi

restart_firewall=$nft_changed
restart_poll=0
restart_apply=0
note_idle=0
note_notify=0
note_mc=0
for u in ${changed_units[@]+"${changed_units[@]}"}; do
    case "$u" in
        craftlatch-poll.service | craftlatch-poll.timer) restart_poll=1 ;;
        craftlatch-apply.service | craftlatch-apply.path) restart_apply=1 ;;
        craftlatch-idle.service | craftlatch-idle.timer) note_idle=1 ;;
        craftlatch-notify@.service) note_notify=1 ;;
        minecraft.service | minecraft.socket) note_mc=1 ;;
        craftlatch-firewall.service) restart_firewall=1 ;;
    esac
done

step=0
if [ "$py_changed" -eq 1 ]; then
    echo "Python スクリプト等の変更: 操作不要（次回の実行から反映）"
fi
if [ "$restart_poll" -eq 1 ]; then
    step=$((step + 1))
    echo "$step. sudo systemctl restart craftlatch-poll.timer"
fi
if [ "$restart_apply" -eq 1 ]; then
    step=$((step + 1))
    echo "$step. sudo systemctl restart craftlatch-apply.path"
fi
if [ "$restart_firewall" -eq 1 ]; then
    step=$((step + 1))
    echo "$step. sudo systemctl restart craftlatch-firewall.service"
    echo "   警告: Minecraft が停止し、許可リストが空になります。次の認証で Minecraft が起動します（手動なら sudo systemctl start minecraft.service）。"
fi
[ "$note_idle" -eq 1 ] && echo "craftlatch-idle の変更: 次回の Minecraft 起動から反映"
[ "$note_notify" -eq 1 ] && echo "craftlatch-notify@.service の変更: 操作不要（次回の通知から反映）"
[ "$note_mc" -eq 1 ] && echo "minecraft.service / minecraft.socket の変更: 次回の起動から反映"
if [ "$py_changed" -eq 0 ] && [ "${#changed_units[@]}" -eq 0 ] && [ "$nft_changed" -eq 0 ]; then
    echo "変更はありません。"
fi
rm -f -- "$pending_path"
