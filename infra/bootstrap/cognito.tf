# Cognito User Pool（設計書 4.1）。共有アカウントで他の User Pool を GitHub Actions の apply ロールから
# 操作させないため、Pool 本体は bootstrap で管理者が作成・変更する（apply ロールには Pool の作成・変更・削除・タグ付けを許可しない）。
# アプリクライアント・ドメイン・グループ・ブランディングは infra/terraform が名前（craftlatch-users）で Pool を参照して作る。
resource "aws_cognito_user_pool" "this" {
  name           = "craftlatch-users"
  user_pool_tier = "ESSENTIALS"

  # 誤削除の防止。削除するときは管理者がここを INACTIVE にして apply してから削除する。
  deletion_protection = "ACTIVE"

  # サインイン識別子はユーザー名（username_attributes・alias_attributes は指定しない）。
  # ユーザー名の大文字小文字は区別しない（作成後は変更できず、変えると User Pool が作り直しになる）。
  username_configuration {
    case_sensitive = false
  }

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  mfa_configuration = "OFF"

  # 最小 8 文字（利用者の負担を考慮した管理者の判断。MFA はないため、推測されにくいパスワードを利用者に求める）。文字種の条件は課さない。
  password_policy {
    minimum_length                   = 8
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
