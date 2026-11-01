variable "region" {
  description = "AWS リージョン"
  type        = string
  default     = "ap-northeast-1"
}

variable "github_repository" {
  description = "OIDC の sub に使う GitHub リポジトリ（owner/name）"
  type        = string
  default     = "Knu334-Inc/CraftLatch"
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
