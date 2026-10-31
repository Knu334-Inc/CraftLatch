# IAM（設計書 4.5）。ロール・ユーザーには Permissions Boundary を必ず付け、権限はインラインポリシーで付与する。
# アクセスキーは Terraform では作らない（state にシークレットが残るため）。

# bootstrap（infra/bootstrap/boundary.tf）が作成した Boundary。
data "aws_iam_policy" "boundary" {
  name = "craftlatch-boundary"
}

################################################################################
# Lambda 実行ロール
################################################################################

data "aws_iam_policy_document" "auth_trust" {
  statement {
    sid     = "LambdaAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "auth" {
  name                 = "${var.project}-auth-role"
  assume_role_policy   = data.aws_iam_policy_document.auth_trust.json
  permissions_boundary = data.aws_iam_policy.boundary.arn
}

data "aws_iam_policy_document" "auth" {
  # 自分のロググループのログストリームへの書き込みのみ。
  statement {
    sid    = "WriteOwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.auth.arn}:*"]
  }

  statement {
    sid       = "SendGrants"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.grants.arn]
  }
}

resource "aws_iam_role_policy" "auth" {
  name   = "${var.project}-auth"
  role   = aws_iam_role.auth.id
  policy = data.aws_iam_policy_document.auth.json
}

################################################################################
# Ubuntu ホスト用 IAM ユーザー（コンソールログイン不可。ログインプロファイルは作らない）
################################################################################

resource "aws_iam_user" "host" {
  name                 = "${var.project}-host"
  permissions_boundary = data.aws_iam_policy.boundary.arn
}

# 受信と削除のみ。漏えいしても許可イベントの偽造（SendMessage）はできない。
data "aws_iam_policy_document" "host" {
  statement {
    sid    = "ReceiveGrants"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
    ]
    resources = [aws_sqs_queue.grants.arn]
  }
}

resource "aws_iam_user_policy" "host" {
  name   = "${var.project}-host"
  user   = aws_iam_user.host.name
  policy = data.aws_iam_policy_document.host.json
}
