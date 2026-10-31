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
