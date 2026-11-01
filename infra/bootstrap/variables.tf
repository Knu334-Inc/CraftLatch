variable "region" {
  description = "AWS リージョン"
  type        = string
  default     = "ap-northeast-1"
}

# GitHub の OIDC トークンの sub の接頭辞。このリポジトリは immutable subject（オーナーとリポジトリの数値 ID 付き）を使う。
# 確認: gh api repos/<OWNER>/<NAME>/actions/oidc/customization/sub の sub_claim_prefix。
# ID を含むため、リポジトリを作り直したり同じ名前が別の所有者に取られたりしても一致しない。
variable "github_oidc_subject_prefix" {
  description = "OIDC の sub の接頭辞（repo:OWNER@OWNER_ID/NAME@REPO_ID）"
  type        = string
  default     = "repo:Knu334-Inc@272232200/CraftLatch@1441016403"

  validation {
    condition     = can(regex("^repo:[A-Za-z0-9-]+@[0-9]+/[A-Za-z0-9._-]+@[0-9]+$", var.github_oidc_subject_prefix))
    error_message = "repo:OWNER@OWNER_ID/NAME@REPO_ID の形式（immutable subject）で指定してください。"
  }
}

variable "state_bucket" {
  description = "Terraform state バケット名（アカウント ID を含むため terraform.tfvars 等で渡し、コミットしない）"
  type        = string
}

# token.actions.githubusercontent.com の OIDC プロバイダーはアカウントに 1 つしか作れない。
# 他のシステムが既に作っている場合はその ARN を渡す（bootstrap はそのプロバイダーを作成・変更・削除しない）。
# 既存のプロバイダーの client_id_list（Audience）に sts.amazonaws.com が含まれている必要がある。
variable "existing_github_oidc_provider_arn" {
  description = "既存の GitHub OIDC プロバイダーの ARN。空なら bootstrap が作成する"
  type        = string
  default     = ""

  validation {
    condition     = var.existing_github_oidc_provider_arn == "" || can(regex("^arn:aws:iam::[0-9]+:oidc-provider/token\\.actions\\.githubusercontent\\.com$", var.existing_github_oidc_provider_arn))
    error_message = "token.actions.githubusercontent.com の OIDC プロバイダーの ARN を指定してください。"
  }
}
