# Lambda craftlatch-auth（設計書 4.3）。

# ZIP 内に craftlatch_auth/ ディレクトリを保つため src/lambda を丸ごと固める（tests/lambda の test_zip_layout_importable と同じ規則）。
data "archive_file" "lambda" {
  type             = "zip"
  source_dir       = "${path.module}/../../src/lambda"
  excludes         = ["**/__pycache__", "**/*.pyc"]
  output_file_mode = "0644"
  output_path      = "${path.module}/build/craftlatch-auth.zip"
}

resource "aws_cloudwatch_log_group" "auth" {
  name              = "/aws/lambda/${var.project}-auth"
  retention_in_days = 30
}

resource "aws_lambda_function" "auth" {
  function_name    = "${var.project}-auth"
  role             = aws_iam_role.auth.arn
  handler          = "craftlatch_auth.handler.lambda_handler"
  runtime          = "python3.13"
  architectures    = ["arm64"]
  memory_size      = 128
  timeout          = 10
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256

  environment {
    variables = {
      COGNITO_DOMAIN = "${aws_cognito_user_pool_domain.this.domain}.auth.${var.region}.amazoncognito.com"
      CLIENT_ID      = aws_cognito_user_pool_client.web.id
      ISSUER         = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.this.id}"
      REDIRECT_URI   = "${aws_apigatewayv2_api.this.api_endpoint}/callback"
      QUEUE_URL      = aws_sqs_queue.grants.url
      ALLOWED_GROUP  = aws_cognito_user_group.minecraft.name
      MAX_AUTH_AGE   = "120"
      GRANT_TTL      = "600"
    }
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.auth.name
  }

  # 関数より先にロググループを作り、保持期間なしのロググループが自動作成されるのを防ぐ。
  depends_on = [aws_cloudwatch_log_group.auth]
}
