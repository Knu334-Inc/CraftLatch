# API Gateway HTTP API（設計書 4.2）。API は Lambda に依存しない（Lambda の REDIRECT_URI とクライアントの callback_urls が API を参照するため）。

data "aws_caller_identity" "current" {}

resource "aws_apigatewayv2_api" "this" {
  name          = "${var.project}-api"
  protocol_type = "HTTP"

  # デュアルスタックにしてはならない（P4）。Lambda は IPv4 の送信元だけを許可する。
  ip_address_type = "ipv4"
}

resource "aws_apigatewayv2_integration" "auth" {
  api_id                 = aws_apigatewayv2_api.this.id
  integration_type       = "AWS_PROXY"
  integration_method     = "POST"
  integration_uri        = aws_lambda_function.auth.invoke_arn
  payload_format_version = "2.0"
}

# $default ルートは作らない（他のパスは 404）。
resource "aws_apigatewayv2_route" "login" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "GET /login"
  target    = "integrations/${aws_apigatewayv2_integration.auth.id}"
}

resource "aws_apigatewayv2_route" "callback" {
  api_id    = aws_apigatewayv2_api.this.id
  route_key = "GET /callback"
  target    = "integrations/${aws_apigatewayv2_integration.auth.id}"
}

# アクセスログは無効（Lambda のログで足りる）。
resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 5
    throttling_rate_limit  = 1
  }
}

resource "aws_lambda_permission" "api" {
  statement_id   = "AllowApiGatewayInvoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.auth.function_name
  principal      = "apigateway.amazonaws.com"
  source_account = data.aws_caller_identity.current.account_id
  source_arn     = "${aws_apigatewayv2_api.this.execution_arn}/*/*"
}
