#!/usr/bin/env bash
# terraform plan ファイルから、アドレス・操作種別・件数だけをジョブサマリーに出す。
# 公開ログに属性値を出さないため、stdout には何も出力しない。
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: tf-summary.sh <tfplan>" >&2
  exit 2
fi

plan_json="${RUNNER_TEMP:?}/plan.json"
summary="${GITHUB_STEP_SUMMARY:?}"

rc=0
terraform show -json "$1" > "$plan_json" 2> /dev/null || rc=$?
if [ "$rc" -ne 0 ]; then
  echo "terraform show failed (exit $rc)"
  echo "terraform show failed (exit $rc)" >> "$summary"
  exit 1
fi

{
  echo "### Terraform plan"
  echo
  jq -r '
    [.resource_changes[]? | select(.change.actions != ["no-op"])] as $c
    | def has($a): .change.actions | index($a) != null;
      "| 作成 | 更新 | 削除 | 置換 |",
      "|---|---|---|---|",
      "| \([$c[] | select(.change.actions == ["create"])] | length) | \([$c[] | select(.change.actions == ["update"])] | length) | \([$c[] | select(.change.actions == ["delete"])] | length) | \([$c[] | select(has("create") and has("delete"))] | length) |",
      "",
      "| アドレス | 操作 |",
      "|---|---|",
      ($c[] | "| `\(.address)` | \(.change.actions | join(",")) |")
  ' "$plan_json"
} >> "$summary"
