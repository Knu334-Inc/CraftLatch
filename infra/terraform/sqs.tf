# 許可イベントのキュー（設計書 4.4）。Standard、DLQ なし。
resource "aws_sqs_queue" "grants" {
  name                       = "${var.project}-grants"
  message_retention_seconds  = 600
  visibility_timeout_seconds = 60
  receive_wait_time_seconds  = 0
  sqs_managed_sse_enabled    = true
}

# TLS を使わないアクセスを拒否する文のみ。送受信の主体の制限は IAM ポリシーで行う。
data "aws_iam_policy_document" "grants_queue" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["sqs:*"]
    resources = [aws_sqs_queue.grants.arn]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_sqs_queue_policy" "grants" {
  queue_url = aws_sqs_queue.grants.id
  policy    = data.aws_iam_policy_document.grants_queue.json
}
