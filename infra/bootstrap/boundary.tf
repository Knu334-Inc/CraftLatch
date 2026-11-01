# Permissions Boundary。apply ロールが作成するロール・ユーザー（craftlatch-auth-role、craftlatch-host）に必ず付ける。
# 上限は設計書 4.5 末尾の範囲のみ: CloudWatch Logs（craftlatch-* のロググループへの書き込み）と
# SQS（craftlatch-grants への SendMessage・ReceiveMessage・DeleteMessage）。
data "aws_iam_policy_document" "boundary" {
  # Lambda のロググループ名は /aws/lambda/<関数名> のため、craftlatch-* の関数のロググループを対象にする。
  # CreateLogStream・PutLogEvents の対象はログストリーム（log-group:<名前>:log-stream:<名前>）。
  statement {
    sid    = "BoundaryWriteCraftlatchLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/craftlatch-*:*"]
  }

  statement {
    sid    = "BoundaryGrantsQueue"
    effect = "Allow"
    actions = [
      "sqs:SendMessage",
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
    ]
    resources = [local.grants_queue_arn]
  }
}

resource "aws_iam_policy" "boundary" {
  name        = "craftlatch-boundary"
  description = "CraftLatch: permissions boundary for IAM roles and users created by the apply role"
  policy      = data.aws_iam_policy_document.boundary.json
}
