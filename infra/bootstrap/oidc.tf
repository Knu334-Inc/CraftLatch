# GitHub Actions の OIDC プロバイダー。
# GitHub は AWS が信頼するルート CA で検証されるため thumbprint_list は指定しない（省略可能）。
# アカウントに 1 つしか作れないため、既存のもの（var.existing_github_oidc_provider_arn）があれば作らずにそれを使う。
# 既存のものを使う場合、その client_id_list に sts.amazonaws.com が含まれている必要がある（bootstrap は変更しない）。
resource "aws_iam_openid_connect_provider" "github" {
  count = var.existing_github_oidc_provider_arn == "" ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

locals {
  github_oidc_provider_arn = var.existing_github_oidc_provider_arn == "" ? aws_iam_openid_connect_provider.github[0].arn : var.existing_github_oidc_provider_arn
}

# plan ロールの信頼ポリシー: このリポジトリの PR からのみ。
data "aws_iam_policy_document" "plan_trust" {
  statement {
    sid     = "GitHubOidcPullRequest"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:pull_request"]
    }
  }
}

# apply ロールの信頼ポリシー: Environment production のジョブからのみ。
# main への限定は Environment の許可ブランチ設定で成立する（設計書 4.7）。
data "aws_iam_policy_document" "apply_trust" {
  statement {
    sid     = "GitHubOidcProductionEnvironment"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:environment:production"]
    }
  }
}
