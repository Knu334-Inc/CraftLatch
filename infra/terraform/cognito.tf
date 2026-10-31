# Cognito（設計書 4.1）。

resource "aws_cognito_user_pool" "this" {
  name           = "${var.project}-users"
  user_pool_tier = "ESSENTIALS"

  # サインイン識別子はユーザー名（username_attributes・alias_attributes は指定しない）。
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  mfa_configuration = "OFF"

  # MFA がないため長さで補う。文字種の条件は課さない。
  password_policy {
    minimum_length                   = 14
    require_lowercase                = false
    require_numbers                  = false
    require_symbols                  = false
    require_uppercase                = false
    temporary_password_validity_days = 7
  }

  # 管理者によるリセットのみ（メール送信の設定が不要）。
  account_recovery_setting {
    recovery_mechanism {
      name     = "admin_only"
      priority = 1
    }
  }
}

resource "aws_cognito_user_group" "minecraft" {
  name         = "minecraft"
  user_pool_id = aws_cognito_user_pool.this.id
}

# プレフィックスドメインは craftlatch-<8 桁の 16 進数>。CI から値を渡す経路を増やさないため random_id で生成する。
resource "random_id" "domain" {
  byte_length = 4
}

resource "aws_cognito_user_pool_domain" "this" {
  domain                = "${var.project}-${random_id.domain.hex}"
  user_pool_id          = aws_cognito_user_pool.this.id
  managed_login_version = 2
}

# パブリッククライアント（シークレットなし）+ PKCE。認可コードフローのみ、スコープは openid のみ。
resource "aws_cognito_user_pool_client" "web" {
  name            = "${var.project}-web"
  user_pool_id    = aws_cognito_user_pool.this.id
  generate_secret = false

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["${aws_apigatewayv2_api.this.api_endpoint}/callback"]

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
  user_pool_id = aws_cognito_user_pool.this.id

  use_cognito_provided_values = true
}
