# 管理者が手元の terraform output で参照する。CI は出力を表示しない（URL・ID をログに残さないため）。
output "login_url" {
  description = "利用者に案内するログイン URL"
  value       = "${aws_apigatewayv2_api.this.api_endpoint}/login"
}

output "user_pool_id" {
  description = "User Pool ID（ユーザー追加の手順で使う）"
  value       = aws_cognito_user_pool.this.id
}

output "queue_url" {
  description = "craftlatch-grants の URL（ホストの /etc/craftlatch/config.json に設定する）"
  value       = aws_sqs_queue.grants.url
}

output "host_user_arn" {
  description = "craftlatch-host の ARN"
  value       = aws_iam_user.host.arn
}
