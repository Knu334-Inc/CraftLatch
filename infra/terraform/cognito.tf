# Cognito（設計書 4.1）。

# User Pool 本体は bootstrap（infra/bootstrap/cognito.tf）で管理者が作成・変更する。apply ロールには Pool 本体の
# 作成・変更・削除を許可していないため、ここでは名前で参照し、配下のリソースだけを作る。
# 同じ名前の Pool が 0 件または 2 件以上なら plan・apply を止める（別の Pool に配下を作らないため）。
data "aws_cognito_user_pools" "this" {
  name = "${var.project}-users"

  lifecycle {
    postcondition {
      condition     = try(length(self.ids), 0) == 1
      error_message = "User Pool craftlatch-users がちょうど 1 件ではありません（bootstrap の適用状況と同名の Pool を確認してください）。"
    }
  }
}

locals {
  user_pool_id = one(data.aws_cognito_user_pools.this.ids)
}

resource "aws_cognito_user_group" "minecraft" {
  name         = "minecraft"
  user_pool_id = local.user_pool_id
}

# プレフィックスドメインは craftlatch-<8 桁の 16 進数>。CI から値を渡す経路を増やさないため random_id で生成する。
resource "random_id" "domain" {
  byte_length = 4
}

resource "aws_cognito_user_pool_domain" "this" {
  domain                = "${var.project}-${random_id.domain.hex}"
  user_pool_id          = local.user_pool_id
  managed_login_version = 2
}

# パブリッククライアント（シークレットなし）+ PKCE。認可コードフローのみ、スコープは openid のみ。
resource "aws_cognito_user_pool_client" "web" {
  name            = "${var.project}-web"
  user_pool_id    = local.user_pool_id
  generate_secret = false

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["${aws_apigatewayv2_api.this.api_endpoint}/callback"]

  # Managed Login の code フロー以外（USER_SRP・CUSTOM など）を無効にする。
  explicit_auth_flows = ["ALLOW_REFRESH_TOKEN_AUTH"]

  prevent_user_existence_errors = "ENABLED"

  # ID・アクセストークン 5 分、リフレッシュトークン 60 分（いずれも最短）。
  access_token_validity  = 5
  id_token_validity      = 5
  refresh_token_validity = 60

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "minutes"
  }
}

# Managed Login の利用に必要。既定のスタイルを使う。
resource "aws_cognito_managed_login_branding" "web" {
  client_id    = aws_cognito_user_pool_client.web.id
  user_pool_id = local.user_pool_id

  use_cognito_provided_values = true
}
